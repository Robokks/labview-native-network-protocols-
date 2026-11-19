"""Session and stream endpoints (client role: connects to a LabVIEW-hosted endpoint).

Handle rule (confirmed live): every link message carries [sender's handle][receiver's handle].
"""
from __future__ import annotations

import collections
import itertools
import os
import queue
import socket
import struct
import threading
import time
from typing import Callable, Optional

from .framing import Frame, FrameBuffer, MsgType, ProtocolError, lv_string
from .locator import locate_port
from .types import (ROLE_READER, ROLE_WRITER, DataType, link_tail, pack_element,
                    unpack_element)

SERVICE = b"__NI_DEFAULT_STREAM_LISTENER_NAME__/NW_STREAMS"
_PARAMS = bytes.fromhex("d00700000a000000")


class LinkRefused(ProtocolError):
    def __init__(self, url: str, code: int):
        super().__init__(f"LabVIEW refused link to {url}: error -{code} "
                         f"(remote endpoint does not exist or is busy)")
        self.code = code


class StreamClosed(ProtocolError):
    pass


class _Link:
    def __init__(self, session: "Session", remote_url: str, local_url: str, td: bytes):
        self.session = session
        self.remote_url, self.local_url = remote_url, local_url
        self.id = os.urandom(8)
        self.handle = next(session._handles)
        self.td = td
        self.peer_id: bytes = b""
        self.peer_handle = 0
        self.buffer_size = 0
        self.closed = threading.Event()
        self._accepted = threading.Event()
        self._error: Optional[Exception] = None

    # -- called from the session receive thread --
    def _on_frame(self, f: Frame) -> None:
        t, pl = f.type, f.payload
        if t == MsgType.LINK_ACCEPT:
            self.peer_id = pl[:8]
            self.peer_handle, _, self.buffer_size = struct.unpack_from("<III", pl, 16)
            self._accepted.set()
        elif t == MsgType.LINK_REFUSED:
            self._error = LinkRefused(self.remote_url, struct.unpack_from("<I", pl, 12)[0])
            self._accepted.set()
        elif t == MsgType.LINK_CLOSED:          # peer closed: acknowledge
            self.session._send(Frame(MsgType.LINK_CLOSED_ACK, pl[:12]))
            self.closed.set()
        elif t == MsgType.LINK_CLOSED_ACK:
            self.closed.set()

    def _open(self, timeout: float, wait_for_peer: float = 0.0) -> None:
        """wait_for_peer > 0: like LabVIEW itself, resend the request every 10 ms while the
        remote endpoint does not exist yet (refused with -314050), up to that many seconds."""
        s = self.session
        create = Frame(MsgType.LINK_CREATE, self.id + struct.pack("<I", self.handle)
                       + lv_string(self.remote_url.encode()) + lv_string(self.local_url.encode())
                       + self.td)
        deadline = time.monotonic() + wait_for_peer
        while True:
            self._accepted.clear(); self._error = None
            s._send(create)
            if not self._accepted.wait(timeout):
                raise TimeoutError(f"no answer to link request for {self.remote_url}")
            if not self._error:
                break
            if (isinstance(self._error, LinkRefused) and self._error.code == 314050
                    and time.monotonic() < deadline):
                time.sleep(0.01)
                continue
            raise self._error
        s._send(Frame(MsgType.LINK_CONFIRM, self.id + struct.pack("<II", self.handle,
                                                                   self.peer_handle) + b"\x01"))

    def close(self, timeout: float = 2.0) -> None:
        if self.closed.is_set() or not self.peer_id:
            return
        try:
            self.session._send(Frame(MsgType.LINK_CLOSED,
                                     self.peer_id + struct.pack("<I", self.peer_handle)))
        except OSError:
            return
        self.closed.wait(timeout)

    def _hdls(self) -> bytes:
        return struct.pack("<II", self.handle, self.peer_handle)


