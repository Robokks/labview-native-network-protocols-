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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RawElement:
    """An element the reader's decoder failed on, kept byte-exact.

    read() and drain() return it in the element's place, so a decode failure loses
    nothing and the elements around it are still delivered. data: the flattened element
    (as read_raw() returns it); error: the decoder's exception as text."""
    data: bytes
    error: str


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
            # 12-byte header; the element may be shorter than 4 bytes (a top-level I8/U8 is 1 byte)
            _src, _dst, cnt = struct.unpack_from("<III", f.payload, 0)
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

    # Flow control: every element taken off the queue must be acknowledged with exactly one
    # CONSUMED, which returns its buffer slot (credit) to the writer. An element that is never
    # acknowledged costs the writer a credit for good, and after buffer_size of them the
    # LabVIEW writer blocks. So an element is acknowledged once it has been decoded and is
    # being handed to the caller (as a RawElement if the decoder failed), never before and
    # never twice.

    def _consumed(self) -> None:
        try:
            self.session._send(Frame(MsgType.CONSUMED, self._hdls() + struct.pack("<I", 1)))
        except OSError:
            pass     # session gone: no slot to return; the close is reported by the next call

    def _decode(self, data: bytes):
        if not self.decoder:
            return data
        try:
            return self.decoder(data)
        except Exception as exc:            # keep the bytes instead of losing the element
            return RawElement(bytes(data), f"{type(exc).__name__}: {exc}")

    def _get(self, timeout: Optional[float]) -> bytes:
        try:
            data = self._q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no element within timeout") from None
        if not data and self.closed.is_set():
            raise StreamClosed("writer endpoint closed the stream")
        return data

    def read_raw(self, timeout: Optional[float] = None) -> bytes:
        """Return one flattened element. Returning its slot to the writer happens here,
        so a slow consumer applies real back-pressure to LabVIEW."""
        data = self._get(timeout)
        self._consumed()
        return data

    def read(self, timeout: Optional[float] = None):
        """Next element, decoded; a RawElement with its bytes if the decoder fails."""
        value = self._decode(self._get(timeout))
        self._consumed()
        return value

    def drain(self, max_items: Optional[int] = None) -> list:
        """Every element available now (decoded), oldest first, without waiting.
        An element the decoder fails on comes back as a RawElement in its place.
        Raises StreamClosed once the writer has closed and nothing is left."""
        out = []
        while max_items is None or len(out) < max_items:
            try:
                data = self._q.get_nowait()
            except queue.Empty:
                break
            if not data and self.closed.is_set():
                if out:
                    self._q.put(b"")       # report the close on the next call
                    break
                raise StreamClosed("writer endpoint closed the stream")
            out.append(self._decode(data))
            self._consumed()
        return out

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
                 trace: Optional[Callable[[str, Frame], None]] = None,
                 context: Optional[str] = None, local_context: Optional[str] = None):
        """context: the REMOTE endpoints' Network Streams context (None = LabVIEW's default;
        e.g. "RT" for a JKI endpoint named RT). local_context: the context named in Python's
        own endpoint URLs (e.g. "PC"); defaults to context."""
        self.host = host
        self.context = context
        self.local_context = local_context if local_context is not None else context
        self.port = port or locate_port(host, timeout, context)
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
    def url(self, path: str, local: bool = False) -> str:
        """ni.dex://<url_host>[:<context>]/<path>; full ni.dex:// URLs pass through."""
        if path.startswith("ni.dex://"):
            return path
        ctx = self.local_context if local else self.context
        host = f"{self.url_host}:{ctx}" if ctx else self.url_host
        return f"ni.dex://{host}/{path.lstrip('/')}"

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
        r = Reader(self, self.url(writer_url), self.url(reader_url, local=True), tail, decoder=decoder)
        r.dtype = dtype if isinstance(dtype, DataType) else None
        self._register(r); r._open(timeout or self.timeout, wait_for_peer)
        return r

    def open_writer(self, reader_url: str, writer_url: str, dtype: "DataType | bytes",
                    buffer_size: int = 10, timeout: Optional[float] = None,
                    wait_for_peer: float = 0.0) -> Writer:
        """Connect a writer to a LabVIEW reader endpoint."""
        tail = link_tail(dtype, buffer_size, ROLE_WRITER) if isinstance(dtype, DataType) else dtype
        w = Writer(self, self.url(reader_url), self.url(writer_url, local=True), tail)
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
        service = (f"{self.context.lower()}/NW_STREAMS".encode("latin-1") if self.context else SERVICE)
        self._send(Frame(MsgType.SERVICE_OPEN, lv_string(service) + bytes.fromhex("e90302"),
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
                        try:
                            link._on_frame(f)
                        except Exception as exc:  # one bad link must not stop the session
                            link._error = exc
                            link.closed.set()
                            if isinstance(link, Reader):
                                link._q.put(b"")
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
