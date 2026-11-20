"""Network-published Shared Variables (NI-PSP) client. Experimental; 25 LabVIEW types verified.

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
from dataclasses import dataclass
from typing import Optional

from .datasocket import _ds_type, _user_attrs, _write_type
from .framing import Frame, FrameBuffer, ProtocolError, lv_string
from .types import (BOOLEAN, DBL, I8, I16, I32, SGL, STRING, TIMESTAMP, U8, U16, U32, DataType,
                    LVVariant, infer_type, parse_td_body)

SVE_PORT = 59110          # tagsrv's port when first seen; it can move (59111 later)
SVE_PORTS = tuple(range(59110, 59121))   # tried in turn when no port is given
CTX_PROC, CTX_ITEMS = 0x03E9, 0x03EA
_PARAMS = bytes.fromhex("d00700000a000000")
# Context + user block sent with the control-environment service, copied from the capture.
_CTRLENV_TAIL = bytes.fromhex("ea03021c0000002900000008000000284e6f626f6479296461164f65006300")
_OPEN_TAIL = bytes.fromhex("0c0000000000")
SUBSCRIBE_FLAGS = 0x0084  # value LabVIEW sent when its reader started


class Msg:
    OPEN, SUBSCRIBE, WRITE, CLOSE = 0x01, 0x02, 0x03, 0x04
    OPENED, PROPERTIES, UPDATE, CLOSED = 0x65, 0x66, 0x68, 0x6A


# ---- value records ----
# record := u32 n (bytes that follow) + u32 kind + data. Seen live with LabVIEW 2026
# (TypeTest Variables, 25 types). Low 16 bits of kind = encoding:
#   0x0003  number sent as f64; high 16 bits = the variable's type (_NUM_SUBTYPES)
#   0x0002  Boolean, 1 byte (01 = true)
#   0x1005  String: u32 length + bytes (no padding)
#   0x100f  Timestamp: u64 fraction + i64 seconds since 1904
#   0x1013  anything else: a self-describing variant (u32 len + descriptor + value),
#           encoded as DataSocket does (2-D arrays column-major, variant attributes
#           carried in the value's descriptor)
K_NUM, K_BOOL, K_STRING, K_TIME, K_VARIANT = 0x0003, 0x0002, 0x1005, 0x100F, 0x1013
_NUM_SUBTYPES = {0: DBL, 1: I8, 2: I16, 3: I32, 4: U8, 5: U16, 6: U32, 7: SGL}
_NUM_KIND = {t: (sub << 16) | K_NUM for sub, t in _NUM_SUBTYPES.items()}
_INT_TYPES = (I8, I16, I32, U8, U16, U32)


@dataclass(frozen=True)
class RawRecord:
    """A value record the client could not decode, kept byte-exact.

    kind: the record's u32 kind field (low 16 bits = encoding, see above).
    data: the record's bytes after the kind field.
    Detect it with ``isinstance(value, nw.RawRecord)``. Before 0.4.1 this was the plain
    tuple ``("raw", kind, data)``.
    """
    kind: int
    data: bytes


def dbl_record(v: float) -> bytes:
    """DBL record: u32 12 + u32 3 + f64."""
    return struct.pack("<IId", 12, 3, v)


def encode_record(value, dtype: Optional[DataType] = None) -> bytes:
    """Value record for a write. dtype: the variable's LabVIEW type (inferred if omitted)."""
    if dtype is None and not isinstance(value, LVVariant):
        dtype = infer_type(value)
    if dtype in _NUM_KIND:
        body = struct.pack("<Id", _NUM_KIND[dtype], float(value))
    elif dtype is BOOLEAN:
        body = struct.pack("<IB", K_BOOL, 1 if value else 0)
    elif dtype is STRING:
        s = value.encode("latin-1") if isinstance(value, str) else bytes(value)
        body = struct.pack("<II", K_STRING, len(s)) + s
    elif dtype is TIMESTAMP:
        body = struct.pack("<I", K_TIME) + TIMESTAMP.pack(value)
    else:
        t, v = _write_type(value, dtype)
        body = struct.pack("<I", K_VARIANT) + _variant_bytes(t, v)
    return struct.pack("<I", len(body)) + body