class Reader(_Link):
    """Reads elements from a LabVIEW Network Stream Writer endpoint."""

    def __init__(self, *a, decoder: Optional[Callable[[bytes], object]] = None, **kw):
        super().__init__(*a, **kw)
        self.decoder = decoder
        self._q: "queue.Queue[bytes]" = queue.Queue()

    def _on_frame(self, f: Frame) -> None:
        if f.type == MsgType.DATA:
            _src, _dst, cnt, _ = struct.unpack_from("<IIII", f.payload, 0)
            self.session._send(Frame(MsgType.RECEIVED, self._hdls() + struct.pack("<I", cnt)))
            self._q.put(f.payload[12:])   # element bytes (incl. length prefix if any)
        else:
            super()._on_frame(f)
            if self.closed.is_set():
                self._q.put(b"")  # wake readers

    def _open(self, timeout: float, wait_for_peer: float = 0.0) -> None:
        super()._open(timeout, wait_for_peer)
        # grant the writer as many credits as our buffer holds
        self.session._send(Frame(MsgType.CREDIT_GRANT, self.id + self._hdls()
                                 + struct.pack("<II", self.buffer_size, 0)))

    def read_raw(self, timeout: Optional[float] = None) -> bytes:
        """Return one flattened element. Returning its slot to the writer happens here,
        so a slow consumer applies real back-pressure to LabVIEW."""
        try:
            data = self._q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no element within timeout") from None
        if not data and self.closed.is_set():
            raise StreamClosed("writer endpoint closed the stream")
        self.session._send(Frame(MsgType.CONSUMED, self._hdls() + struct.pack("<I", 1)))
        return data

    def read(self, timeout: Optional[float] = None):
        data = self.read_raw(timeout)
        return self.decoder(data) if self.decoder else data

    @property
    def available(self) -> int:
        return self._q.qsize()


