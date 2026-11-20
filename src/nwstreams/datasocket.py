"""DataSocket (dstp://) client for the NI DataSocket Server (TCP 3015). Experimental.

Wire format (decoded from a capture of NI's 'Simple DataSocket' example, confirmed live):
every message is a flattened variant, u32 len (incl. itself) + type descriptor + value,
and the value is a cluster whose first I32 is the opcode.

  client -> server                      server -> client
  (1, 3), (2, 3)    hello, version 3    (9, 3)               hello ack
  (4, item, mode)   open item           (56, item)           sent before the open ack
                                        (12, item, mode)     open ack
  (13, item, value, seq:U32)  write     (14, seq:U32, 1)     write ack
                                        (6, item, value)     new value for subscribers
  (5, item)         close item          (10,)                not decoded yet
  (3,)              disconnect

The open field is an access mode, not an id: LabVIEW's DataSocket Open sent 4 for "Write"
and 7 for "Read" (the server ignored an open with 100). Probably bit flags, not verified.
Behaviour seen live: opening an item pushes its current value at once (0 for a new item),
and the server does not echo a write back to the connection that wrote it.
The value inside 13/6 carries its own type descriptor, so any type nwstreams.types
supports can be written or read.
"""
from __future__ import annotations

import itertools
import queue
import socket
import struct
import threading
from typing import Any, Optional

from .types import (I32, STRING, U32, WAVEFORM, Array, Attributed, Attributes, Cluster,
                    ColumnMajorArray, DataType, LVVariant, VariantValue, VARIANT, infer_type,
                    parse_td_body)


def _ds_type(t: DataType) -> DataType:
    """The type as the DataSocket Server carries it: arrays of 2+ dimensions column-major."""
    if isinstance(t, Array) and not isinstance(t, ColumnMajorArray):
        e = _ds_type(t.element)
        return ColumnMajorArray(e, t.ndims) if t.ndims > 1 else Array(e, 1)
    if isinstance(t, Cluster):
        return Cluster(*(_ds_type(f) for f in t.fields))
    if isinstance(t, Attributed) and t is not WAVEFORM:
        return Attributed(_ds_type(t.inner), t.attributes)
    return t


def _user_attrs(t: DataType):
    """Attributes LabVIEW moved from a variant into the value's descriptor (not _NI_*)."""
    if isinstance(t, Attributed) and t is not WAVEFORM \
            and not all(k.startswith("_NI_") for k in t.attributes):
        return t.attributes
    return None


def _write_type(value, dtype: Optional[DataType]):
    """(type, value) to send. A variant with attributes is sent the way LabVIEW's
    DataSocket Write sends it: the contents' own type carrying the attributes."""
    if isinstance(value, LVVariant) and dtype in (None, VARIANT):
        inner_t = infer_type(value.value) if value.value is not None else VARIANT
        return Attributed(_ds_type(inner_t), Attributes(value.attributes, types=getattr(
            value.attributes, "types", {}))), value.value
    return _ds_type(dtype or infer_type(value)), value

PORT = 3015
_HELLO = Cluster(I32, I32)
_OPEN = Cluster(I32, STRING, I32)
_CLOSE = Cluster(I32, STRING)
_BYE = Cluster(I32)


class DataSocketItemBusy(TimeoutError):
    """Write-mode open got no reply: the server allows one writer per item and stays
    silent (no error message) while another client holds it. Seen live with LabVIEW."""


def _msg(t: DataType, v) -> bytes:
    return VARIANT.pack(VariantValue(t, v))


def _item_name(url: str) -> str:
    """'dstp://localhost/wave1' -> 'wave1' (the server only ever sees the item name)."""
    if url.startswith("dstp://"):
        url = url[len("dstp://"):].split("/", 1)[1] if "/" in url[len("dstp://"):] else ""
    return url.strip("/")