def _variant_bytes(t: DataType, v) -> bytes:
    inner = t.td_body() + t.pack(v)
    return struct.pack("<I", 4 + len(inner)) + inner


def parse_record(p: bytes, o: int):
    """Decode a value record; unknown or undecodable encodings come back as RawRecord."""
    n, kind = struct.unpack_from("<II", p, o)
    data, low = p[o + 8:o + 4 + n], kind & 0xFFFF
    try:
        if low == K_NUM and len(data) == 8:
            x = struct.unpack_from("<d", data, 0)[0]
            t = _NUM_SUBTYPES.get(kind >> 16, DBL)
            return int(x) if t in _INT_TYPES else x
        if low == K_BOOL and len(data) >= 1:
            return data[0] != 0
        if low == K_STRING:
            m = struct.unpack_from("<I", data, 0)[0]
            return data[4:4 + m].decode("latin-1")
        if low == K_TIME and len(data) == 16:
            return TIMESTAMP.unpack(data, 0)[0]
        if low == K_VARIANT:
            t, q = parse_td_body(data, 4)
            t = _ds_type(t)
            v = t.unpack(data, q)[0]
            attrs = _user_attrs(t)
            return LVVariant(v, attrs) if attrs is not None else v
    except (NotImplementedError, struct.error, IndexError, UnicodeDecodeError):
        pass
    return RawRecord(kind, bytes(data))


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

    def __init__(self, library: str, host: str = "127.0.0.1", port: Optional[int] = None,
                 machine: Optional[str] = None, timeout: float = 5.0):
        """port=None: try SVE_PORTS in turn and keep the first port whose engine serves
        *library*. The engine's port moved from 59110 to 59111 on the test PC after its
        NI services restarted, so a fixed default is not reliable."""
        self.library = library
        self.machine = (machine or socket.gethostname()).upper()
        self.timeout = timeout
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._items: "dict[int, _Item]" = {}
        self._by_name: "dict[str, _Item]" = {}
        errors = []
        for p in ([port] if port else SVE_PORTS):
            self._fb = FrameBuffer()
            self._pending: "collections.deque[Frame]" = collections.deque()
            try:
                self._sock = socket.create_connection((host, p), timeout=timeout)
            except OSError as exc:
                errors.append(f"{p}: {exc}")
                continue
            try:
                self._handshake()
                self.port = p
                break
            except (OSError, ProtocolError) as exc:
                errors.append(f"{p}: {exc}")
                self._sock.close()
        else:
            raise ProtocolError(f"no Shared Variable Engine on {host} serves library "
                                f"{library!r} (deployed?): " + "; ".join(errors))
        self._closed = False
        self.closed = threading.Event()   # set when the connection is gone
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

    def drain(self, name: str) -> list:
        """Every update pushed for this variable since the last read/drain, oldest first,
        without waiting. Raises ConnectionError once the connection is gone and nothing
        is left."""
        q, out = self._item(name).values, []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                break
        if not out and self.closed.is_set():
            raise ConnectionError("Shared Variable Engine connection closed")
        return out

    def latest(self, name: str):
        return self._item(name).latest

    def write(self, name: str, value, dtype: Optional[DataType] = None) -> None:
        """Write a value. dtype: the variable's LabVIEW type, e.g. nw.I16 or
        nw.Array(nw.I32, 2); inferred from the value when omitted (float -> DBL,
        int -> I32, str -> String, ...)."""
        it = self._item(name)
        self._send(Frame(Msg.WRITE, struct.pack("<III", it.handle, it.id, 1)
                         + encode_record(value, dtype), src=CTX_ITEMS, dst=CTX_ITEMS))

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
                        it.last_raw = p[4:]
                        v = parse_record(p, 4)
                        it.latest = v
                        it.values.put(v)
                    elif t == Msg.CLOSED:
                        it.closed.set()
        except (OSError, ConnectionError):
            pass
        finally:
            self.closed.set()      # connection lost or closed: no more values will arrive