class Writer(_Link):
    """Writes elements to a LabVIEW Network Stream Reader endpoint (credit-based)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._credits = threading.Semaphore(0)
        self._granted = threading.Event()

    def _on_frame(self, f: Frame) -> None:
        if f.type == MsgType.CREDIT_GRANT:
            for _ in range(struct.unpack_from("<I", f.payload, 16)[0]):
                self._credits.release()
            self._granted.set()
        elif f.type == MsgType.CONSUMED:
            for _ in range(struct.unpack_from("<I", f.payload, 8)[0]):
                self._credits.release()
        elif f.type == MsgType.RECEIVED:
            pass
        else:
            super()._on_frame(f)

    def write(self, value, timeout: Optional[float] = 5.0) -> None:
        if self.dtype is None:
            raise TypeError("link opened with a raw descriptor; use write_raw()")
        self.write_raw(pack_element(self.dtype, value), timeout)

    def write_raw(self, flat: bytes, timeout: Optional[float] = 5.0) -> None:
        if self.closed.is_set():
            raise StreamClosed("reader endpoint closed the stream")
        if not self._credits.acquire(timeout=timeout):
            raise TimeoutError("reader buffer full (no credit) within timeout")
        self.session._send(Frame(MsgType.DATA, self._hdls() + struct.pack("<I", 1) + flat))


class Session:
    """One TCP session to a LabVIEW Network Streams listener; carries any number of links."""

    def __init__(self, host: str = "127.0.0.1", port: Optional[int] = None,
                 url_host: Optional[str] = None, timeout: float = 5.0,
                 trace: Optional[Callable[[str, Frame], None]] = None):
        self.host = host
        self.port = port or locate_port(host, timeout)
        self.url_host = url_host or socket.gethostname().upper()
        self.timeout = timeout
        self.trace = trace
        self._handles = itertools.count(0x00600001)
        self._links: dict = {}           # by local handle and by local id
        self._lock = threading.Lock()
        self._fb = FrameBuffer()
        self._pending: "collections.deque[Frame]" = collections.deque()
        self._sock = socket.create_connection((host, self.port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._handshake()
        self._closed = False
        self._rx = threading.Thread(target=self._rx_loop, name="nwstreams-rx", daemon=True)
        self._rx.start()

    # -- public API --
    def url(self, path: str) -> str:
        return path if path.startswith("ni.dex://") else f"ni.dex://{self.url_host}/{path.lstrip('/')}"

    def open_reader(self, writer_url: str, reader_url: str, dtype: "DataType | bytes",
                    buffer_size: int = 100, decoder=None,
                    timeout: Optional[float] = None, wait_for_peer: float = 0.0) -> Reader:
        """Connect a reader to a LabVIEW writer endpoint. dtype: a nwstreams.types
        DataType (e.g. DBL, Array(STRING)), or raw descriptor bytes from a capture."""
        if isinstance(dtype, DataType):
            tail = link_tail(dtype, buffer_size, ROLE_READER)
            decoder = decoder or (lambda b, t=dtype: unpack_element(t, b))
        else:
            tail = dtype
        r = Reader(self, self.url(writer_url), self.url(reader_url), tail, decoder=decoder)
        r.dtype = dtype if isinstance(dtype, DataType) else None
        self._register(r); r._open(timeout or self.timeout, wait_for_peer)
        return r

    def open_writer(self, reader_url: str, writer_url: str, dtype: "DataType | bytes",
                    buffer_size: int = 10, timeout: Optional[float] = None,
                    wait_for_peer: float = 0.0) -> Writer:
        """Connect a writer to a LabVIEW reader endpoint."""
        tail = link_tail(dtype, buffer_size, ROLE_WRITER) if isinstance(dtype, DataType) else dtype
        w = Writer(self, self.url(reader_url), self.url(writer_url), tail)
        w.dtype = dtype if isinstance(dtype, DataType) else None
        self._register(w); w._open(timeout or self.timeout, wait_for_peer)
        if not w._granted.wait(timeout or self.timeout):
            raise TimeoutError("reader did not grant credits")
        return w

    def close(self) -> None:
        if self._closed:
            return
        for link in {id(l): l for l in self._links.values()}.values():
            link.close()
        self._closed = True
        try:
            self._send(Frame(MsgType.SESSION_CLOSE))
        except OSError:
            pass
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals --
    def _register(self, link: _Link) -> None:
        self._links[link.handle] = link
        self._links[link.id] = link

    def _send(self, f: Frame) -> None:
        data = f.encode()
        with self._lock:
            self._sock.sendall(data)
        if self.trace:
            self.trace("TX", f)

    def _recv_frame(self) -> Frame:
        while not self._pending:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("LabVIEW closed the session")
            self._pending.extend(self._fb.feed(chunk))
        return self._pending.popleft()

    def _expect(self, typ: int) -> Frame:
        while True:
            f = self._recv_frame()
            if self.trace:
                self.trace("RX", f)
            if f.type == MsgType.PARAMS and not f.payload and typ != MsgType.PARAMS:
                continue
            if f.type != typ:
                raise ProtocolError(f"expected 0x{typ:02x}, got 0x{f.type:02x}")
            return f

    def _handshake(self) -> None:
        self._send(Frame(MsgType.SESSION_OPEN, src=0x03F9, dst=0))
        self._expect(MsgType.SESSION_ACCEPT)
        self._send(Frame(MsgType.PARAMS, _PARAMS, flags=1, src=0x03F9, dst=0x03FA))
        self._send(Frame(MsgType.SERVICE_OPEN, lv_string(SERVICE) + bytes.fromhex("e90302"),
                         src=0x03F9, dst=0x03FA))
        self._expect(MsgType.PARAMS)
        reply = self._expect(MsgType.SERVICE_REPLY)
        if reply.payload[:4] != b"\x00\x00\x00\x00":
            raise ProtocolError(f"service open failed: {reply.payload.hex()}")

    def _route(self, f: Frame) -> Optional[_Link]:
        t, pl = f.type, f.payload
        if t == MsgType.LINK_ACCEPT:
            return self._links.get(pl[8:16])
        if t in (MsgType.LINK_REFUSED, MsgType.LINK_CLOSED):
            return self._links.get(pl[:8])
        if t == MsgType.LINK_CLOSED_ACK:
            return next((l for l in self._links.values() if l.peer_id == pl[:8]), None)
        if t in (MsgType.WRITER_READY, MsgType.CREDIT_GRANT):
            return self._links.get(struct.unpack_from("<I", pl, 12)[0])
        if t in (MsgType.DATA, MsgType.RECEIVED, MsgType.CONSUMED):
            return self._links.get(struct.unpack_from("<I", pl, 4)[0])
        return None

    def _rx_loop(self) -> None:
        self._sock.settimeout(None)
        try:
            while True:
                if self._pending:
                    frames, self._pending = list(self._pending), collections.deque()
                else:
                    chunk = self._sock.recv(1 << 16)
                    if not chunk:
                        break
                    frames = self._fb.feed(chunk)
                for f in frames:
                    if self.trace:
                        self.trace("RX", f)
                    link = self._route(f)
                    if link:
                        link._on_frame(f)
        except OSError:
            pass
        finally:
            for link in self._links.values():
                if not link.closed.is_set():
                    link.closed.set()
                    if isinstance(link, Reader):
                        link._q.put(b"")


def connect(host: str = "127.0.0.1", port: Optional[int] = None, *, url_host: Optional[str] = None,
            retry_for: float = 0.0, timeout: float = 5.0, **kw) -> Session:
    """Open a session; with retry_for > 0, keep retrying (e.g. until LabVIEW is up)."""
    deadline = time.monotonic() + retry_for
    while True:
        try:
            return Session(host, port, url_host, timeout, **kw)
        except (OSError, ProtocolError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
