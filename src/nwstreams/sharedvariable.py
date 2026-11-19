"""Network-published Shared Variables (NI-PSP) client. Experimental, DBL values only.

Talks to the Shared Variable Engine (tagsrv) with the same 'C7' Logos frames as Network
Streams. Decoded from a capture of NI's 'Shared Variable' example and confirmed live
(read and write) on LabVIEW 2026. See docs/SHARED_VARIABLE_FINDINGS.md.

    with SharedVariableClient("Server Variables") as sv:
        sv.open(r"psp:\\localhost\\Server Variables\\Monitor Value 1")
        print(sv.read("Monitor Value 1"))
        sv.write("Monitor Value 1", 0.25)
"""
from __future__ import annotations

import collections
import itertools
import queue
import socket
import struct
import threading
from typing import Optional

from .framing import Frame, FrameBuffer, ProtocolError, lv_string

SVE_PORT = 59110          # tagsrv on the test PC; pass port= if yours differs
CTX_PROC, CTX_ITEMS = 0x03E9, 0x03EA
_PARAMS = bytes.fromhex("d00700000a000000")
# Context + user block sent with the control-environment service, copied from the capture.
_CTRLENV_TAIL = bytes.fromhex("ea03021c0000002900000008000000284e6f626f6479296461164f65006300")
_OPEN_TAIL = bytes.fromhex("0c0000000000")
SUBSCRIBE_FLAGS = 0x0084  # value LabVIEW sent when its reader started


class Msg:
    OPEN, SUBSCRIBE, WRITE, CLOSE = 0x01, 0x02, 0x03, 0x04
    OPENED, PROPERTIES, UPDATE, CLOSED = 0x65, 0x66, 0x68, 0x6A


def dbl_record(v: float) -> bytes:
    """Value record seen for DBL: u32 12 + u32 3 + f64. (Meaning of 3 not decoded.)"""
    return struct.pack("<IId", 12, 3, v)


def parse_record(p: bytes, o: int):
    n, kind = struct.unpack_from("<II", p, o)
    if n == 12 and kind == 3:
        return struct.unpack_from("<d", p, o + 8)[0]
    return ("raw", kind, p[o + 8:o + 4 + n])   # not decoded yet


def parse_psp_url(url: str, default_lib: Optional[str] = None):
    r"""'psp:\\host\lib\var', '\\host\lib\var', 'lib\var' or 'var' -> (host, lib, var)."""
    u = url.replace("/", "\\")
    if u.lower().startswith("psp:"):
        u = u[4:]
    parts = [p for p in u.split("\\") if p]
    if u.startswith("\\\\") and len(parts) >= 3:
        return parts[0], parts[1], "\\".join(parts[2:])
    if len(parts) >= 2:
        return None, parts[0], "\\".join(parts[1:])
    return None, default_lib, parts[0] if parts else ""


class _Item:
    def __init__(self, item_id: int, name: str):
        self.id, self.name = item_id, name
        self.handle: Optional[int] = None
        self.opened = threading.Event()
        self.closed = threading.Event()
        self.properties = b""
        self.values: "queue.Queue" = queue.Queue()
        self.latest = None


