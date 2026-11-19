"""Payload codecs and type-descriptor templates.

The type descriptors (TD) are copied byte-for-byte from captures of the NI
'Simple Network Streams' example. They are NOT generated from LabVIEW types yet,
so only these exact data types are supported.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

LV_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)

# Descriptor + trailer that follow the two URLs in a LINK_CREATE message.
# The trailer embeds this side's buffer size; it is patched in by patch_buffer().
TD_WAVEFORM_ARRAY_DBL = bytes.fromhex(
    "7e0000007e00000000080100760000000240320000000340120000000009070000005f4e495f4d4454"
    "0001080200060000000009060000000002030000004d4454000300010805000600000004050600000002"
    "040e000000000801000600000002041a00000001080300060000000201060000000003060000000009"
    "060000000209640000006400000001000000010132")
TD_MESSAGE_CLUSTER = bytes.fromhex(  # cluster{string Message, variant Message Data}
    "1400000014000000010802000600000000090600000002090a0000000a00000002000000010132")


@dataclass
class Waveform:
    t0: float          # seconds since the LabVIEW epoch (1904-01-01 UTC)
    dt: float
    y: tuple[float, ...]
    attributes_raw: bytes = b""

    @property
    def t0_datetime(self) -> datetime:
        return LV_EPOCH + timedelta(seconds=self.t0)


def lv_timestamp(raw: bytes) -> float:
    frac, secs = struct.unpack("<Qq", raw)
    return secs + frac / 2**64


def decode_waveform_array(flat: bytes) -> list[Waveform]:
    """Flattened 1-D array of waveform(DBL), as sent by the example's /data/writer.

    The attribute variant isn't parsed: 274 bytes between waveforms, and the last
    one runs to the end. That holds for the example's waveforms only.
    """
    o = 0
    n = struct.unpack_from("<I", flat, o)[0]
    o += 4
    out = []
    for i in range(n):
        t0 = lv_timestamp(flat[o:o + 16]); o += 16
        dt = struct.unpack_from("<d", flat, o)[0]; o += 8
        ns = struct.unpack_from("<I", flat, o)[0]; o += 4
        y = struct.unpack_from(f"<{ns}d", flat, o); o += 8 * ns
        end = len(flat) if i == n - 1 else o + 274
        out.append(Waveform(t0, dt, y, flat[o:end]))
        o = end
    return out


_SETMSG_VARIANT_DBL = bytes.fromhex("000e0000000204")


def encode_message_dbl(message: str, value: float) -> bytes:
    """cluster{Message: string, Message Data: variant(DBL)} - e.g. 'Set Frequency'.
    Only variant-of-DBL is supported (layout taken from the capture)."""
    m = message.encode("latin-1")
    return (bytes.fromhex("24000000") + struct.pack("<I", len(m)) + m
            + _SETMSG_VARIANT_DBL + struct.pack("<d", value))


def decode_message_dbl(flat: bytes) -> tuple[str, float]:
    n = struct.unpack_from("<I", flat, 4)[0]
    msg = flat[8:8 + n].decode("latin-1")
    return msg, struct.unpack_from("<d", flat, 8 + n + len(_SETMSG_VARIANT_DBL))[0]


def _waveform_type():
    from .types import Opaque
    n = struct.unpack_from("<I", TD_WAVEFORM_ARRAY_DBL, 0)[0]
    return Opaque(TD_WAVEFORM_ARRAY_DBL[4:4 + n], decode_waveform_array, encode_waveform_array)


def _lv_timestamp_bytes(t: float) -> bytes:
    secs = int(t // 1)
    frac = int(round((t - secs) * 2**64)) & (2**64 - 1)
    return struct.pack("<Qq", frac, secs)


def encode_waveform_array(waveforms: "list[Waveform]") -> bytes:
    """Flatten waveforms for WAVEFORM_ARRAY (element body without the length prefix).

    Attribute blocks aren't generated: each waveform uses its own attributes_raw, or
    else the captured example templates (sine for even indexes, square for odd; the
    last block in the array is the 276-byte form).
    """
    from ._wfm_attr_templates import SINE, SQUARE
    out = [struct.pack("<I", len(waveforms))]
    for i, w in enumerate(waveforms):
        last = i == len(waveforms) - 1
        attrs = w.attributes_raw or (SQUARE if last else SINE[:274] if i % 2 == 0 else SQUARE[:274])
        if not w.attributes_raw and last and len(waveforms) == 1:
            attrs = SINE[:274] + SQUARE[274:]
        out += [_lv_timestamp_bytes(w.t0), struct.pack("<dI", w.dt, len(w.y)),
                struct.pack(f"<{len(w.y)}d", *w.y), attrs]
    return b"".join(out)


# 1-D array of waveform(DBL) as in the Simple Network Streams example (descriptor from capture)
WAVEFORM_ARRAY = _waveform_type()