class DataSocketClient:
    def __init__(self, host: str = "127.0.0.1", port: int = PORT, timeout: float = 5.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._lock = threading.Lock()
        self._buf = bytearray()
        self.timeout = timeout
        self._seq = itertools.count(1)
        self._acks: "dict[int, threading.Event]" = {}
        self._opened: "dict[str, threading.Event]" = {}
        self._values: "dict[str, queue.Queue]" = {}
        self.latest: "dict[str, Any]" = {}
        self.other: "queue.Queue" = queue.Queue()      # undecoded / unknown messages
        self._send(_HELLO, (1, 3))
        self._send(_HELLO, (2, 3))
        t, v = self._read_one()
        if v != (9, 3):
            raise IOError(f"unexpected DataSocket hello reply: {v!r}")
        self._closed = False
        self.closed = threading.Event()   # set when the connection is gone
        threading.Thread(target=self._rx, name="datasocket-rx", daemon=True).start()

    # ---- public API ----
    MODE_WRITE, MODE_READ = 4, 7     # values LabVIEW's DataSocket Open sends

    def open(self, url: str, mode: int = 7) -> str:
        """Open an item (mode 7 = LabVIEW 'Read', receives pushed values; 4 = 'Write')."""
        item = _item_name(url)
        ev = self._opened.setdefault(item, threading.Event())
        self._values.setdefault(item, queue.Queue())
        self._send(_OPEN, (4, item, mode))
        if not ev.wait(self.timeout):
            self._opened.pop(item, None)
            if mode == self.MODE_WRITE:
                raise DataSocketItemBusy(
                    f"DataSocket server did not answer the write-mode open of {item!r}; "
                    f"another client (e.g. a LabVIEW writer) probably has it open for writing. "
                    f"Stop that writer, or open with mode=MODE_READ to read it.")
            raise TimeoutError(f"server did not acknowledge open of {item!r}")
        return item

    def write(self, url: str, value, dtype: Optional[DataType] = None, wait_ack: bool = True):
        """Write a value (type inferred for float/str/list of str, else pass dtype)."""
        item = _item_name(url)
        t, value = _write_type(value, dtype)
        seq = next(self._seq)
        ev = self._acks[seq] = threading.Event()
        self._send(Cluster(I32, STRING, t, U32), (13, item, value, seq))
        if wait_ack and not ev.wait(self.timeout):
            raise TimeoutError(f"no write acknowledgement for seq {seq}")

    def read(self, url: str, timeout: Optional[float] = None):
        """Next value pushed by the server for this item (open() it first)."""
        try:
            return self._values[_item_name(url)].get(timeout=timeout or self.timeout)
        except queue.Empty:
            raise TimeoutError("no new value within timeout") from None

    def drain(self, url: str) -> list:
        """Every value pushed for this item since the last read/drain, oldest first,
        without waiting. Raises ConnectionError once the connection is gone and
        nothing is left."""
        q, out = self._values[_item_name(url)], []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                break
        if not out and self.closed.is_set():
            raise ConnectionError("DataSocket connection closed")
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            for item in list(self._opened):
                self._send(_CLOSE, (5, item))
            self._send(_BYE, (3,))
        except OSError:
            pass
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- internals ----
    def _send(self, t: DataType, v) -> None:
        data = _msg(t, v)
        with self._lock:
            self._sock.sendall(data)

    def _read_one(self):
        while True:
            if len(self._buf) >= 4:
                n = struct.unpack_from("<I", self._buf, 0)[0]
                if len(self._buf) >= n:
                    b = bytes(self._buf[:n]); del self._buf[:n]
                    try:
                        t, p = parse_td_body(b, 4)
                        t = _ds_type(t)
                        v = t.unpack(b, p)[0]
                    except NotImplementedError:
                        return None, b
                    # value message (6, item, value): give back attributes LabVIEW put
                    # in the value's descriptor as an LVVariant
                    if isinstance(t, Cluster) and len(t.fields) == 3 and v and v[0] == 6:
                        attrs = _user_attrs(t.fields[2])
                        if attrs is not None:
                            v = (v[0], v[1], LVVariant(v[2], attrs))
                    return t, v
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("DataSocket server closed the connection")
            self._buf += chunk

    def _rx(self) -> None:
        self._sock.settimeout(None)
        try:
            while True:
                t, v = self._read_one()
                op = v[0] if t is not None and isinstance(v, tuple) and v else None
                if op == 12:
                    self._opened.setdefault(v[1], threading.Event()).set()
                elif op == 14:
                    ev = self._acks.pop(v[1], None)
                    if ev:
                        ev.set()
                elif op == 6:
                    self.latest[v[1]] = v[2]
                    self._values.setdefault(v[1], queue.Queue()).put(v[2])
                elif op == 56:
                    pass
                else:
                    self.other.put((t, v))
        except (OSError, ConnectionError):
            pass
        finally:
            self.closed.set()      # connection lost or closed: no more values will arrive