class SharedVariableClient:
    """One session to the Shared Variable Engine process of one variable library."""

    def __init__(self, library: str, host: str = "127.0.0.1", port: int = SVE_PORT,
                 machine: Optional[str] = None, timeout: float = 5.0):
        self.library = library
        self.machine = (machine or socket.gethostname()).upper()
        self.timeout = timeout
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._fb = FrameBuffer()
        self._pending: "collections.deque[Frame]" = collections.deque()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._items: "dict[int, _Item]" = {}
        self._by_name: "dict[str, _Item]" = {}
        self._handshake()
        self._closed = False
        threading.Thread(target=self._rx, name="sharedvariable-rx", daemon=True).start()

    # ---- public API ----
    def open(self, url: str, subscribe: bool = True) -> str:
        """Open a variable (psp URL or name in this library); returns its name."""
        host, lib, var = parse_psp_url(url, self.library)
        if lib and lib != self.library:
            raise ValueError(f"variable is in library {lib!r}; this client serves {self.library!r}")
        if var in self._by_name:
            return var
        machine = self.machine if host in (None, "localhost", "127.0.0.1") else host.upper()
        it = _Item(next(self._ids), var)
        self._items[it.id] = it
        self._by_name[var] = it
        path = f"\\\\{machine}\\{self.library}\\{var}".encode("latin-1")
        self._send(Frame(Msg.OPEN, struct.pack("<I", it.id) + lv_string(path) + _OPEN_TAIL,
                         src=CTX_ITEMS, dst=CTX_ITEMS))
        if not it.opened.wait(self.timeout):
            raise TimeoutError(f"Shared Variable Engine did not open {path.decode()!r} "
                               f"(is the library deployed?)")
        if subscribe:
            self._send(Frame(Msg.SUBSCRIBE, struct.pack("<IIH", it.handle, it.id, SUBSCRIBE_FLAGS),
                             src=CTX_ITEMS, dst=CTX_ITEMS))
        return var

    def read(self, name: str, timeout: Optional[float] = None):
        """Next value pushed by the engine (the current value arrives right after open)."""
        try:
            return self._item(name).values.get(timeout=timeout or self.timeout)
        except queue.Empty:
            raise TimeoutError(f"no update for {name!r} within timeout") from None

    def latest(self, name: str):
        return self._item(name).latest

    def write(self, name: str, value: float) -> None:
        it = self._item(name)
        self._send(Frame(Msg.WRITE, struct.pack("<III", it.handle, it.id, 1) + dbl_record(float(value)),
                         src=CTX_ITEMS, dst=CTX_ITEMS))

    def properties(self, name: str) -> bytes:
        return self._item(name).properties

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            for it in self._items.values():
                if it.handle is not None:
                    self._send(Frame(Msg.CLOSE, struct.pack("<II", it.handle, it.id),
                                     src=CTX_ITEMS, dst=CTX_ITEMS))
            self._send(Frame(0xE5, src=CTX_ITEMS, dst=CTX_ITEMS))
            self._send(Frame(0xE5))
        except OSError:
            pass
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- internals ----
    def _item(self, name: str) -> _Item:
        var = parse_psp_url(name, self.library)[2]
        try:
            return self._by_name[var]
        except KeyError:
            raise KeyError(f"{var!r} is not open; call open() first") from None

    def _send(self, f: Frame) -> None:
        with self._lock:
            self._sock.sendall(f.encode())

    def _recv(self) -> Frame:
        while not self._pending:
            d = self._sock.recv(65536)
            if not d:
                raise ConnectionError("Shared Variable Engine closed the connection")
            self._pending.extend(self._fb.feed(d))
        return self._pending.popleft()

    def _expect(self, t: int) -> Frame:
        while True:
            f = self._recv()
            if f.type == t:
                return f
            if not (f.type == 0xDF and not f.payload):
                raise ProtocolError(f"expected 0x{t:02x}, got 0x{f.type:02x}")

    def _handshake(self) -> None:
        self._send(Frame(0xE0, src=0x042A, dst=0))
        self._expect(0xE1)
        self._send(Frame(0xDF, _PARAMS, flags=1, src=0x042A, dst=0x03EB))
        self._send(Frame(0xE3, lv_string(f"{self.library}/PROCESS".encode("latin-1"))
                         + bytes.fromhex("e90301"), src=0x042A, dst=0x03EB))
        self._expect(0xDF)
        r = self._expect(0xE4)
        if r.payload[:4] != b"\0\0\0\0":
            raise ProtocolError(f"library process {self.library!r} not available "
                                f"(deployed?): {r.payload.hex()}")
        self._send(Frame(0xE3, lv_string(b"TaggerCtrlEnv/CONTROLENV_REALTIME") + _CTRLENV_TAIL,
                         flags=1, src=0x042A, dst=0x03EB))
        r = self._expect(0xE4)
        if r.payload[:4] != b"\0\0\0\0":
            raise ProtocolError(f"control environment refused: {r.payload.hex()}")

    def _rx(self) -> None:
        self._sock.settimeout(None)
        try:
            while True:
                f = self._recv()
                t, p = f.type, f.payload
                if t in (Msg.OPENED, Msg.PROPERTIES, Msg.UPDATE, Msg.CLOSED) and len(p) >= 4:
                    it = self._items.get(struct.unpack_from("<I", p, 0)[0])
                    if it is None:
                        continue
                    if t == Msg.OPENED:
                        it.handle = struct.unpack_from("<I", p, 4)[0]
                        it.opened.set()
                    elif t == Msg.PROPERTIES:
                        it.properties = p[4:]
                    elif t == Msg.UPDATE and f.flags == 8:
                        v = parse_record(p, 4)
                        it.latest = v
                        it.values.put(v)
                    elif t == Msg.CLOSED:
                        it.closed.set()
        except (OSError, ConnectionError):
            pass
