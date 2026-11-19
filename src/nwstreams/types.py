"""Type descriptors and (un)flattening for Network Streams data.

Derived from captures of the NI 'Simple Network Streams' example with its data stream
changed to: DBL, 1D DBL array, 1D string array, variant, and a 9-field cluster
(DBL, Boolean, DBL[], String[], Boolean[], error cluster, Path, DBL[,], Variant).

Descriptor (TD) := u32 len (incl. itself) + body
  DBL 02 04 | I32 00 03 | Boolean 02 01 | String 00 09 | Path 05 09 | Variant 02 09
  array   00 08 u16 ndims  TD(element)
  cluster 01 08 u16 nfields TD(field)...

LINK_CREATE tail := u32 len(TD) + TD + u32 buf + u32 buf + u32 role (1 reader, 2 writer)
                    + flag + 01 32 [+ 02 96 03 af when flag == 03]
  flag 03 = top-level fixed-size numeric (DBL, 1D DBL array), 01 otherwise.

Element data (after the 12-byte handles/count header of a DATA frame). Every item has
an even size, so multi-byte values always fall on even offsets:
  top-level DBL     : raw 8 bytes, no length prefix
  anything else     : u32 total length (incl. itself) + value
  top-level 1D DBL[]: raw elements, count implied by the length
  nested array      : u32 per dimension + elements
  Boolean           : 2 bytes (ff 00 = true); in arrays 1 byte each, array padded to even
  string            : u32 n + bytes, padded to even length
  path              : u16 type + u16 count + count × (u8 n + bytes), padded to even
  variant           : u32 len (incl. itself) + TD body of contents + value;
                      empty variant = 05 03 00 00 00 00
Unverified types raise NotImplementedError rather than guessing.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Sequence


def _pad(n: int) -> bytes:
    return b"\x00" * (n % 2)


class DataType:
    fixed_size = False   # True -> numeric fast path at top level (flag 03)

    def td_body(self) -> bytes:
        raise NotImplementedError

    def td(self) -> bytes:
        b = self.td_body()
        return struct.pack("<I", 4 + len(b)) + b

    def pack(self, v: Any) -> bytes:
        raise NotImplementedError

    def unpack(self, b: bytes, o: int) -> tuple[Any, int]:
        raise NotImplementedError


@dataclass(frozen=True)
class _Num(DataType):
    name: str
    code: bytes
    fmt: str
    fixed_size = True

    def td_body(self):
        return self.code

    def pack(self, v):
        return struct.pack("<" + self.fmt, v)

    def unpack(self, b, o):
        return struct.unpack_from("<" + self.fmt, b, o)[0], o + struct.calcsize(self.fmt)

    def __repr__(self):
        return self.name


DBL = _Num("DBL", b"\x02\x04", "d")
I32 = _Num("I32", b"\x00\x03", "i")   # verified inside clusters (error code, DataSocket)
U32 = _Num("U32", b"\x01\x03", "I")   # verified inside DataSocket messages (sequence numbers)


class _Bool(DataType):
    def td_body(self):
        return b"\x02\x01"

    def pack(self, v):
        return b"\xff\x00" if v else b"\x00\x00"

    def unpack(self, b, o):
        return b[o] != 0, o + 2

    def __repr__(self):
        return "BOOLEAN"


BOOLEAN = _Bool()


class _String(DataType):
    def td_body(self):
        return b"\x00\x09"

    def pack(self, v):
        s = v.encode("latin-1") if isinstance(v, str) else bytes(v)
        return struct.pack("<I", len(s)) + s + _pad(len(s))

    def unpack(self, b, o):
        n = struct.unpack_from("<I", b, o)[0]
        return b[o + 4:o + 4 + n].decode("latin-1"), o + 4 + n + n % 2

    def __repr__(self):
        return "STRING"


STRING = _String()


@dataclass(frozen=True)
class LVPath:
    parts: tuple = ()
    kind: int = 0          # 0 = absolute (only value seen so far)

    def __str__(self):
        if not self.parts:
            return ""
        head, *rest = self.parts
        return "\\".join([head + ":" if len(head) == 1 else head, *rest])


class _Path(DataType):
    def td_body(self):
        return b"\x05\x09"

    def pack(self, v):
        if isinstance(v, str):
            v = LVPath(tuple(p.rstrip(":") for p in v.replace("/", "\\").split("\\") if p))
        body = struct.pack("<HH", v.kind, len(v.parts))
        for p in v.parts:
            e = p.encode("latin-1")
            body += bytes([len(e)]) + e
        return body + _pad(len(body))

    def unpack(self, b, o):
        kind, cnt = struct.unpack_from("<HH", b, o)
        p, parts = o + 4, []
        for _ in range(cnt):
            n = b[p]
            parts.append(b[p + 1:p + 1 + n].decode("latin-1"))
            p += 1 + n
        return LVPath(tuple(parts), kind), p + (p - o) % 2

    def __repr__(self):
        return "PATH"


PATH = _Path()


@dataclass(frozen=True)
class RawVariant:
    """Variant contents of a type not decoded yet (kept byte-exact)."""
    code: bytes
    data: bytes


@dataclass(frozen=True)
class VariantValue:
    """Explicitly typed value to put in a variant, e.g. VariantValue(Array(STRING), [...])."""
    dtype: DataType
    value: Any


_EMPTY_VARIANT = b"\x05\x03\x00\x00\x00\x00"


class _Variant(DataType):
    """Variant. Verified contents: empty, DBL, 1D string array. Unknown -> RawVariant."""

    def td_body(self):
        return b"\x02\x09"

    def pack(self, v):
        if v is None:
            inner = _EMPTY_VARIANT
        elif isinstance(v, RawVariant):
            inner = v.code + v.data
        else:
            t = v.dtype if isinstance(v, VariantValue) else infer_type(v)
            val = v.value if isinstance(v, VariantValue) else v
            inner = t.td_body() + t.pack(val)
        return struct.pack("<I", 4 + len(inner)) + inner

    def unpack(self, b, o):
        n = struct.unpack_from("<I", b, o)[0]
        inner = b[o + 4:o + n]
        if inner == _EMPTY_VARIANT:
            return None, o + n
        try:
            t, p = parse_td_body(b, o + 4)
            val = t.unpack(b, p)[0]
        except NotImplementedError:
            val = RawVariant(inner[:2], inner[2:])
        return val, o + n

    def __repr__(self):
        return "VARIANT"


VARIANT = _Variant()


def infer_type(v) -> DataType:
    if isinstance(v, bool):
        return BOOLEAN
    if isinstance(v, float):
        return DBL
    if isinstance(v, str):
        return STRING
    if isinstance(v, (list, tuple)) and v and all(isinstance(x, str) for x in v):
        return Array(STRING)
    raise TypeError(f"cannot infer LabVIEW type for {type(v).__name__}; use VariantValue")


@dataclass(frozen=True)
class Array(DataType):
    element: DataType
    ndims: int = 1

    @property
    def fixed_size(self):  # top-level 1D numeric arrays use the raw fast path
        return self.ndims == 1 and isinstance(self.element, _Num)

    def td_body(self):
        return b"\x00\x08" + struct.pack("<H", self.ndims) + self.element.td()

    # nested form: dimension sizes + elements (values: nested lists for ndims > 1)
    def pack(self, v: Sequence):
        dims, flat = _shape(v, self.ndims)
        head = struct.pack(f"<{self.ndims}I", *dims)
        if self.element is BOOLEAN:   # packed: 1 byte per element, array padded to even
            return head + bytes(0xFF if x else 0 for x in flat) + _pad(len(flat))
        return head + b"".join(self.element.pack(x) for x in flat)

    def unpack(self, b, o):
        dims = struct.unpack_from(f"<{self.ndims}I", b, o)
        o += 4 * self.ndims
        total = 1
        for d in dims:
            total *= d
        if self.element is BOOLEAN:
            flat = [x != 0 for x in b[o:o + total]]
            return _reshape(flat, dims), o + total + total % 2
        flat = []
        for _ in range(total):
            x, o = self.element.unpack(b, o)
            flat.append(x)
        return _reshape(flat, dims), o

    # top-level raw form (fixed_size only)
    def pack_raw(self, v):
        return b"".join(self.element.pack(x) for x in v)

    def unpack_raw(self, b, o, end):
        out = []
        while o < end:
            x, o = self.element.unpack(b, o)
            out.append(x)
        return out

    def __repr__(self):
        return f"Array({self.element!r}{', ' + str(self.ndims) if self.ndims != 1 else ''})"


def _shape(v, ndims):
    dims, cur = [], v
    for _ in range(ndims):
        dims.append(len(cur))
        cur = cur[0] if len(cur) else []
    flat = list(v)
    for _ in range(ndims - 1):
        flat = [x for row in flat for x in row]
    return dims, flat


def _reshape(flat, dims):
    for d in reversed(dims[1:]):
        flat = [flat[i:i + d] for i in range(0, len(flat), d)] if d else []
    return flat


@dataclass(frozen=True)
class Cluster(DataType):
    fields: tuple = field(default=())

    def __init__(self, *fields: DataType):
        object.__setattr__(self, "fields", tuple(fields))

    def td_body(self):
        return b"\x01\x08" + struct.pack("<H", len(self.fields)) + b"".join(f.td() for f in self.fields)

    def pack(self, v):
        if len(v) != len(self.fields):
            raise ValueError(f"cluster needs {len(self.fields)} values, got {len(v)}")
        return b"".join(f.pack(x) for f, x in zip(self.fields, v))

    def unpack(self, b, o):
        out = []
        for f in self.fields:
            x, o = f.unpack(b, o)
            out.append(x)
        return tuple(out), o

    def __repr__(self):
        return f"Cluster({', '.join(map(repr, self.fields))})"


ERROR_CLUSTER = Cluster(BOOLEAN, I32, STRING)   # {status, code, source}


@dataclass(frozen=True)
class Opaque(DataType):
    """A descriptor copied from a capture (e.g. waveform array) with a custom decoder."""
    raw_td: bytes
    decoder: Any = None
    encoder: Any = None
    fixed_size = False

    def td(self):
        return self.raw_td

    def pack(self, v):
        if self.encoder is None:
            raise NotImplementedError("this opaque type has no encoder")
        return self.encoder(v)

    def unpack(self, b, o):
        return (self.decoder(b[o:]) if self.decoder else b[o:]), len(b)


_CODES = {b"\x02\x04": DBL, b"\x00\x03": I32, b"\x01\x03": U32, b"\x02\x01": BOOLEAN,
          b"\x00\x09": STRING, b"\x05\x09": PATH, b"\x02\x09": VARIANT}


def parse_td_body(b: bytes, o: int):
    """Parse a descriptor body (no length prefix) at offset o. Returns (type, end offset)."""
    code = bytes(b[o:o + 2])
    if code in _CODES:
        return _CODES[code], o + 2
    if code in (b"\x00\x08", b"\x01\x08"):
        cnt = struct.unpack_from("<H", b, o + 2)[0]
        p, subs = o + 4, []
        for _ in range(1 if code == b"\x00\x08" else cnt):
            ln = struct.unpack_from("<I", b, p)[0]
            subs.append(parse_td_body(b, p + 4)[0])
            p += ln
        return (Array(subs[0], cnt) if code == b"\x00\x08" else Cluster(*subs)), p
    raise NotImplementedError(f"descriptor code {code.hex()} not decoded yet")


def parse_td(b: bytes, o: int = 0) -> DataType:
    """Parse a length-prefixed descriptor (as carried in LINK_CREATE)."""
    return parse_td_body(b, o + 4)[0]


# ---- link and element encoding ----

ROLE_READER, ROLE_WRITER = 1, 2
_FIXED_EXTRA = bytes.fromhex("029603af")


def link_tail(t: DataType, buffer_size: int, role: int) -> bytes:
    td = t.td()
    flag = 3 if t.fixed_size else 1
    return (struct.pack("<I", len(td)) + td + struct.pack("<III", buffer_size, buffer_size, role)
            + bytes([flag, 0x01, 0x32]) + (_FIXED_EXTRA if flag == 3 else b""))


def pack_element(t: DataType, v) -> bytes:
    if t.fixed_size and not isinstance(t, Array):
        return t.pack(v)
    body = t.pack_raw(v) if isinstance(t, Array) and t.fixed_size else t.pack(v)
    return struct.pack("<I", 4 + len(body)) + body


def unpack_element(t: DataType, b: bytes):
    if t.fixed_size and not isinstance(t, Array):
        return t.unpack(b, 0)[0]
    n = struct.unpack_from("<I", b, 0)[0]
    if isinstance(t, Array) and t.fixed_size:
        return t.unpack_raw(b, 4, n)
    return t.unpack(b[:n], 4)[0]


MESSAGE = Cluster(STRING, VARIANT)   # the example's command type {Message, Message Data}
