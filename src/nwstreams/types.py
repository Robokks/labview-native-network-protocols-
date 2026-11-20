"""Type descriptors and (un)flattening for Network Streams data.

Derived from captures of the NI 'Simple Network Streams' example with its data stream
changed to: DBL, 1D DBL array, 1D string array, variant, and a 9-field cluster
(DBL, Boolean, DBL[], String[], Boolean[], error cluster, Path, DBL[,], Variant), and
from 'TypeTest Variant Writer.vi' (LabVIEW 2026), which sends one value of each numeric,
complex, timestamp and waveform type inside a variant.

Descriptor (TD) := u32 len (incl. itself) + body
  Scalar codes are (kind, size class); size class n = 2**(n-1) bytes:
            size 1   size 2   size 4   size 8   size 16
    kind 00  I8 0001  I16 0002 I32 0003 I64 0004 EXT 0005
    kind 01  U8 0101  U16 0102 U32 0103 U64 0104 CDB 0105
    kind 02  Bool0201          SGL 0203 DBL 0204
    kind 03                             CSG 0304
    kind 04                                      Timestamp 0405
  Enums are sent as their plain integer type (U16 enum -> 0102); item names are dropped.
  String 00 09 | Path 05 09 | Variant 02 09
  array   00 08 u16 ndims  TD(element)
  cluster 01 08 u16 nfields TD(field)...
  attributed type: one or more attribute blocks, then the TD body it applies to:
    attribute block := 02 40 u32 len(incl. itself) + name block + value TD body + value data
    name block      := 03 40 u32 len(incl. itself) + 00 09 + string data
  Waveform DBL = attributed cluster{Timestamp t0, DBL dt, DBL[] Y, error, Variant attrs}
                 with the single type attribute _NI_MDT = cluster{String, I16} ("MDT", 3)

LINK_CREATE tail := u32 len(TD) + TD + u32 buf + u32 buf + u32 role (1 reader, 2 writer)
                    + flag + 01 32 [+ 02 96 03 af when flag == 03]
  flag 03 = top-level fixed-size numeric (DBL, 1D DBL array), 01 otherwise.

Element data (after the 12-byte handles/count header of a DATA frame). Every item has
an even size, so multi-byte values always fall on even offsets:
  top-level DBL     : raw 8 bytes, no length prefix
  anything else     : u32 total length (incl. itself) + value
  top-level 1D DBL[]: raw elements, count implied by the length
  nested array      : u32 per dimension + elements
  numbers           : little-endian; a 1-byte number is padded to 2 bytes
  EXT               : IEEE 754 binary128, BIG-endian
  complex           : real, imaginary
  timestamp         : u64 fraction (2**-64 s) + i64 seconds since 1904-01-01 UTC
  Boolean           : 2 bytes (ff 00 = true); in arrays 1 byte each, array padded to even
  string            : u32 n + bytes, padded to even length
  path              : u16 type + u16 count + count × (u8 n + bytes), padded to even
  variant           : u32 len (incl. itself) + attribute blocks + TD body of contents + value;
                      no contents = 05 03 00 00 00 00

Not verified yet (Phase 2): the new numeric types as a stream's OWN top-level type (link
flag and raw form), and arrays of 1-byte numbers (assumed packed like Boolean arrays).
Unknown descriptor codes raise NotImplementedError rather than guessing.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Optional, Sequence


def _pad(n: int) -> bytes:
    return b"\x00" * (n % 2)


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


class DataType:
    fixed_size = False   # True -> numeric fast path at top level (flag 03)

    def td_body(self) -> bytes:
        raise NotImplementedError

    def td(self) -> bytes:
        b = self.td_body()
        return _u32(4 + len(b)) + b

    def pack(self, v: Any) -> bytes:
        raise NotImplementedError

    def unpack(self, b: bytes, o: int) -> tuple[Any, int]:
        raise NotImplementedError


# ---- scalars ----

@dataclass(frozen=True)
class _Num(DataType):
    """Integer or SGL/DBL. A 1-byte scalar is padded to 2 bytes."""
    name: str
    code: bytes
    fmt: str
    fixed_size = True

    @property
    def size(self) -> int:
        return struct.calcsize("<" + self.fmt)

    def td_body(self):
        return self.code

    def pack(self, v):
        return struct.pack("<" + self.fmt, v) + _pad(self.size)

    def unpack(self, b, o):
        n = self.size
        return struct.unpack_from("<" + self.fmt, b, o)[0], o + n + n % 2

    def __repr__(self):
        return self.name


I8 = _Num("I8", b"\x00\x01", "b")
I16 = _Num("I16", b"\x00\x02", "h")
I32 = _Num("I32", b"\x00\x03", "i")
I64 = _Num("I64", b"\x00\x04", "q")
U8 = _Num("U8", b"\x01\x01", "B")
U16 = _Num("U16", b"\x01\x02", "H")
U32 = _Num("U32", b"\x01\x03", "I")
U64 = _Num("U64", b"\x01\x04", "Q")
SGL = _Num("SGL", b"\x02\x03", "f")
DBL = _Num("DBL", b"\x02\x04", "d")


@dataclass(frozen=True)
class _Complex(DataType):
    """CSG / CDB: real then imaginary part, little-endian. Value: Python complex."""
    name: str
    code: bytes
    fmt: str
    fixed_size = True

    def td_body(self):
        return self.code

    def pack(self, v):
        v = complex(v)
        return struct.pack("<" + self.fmt * 2, v.real, v.imag)

    def unpack(self, b, o):
        re, im = struct.unpack_from("<" + self.fmt * 2, b, o)
        return complex(re, im), o + 2 * struct.calcsize(self.fmt)

    def __repr__(self):
        return self.name


CSG = _Complex("CSG", b"\x03\x04", "f")
CDB = _Complex("CDB", b"\x01\x05", "d")


class _Ext(DataType):
    """EXT: IEEE 754 binary128 (quad precision), BIG-endian - unlike everything else.
    Value: Python float (rounded to double precision when read)."""
    fixed_size = True

    def td_body(self):
        return b"\x00\x05"

    def pack(self, v):
        x = float(v)
        sign = 1 if math.copysign(1.0, x) < 0 else 0
        if math.isnan(x):
            exp, mant = 0x7FFF, 1 << 111
        elif math.isinf(x):
            exp, mant = 0x7FFF, 0
        elif x == 0:
            exp, mant = 0, 0
        else:
            m, e = math.frexp(abs(x))                 # |x| = m * 2**e, 0.5 <= m < 1
            exp, mant = e - 1 + 16383, int(m * 2 ** 113) - (1 << 112)
        return ((sign << 127) | (exp << 112) | mant).to_bytes(16, "big")

    def unpack(self, b, o):
        n = int.from_bytes(b[o:o + 16], "big")
        sign, exp, mant = n >> 127, (n >> 112) & 0x7FFF, n & ((1 << 112) - 1)
        if exp == 0x7FFF:
            x = math.nan if mant else math.inf
        else:
            q = Fraction(mant, 1 << (16382 + 112)) if exp == 0 else \
                Fraction(mant | (1 << 112)) * Fraction(2) ** (exp - 16383 - 112)
            try:
                x = float(q)
            except OverflowError:
                x = math.inf
        return (-x if sign else x), o + 16

    def __repr__(self):
        return "EXT"


EXT = _Ext()


@dataclass(frozen=True)
class Timestamp:
    """LabVIEW timestamp: whole seconds since 1904-01-01 UTC + fraction in 2**-64 s."""
    seconds: int
    fraction: int = 0

    LV_TO_UNIX = 2082844800    # seconds from 1904-01-01 to 1970-01-01

    @property
    def lv(self) -> float:
        """Seconds since the LabVIEW epoch (1904-01-01 UTC)."""
        return self.seconds + self.fraction / 2 ** 64

    @property
    def unix(self) -> float:
        """Seconds since the Unix epoch (1970-01-01 UTC)."""
        return (self.seconds - self.LV_TO_UNIX) + self.fraction / 2 ** 64

    @classmethod
    def from_lv(cls, t: float) -> "Timestamp":
        secs = math.floor(t)
        frac = int(round((t - secs) * 2 ** 64))
        return cls(secs + frac // 2 ** 64, frac % 2 ** 64)

    @classmethod
    def from_unix(cls, t: float) -> "Timestamp":
        ts = cls.from_lv(t)
        return cls(ts.seconds + cls.LV_TO_UNIX, ts.fraction)


class _Timestamp(DataType):
    """Value: Timestamp (a float is taken as seconds since 1904)."""
    fixed_size = True

    def td_body(self):
        return b"\x04\x05"

    def pack(self, v):
        if not isinstance(v, Timestamp):
            v = Timestamp.from_lv(float(v))
        return struct.pack("<Qq", v.fraction, v.seconds)

    def unpack(self, b, o):
        frac, secs = struct.unpack_from("<Qq", b, o)
        return Timestamp(secs, frac), o + 16

    def __repr__(self):
        return "TIMESTAMP"


TIMESTAMP = _Timestamp()


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
        return _u32(len(s)) + s + _pad(len(s))

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


# ---- variants and attributes ----

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


class Attributes(dict):
    """Attribute name -> value, in wire order. `.types` remembers each value's LabVIEW
    type so that re-encoding is byte-exact; values without an entry are inferred."""

    def __init__(self, *a, types: Optional[dict] = None, **kw):
        super().__init__(*a, **kw)
        self.types = dict(types or {})


@dataclass
class LVVariant:
    """A variant that carries attributes (Set Variant Attribute / waveform attributes).
    A variant without attributes decodes to its plain value instead."""
    value: Any = None
    attributes: Attributes = field(default_factory=Attributes)


_EMPTY_VARIANT = b"\x05\x03\x00\x00\x00\x00"
_ATTR, _ATTR_NAME = b"\x02\x40", b"\x03\x40"


def _typed(v) -> tuple[DataType, Any]:
    if isinstance(v, VariantValue):
        return v.dtype, v.value
    return infer_type(v), v


def _pack_contents(t: Optional[DataType], v) -> bytes:
    """TD body + data, as inside a variant or an attribute value."""
    if isinstance(v, RawVariant):
        return v.code + v.data
    if t is None:
        t, v = _typed(v)
    return t.td_body() + t.pack(v)


def _pack_attrs(attrs: dict) -> bytes:
    types = getattr(attrs, "types", {})
    out = b""
    for name, v in attrs.items():
        name_part = b"\x00\x09" + STRING.pack(name)
        blk = _ATTR_NAME + _u32(4 + len(name_part)) + name_part + _pack_contents(types.get(name), v)
        out += _ATTR + _u32(4 + len(blk)) + blk
    return out


def _unpack_contents(b: bytes, p: int, end: int):
    """(type or None, value) of TD body + data in b[p:end]; unknown -> RawVariant."""
    try:
        t, q = parse_td_body(b, p)
        return t, t.unpack(b[:end], q)[0]
    except (NotImplementedError, struct.error, IndexError):
        return None, RawVariant(bytes(b[p:p + 2]), bytes(b[p + 2:end]))


def _unpack_attrs(b: bytes, p: int) -> tuple[Attributes, int]:
    attrs = Attributes()
    while bytes(b[p:p + 2]) == _ATTR:
        end = p + 2 + struct.unpack_from("<I", b, p + 2)[0]
        q = p + 6
        if bytes(b[q:q + 2]) != _ATTR_NAME:
            raise NotImplementedError(f"attribute block without a name at offset {q}")
        name_end = q + 2 + struct.unpack_from("<I", b, q + 2)[0]
        name = STRING.unpack(b, q + 8)[0]          # skip 03 40, u32 len, 00 09
        t, v = _unpack_contents(b, name_end, end)
        attrs[name] = v
        if t is not None:
            attrs.types[name] = t
        p = end
    return attrs, p


class _Variant(DataType):
    """Variant. Value: plain value, None (empty), LVVariant (with attributes), or
    RawVariant for contents of a type not decoded yet."""

    def td_body(self):
        return b"\x02\x09"

    def pack(self, v):
        if isinstance(v, LVVariant):
            inner = _pack_attrs(v.attributes) + (_EMPTY_VARIANT if v.value is None
                                                 else _pack_contents(None, v.value))
        elif v is None:
            inner = _EMPTY_VARIANT
        else:
            inner = _pack_contents(None, v)
        return _u32(4 + len(inner)) + inner

    def unpack(self, b, o):
        n = struct.unpack_from("<I", b, o)[0]
        end = o + n
        # Leading 02 40 blocks are either the contents' own type attributes (a waveform's
        # _NI_MDT) or variant attributes. NI type attributes are named _NI_*.
        if bytes(b[o + 4:o + 6]) == _ATTR:
            try:
                t, q = parse_td_body(b, o + 4)
                if isinstance(t, Attributed) and all(k.startswith("_NI_") for k in t.attributes):
                    val, e = t.unpack(b[:end], q)
                    if e == end:
                        return val, end
            except (NotImplementedError, struct.error, IndexError):
                pass
        attrs, p = _unpack_attrs(b, o + 4)
        if bytes(b[p:end]) == _EMPTY_VARIANT:
            val = None
        else:
            val = _unpack_contents(b, p, end)[1]
        return (LVVariant(val, attrs) if attrs else val), end

    def __repr__(self):
        return "VARIANT"


VARIANT = _Variant()


def infer_type(v) -> DataType:
    if isinstance(v, bool):
        return BOOLEAN
    if isinstance(v, int):
        return I32 if -2 ** 31 <= v < 2 ** 31 else I64
    if isinstance(v, float):
        return DBL
    if isinstance(v, complex):
        return CDB
    if isinstance(v, str):
        return STRING
    if isinstance(v, Timestamp):
        return TIMESTAMP
    if isinstance(v, Waveform):
        return WAVEFORM
    if isinstance(v, (list, tuple)) and v and all(isinstance(x, str) for x in v):
        return Array(STRING)
    raise TypeError(f"cannot infer LabVIEW type for {type(v).__name__}; use VariantValue")


# ---- containers ----

def _packed(element: DataType) -> bool:
    """Array elements stored 1 byte each (array padded to even): Boolean, I8, U8."""
    return element is BOOLEAN or (isinstance(element, _Num) and element.size == 1)


@dataclass(frozen=True)
class Array(DataType):
    element: DataType
    ndims: int = 1

    @property
    def fixed_size(self):  # top-level 1D numeric arrays use the raw fast path
        # 1-byte elements are excluded until verified: in the raw form an odd count's
        # pad byte could not be told apart from an element.
        return self.ndims == 1 and isinstance(self.element, _Num) and not _packed(self.element)

    def td_body(self):
        return b"\x00\x08" + struct.pack("<H", self.ndims) + self.element.td()

    def _pack_flat(self, flat):
        if self.element is BOOLEAN:
            return bytes(0xFF if x else 0 for x in flat) + _pad(len(flat))
        if _packed(self.element):
            return struct.pack(f"<{len(flat)}{self.element.fmt}", *flat) + _pad(len(flat))
        return b"".join(self.element.pack(x) for x in flat)

    def _unpack_flat(self, b, o, total):
        if self.element is BOOLEAN:
            return [x != 0 for x in b[o:o + total]], o + total + total % 2
        if _packed(self.element):
            return list(struct.unpack_from(f"<{total}{self.element.fmt}", b, o)), o + total + total % 2
        flat = []
        for _ in range(total):
            x, o = self.element.unpack(b, o)
            flat.append(x)
        return flat, o

    # nested form: dimension sizes + elements (values: nested lists for ndims > 1)
    def pack(self, v: Sequence):
        dims, flat = _shape(v, self.ndims)
        return struct.pack(f"<{self.ndims}I", *dims) + self._pack_flat(flat)

    def unpack(self, b, o):
        dims = struct.unpack_from(f"<{self.ndims}I", b, o)
        o += 4 * self.ndims
        total = 1
        for d in dims:
            total *= d
        flat, o = self._unpack_flat(b, o, total)
        return _reshape(flat, dims), o

    # top-level raw form (fixed_size only)
    def pack_raw(self, v):
        return self._pack_flat(list(v))

    def unpack_raw(self, b, o, end):
        if _packed(self.element):
            return self._unpack_flat(b, o, end - o)[0]
        out = []
        while o < end:
            x, o = self.element.unpack(b, o)
            out.append(x)
        return out

    def __repr__(self):
        return f"Array({self.element!r}{', ' + str(self.ndims) if self.ndims != 1 else ''})"


@dataclass(frozen=True)
class ColumnMajorArray(Array):
    """Array whose elements are stored column-major (first index varies fastest), as the
    DataSocket Server does for 2-D and larger arrays (seen live with LabVIEW 2026).
    Values are still nested lists indexed [row][column]."""

    def pack(self, v: Sequence):
        if self.ndims == 1:
            return super().pack(v)
        dims, flat = _shape(v, self.ndims)
        return struct.pack(f"<{self.ndims}I", *dims) + self._pack_flat(_reorder(flat, dims, to_f=True))

    def unpack(self, b, o):
        if self.ndims == 1:
            return super().unpack(b, o)
        dims = struct.unpack_from(f"<{self.ndims}I", b, o)
        o += 4 * self.ndims
        total = 1
        for d in dims:
            total *= d
        flat, o = self._unpack_flat(b, o, total)
        return _reshape(_reorder(flat, dims, to_f=False), dims), o


def _reorder(flat, dims, to_f):
    """Row-major <-> column-major order of a flat element list."""
    n = len(dims)
    c_strides, f_strides = [1] * n, [1] * n
    for k in range(n - 2, -1, -1):
        c_strides[k] = c_strides[k + 1] * dims[k + 1]
    for k in range(1, n):
        f_strides[k] = f_strides[k - 1] * dims[k - 1]
    out = [None] * len(flat)
    for c_index in range(len(flat)):
        rest, f_index = c_index, 0
        for k in range(n):
            i, rest = divmod(rest, c_strides[k])
            f_index += i * f_strides[k]
        if to_f:
            out[f_index] = flat[c_index]
        else:
            out[c_index] = flat[f_index]
    return out


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


class Attributed(DataType):
    """A type carrying type-level attributes in its descriptor (02 40 blocks).
    Values are those of the inner type."""

    def __init__(self, inner: DataType, attributes: Attributes):
        self.inner, self.attributes = inner, attributes

    @property
    def fixed_size(self):
        return False

    def td_body(self):
        return _pack_attrs(self.attributes) + self.inner.td_body()

    def pack(self, v):
        return self.inner.pack(v)

    def unpack(self, b, o):
        return self.inner.unpack(b, o)

    def __eq__(self, other):
        return isinstance(other, DataType) and other.td_body() == self.td_body()

    def __hash__(self):
        return hash(self.td_body())

    def __repr__(self):
        return f"Attributed({self.inner!r}, {dict(self.attributes)!r})"


# ---- waveform ----

@dataclass
class Waveform:
    t0: float          # seconds since the LabVIEW epoch (1904-01-01 UTC)
    dt: float
    y: tuple[float, ...]
    attributes_raw: bytes = b""        # WAVEFORM_ARRAY template codec only
    attributes: Attributes = field(default_factory=Attributes)
    error: tuple = (False, 0, "")
    timestamp: Optional[Timestamp] = None   # exact t0 when decoded; overrides t0 when set

    @property
    def t0_datetime(self):
        from datetime import datetime, timedelta, timezone
        return datetime(1904, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.t0)

    @property
    def t0_unix(self) -> float:
        return (self.timestamp or Timestamp.from_lv(self.t0)).unix


_WFM_CLUSTER = Cluster(TIMESTAMP, DBL, Array(DBL), ERROR_CLUSTER, VARIANT)
_WFM_MDT_TYPE = Cluster(STRING, I16)
_WFM_ATTRS = Attributes({"_NI_MDT": ("MDT", 3)}, types={"_NI_MDT": _WFM_MDT_TYPE})


class _WaveformDBL(Attributed):
    """Waveform of DBL. Value: Waveform (t0, dt, y, attributes, error)."""

    def __init__(self):
        super().__init__(_WFM_CLUSTER, _WFM_ATTRS)

    def pack(self, w: Waveform):
        ts = w.timestamp or Timestamp.from_lv(w.t0)
        attrs = LVVariant(None, w.attributes) if w.attributes else None
        return _WFM_CLUSTER.pack((ts, w.dt, list(w.y), w.error, attrs))

    def unpack(self, b, o):
        (ts, dt, y, err, var), o = _WFM_CLUSTER.unpack(b, o)
        attrs = var.attributes if isinstance(var, LVVariant) else Attributes()
        return Waveform(ts.lv, dt, tuple(y), attributes=attrs, error=err, timestamp=ts), o

    def __repr__(self):
        return "WAVEFORM"


WAVEFORM = _WaveformDBL()


# ---- descriptor parsing ----

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


_CODES = {t.td_body(): t for t in (I8, I16, I32, I64, U8, U16, U32, U64, SGL, DBL, EXT,
                                    CSG, CDB, TIMESTAMP, BOOLEAN, STRING, PATH, VARIANT)}


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
    if code == _ATTR:
        attrs, p = _unpack_attrs(b, o)
        inner, p = parse_td_body(b, p)
        t = Attributed(inner, attrs)
        return (WAVEFORM if t == WAVEFORM else t), p
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
    return (_u32(len(td)) + td + struct.pack("<III", buffer_size, buffer_size, role)
            + bytes([flag, 0x01, 0x32]) + (_FIXED_EXTRA if flag == 3 else b""))


def pack_element(t: DataType, v) -> bytes:
    if t.fixed_size and not isinstance(t, Array):
        if isinstance(t, _Num):     # top level: raw, a 1-byte number is NOT padded (seen live)
            return struct.pack("<" + t.fmt, v)
        return t.pack(v)
    body = t.pack_raw(v) if isinstance(t, Array) and t.fixed_size else t.pack(v)
    return _u32(4 + len(body)) + body


def unpack_element(t: DataType, b: bytes):
    if t.fixed_size and not isinstance(t, Array):
        if isinstance(t, _Num):
            return struct.unpack_from("<" + t.fmt, b, 0)[0]
        return t.unpack(b, 0)[0]
    n = struct.unpack_from("<I", b, 0)[0]
    if isinstance(t, Array) and t.fixed_size:
        return t.unpack_raw(b, 4, n)
    return t.unpack(b[:n], 4)[0]


MESSAGE = Cluster(STRING, VARIANT)   # the example's command type {Message, Message Data}
