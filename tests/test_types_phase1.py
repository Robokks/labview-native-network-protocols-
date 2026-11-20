"""Types learned from 'TypeTest Variant Writer.vi' (LabVIEW 2026, 2026-11-20).

The fixture holds the 20 raw stream elements LabVIEW sent, each a Variant holding one
known value. Every element must decode to that value and re-encode byte for byte.
"""
import json
import math
from pathlib import Path

import pytest

import nwstreams as nw
from nwstreams.framing import FrameBuffer, MsgType

FIX = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIX / "type_variant_probe.json").read_text())
T0 = nw.Timestamp(3850070400)          # 2026-01-01 00:00:00 UTC

EXPECTED = {
    1: (nw.I8, -5), 2: (nw.I16, -300), 3: (nw.I32, -100), 4: (nw.I64, -5000000000),
    5: (nw.U8, 250), 6: (nw.U16, 65000), 7: (nw.U32, 4000000000), 8: (nw.U64, 10000000000),
    9: (nw.SGL, 0.25), 10: (nw.DBL, 0.5), 11: (nw.EXT, 1.5),
    12: (nw.CSG, 1.5 + 2.5j), 13: (nw.CDB, 1.5 - 2.5j),
    14: (nw.U16, 2),                    # U16 enum {Off, Low, High} = High
    15: (nw.U8, 1),                     # U8 enum {A, B, C} = B
    16: (nw.TIMESTAMP, T0), 17: (nw.TIMESTAMP, nw.Timestamp(3850070400, 2 ** 63)),
    18: (nw.Array(nw.I16), [1, -2, 3]),
}


def element(i):
    return bytes.fromhex(ROWS[i - 1]["element"])


@pytest.mark.parametrize("i", sorted(EXPECTED))
def test_variant_scalar_decodes_and_reencodes(i):
    dtype, value = EXPECTED[i]
    raw = element(i)
    got = nw.unpack_element(nw.VARIANT, raw)
    assert got == value and type(got) is type(value)
    assert nw.pack_element(nw.VARIANT, nw.VariantValue(dtype, value)) == raw
    assert nw.parse_td(dtype.td()) == dtype


def test_timestamp_conversions():
    assert T0.unix == 1767225600.0      # 2026-01-01T00:00:00Z
    assert nw.Timestamp.from_unix(1767225600.5) == nw.Timestamp(3850070400, 2 ** 63)
    assert nw.Timestamp.from_lv(T0.lv) == T0


@pytest.mark.parametrize("x", [1.5, -2.25, 0.0, -0.0, 1e-300, 1e300, 5e-324, math.inf, -math.inf])
def test_ext_roundtrip(x):
    got = nw.EXT.unpack(nw.EXT.pack(x), 0)[0]
    assert got == x and math.copysign(1, got) == math.copysign(1, x)
    assert math.isnan(nw.EXT.unpack(nw.EXT.pack(math.nan), 0)[0])


def test_ext_matches_labview_bytes():
    assert nw.EXT.pack(1.5) == bytes.fromhex("3fff8000000000000000000000000000")


def test_waveform_without_attributes():
    raw = element(19)
    w = nw.unpack_element(nw.VARIANT, raw)
    assert isinstance(w, nw.Waveform)
    assert w.timestamp == T0 and w.dt == 0.001 and w.y == (1.0, 2.0, 3.0)
    assert w.attributes == {} and w.error == (False, 0, "")
    assert w.t0_unix == 1767225600.0
    assert nw.pack_element(nw.VARIANT, w) == raw
    fresh = nw.Waveform(T0.lv, 0.001, (1.0, 2.0, 3.0))     # built from scratch
    assert nw.pack_element(nw.VARIANT, fresh) == raw


def test_waveform_with_attribute():
    raw = element(20)
    w = nw.unpack_element(nw.VARIANT, raw)
    assert w.attributes == {"unit": "V"}
    assert nw.pack_element(nw.VARIANT, w) == raw
    fresh = nw.Waveform(T0.lv, 0.001, (1.0, 2.0, 3.0), attributes=nw.Attributes({"unit": "V"}))
    assert nw.pack_element(nw.VARIANT, fresh) == raw


def test_generic_waveform_array_matches_example_capture():
    """The Simple Network Streams example's waveforms, decoded generically: same values
    as the template codec, attributes parsed, and re-encoded byte for byte."""
    data = [f.payload[12:] for f in FrameBuffer().feed((FIX / "labview_to_client.bin").read_bytes())
            if f.type == MsgType.DATA]
    t = nw.Array(nw.WAVEFORM)
    for e in data:
        generic = nw.unpack_element(t, e)
        legacy = nw.unpack_element(nw.WAVEFORM_ARRAY, e)
        assert [(w.t0, w.dt, w.y) for w in generic] == [(w.t0, w.dt, w.y) for w in legacy]
        assert generic[0].attributes["NI_ChannelName"] == "Sine with Uniform Noise"
        assert generic[0].attributes["NI_ExpIsRelativeTime"] is True
        assert isinstance(generic[0].attributes["NI_ExpTimeStamp"], nw.Timestamp)
        assert nw.pack_element(t, generic) == e


def test_variant_attributes_roundtrip():
    v = nw.LVVariant(2.5, nw.Attributes({"a": "x", "n": 3, "on": True}))
    back = nw.VARIANT.unpack(nw.VARIANT.pack(v), 0)[0]
    assert back.value == 2.5 and back.attributes == {"a": "x", "n": 3, "on": True}


@pytest.mark.parametrize("dtype,values", [
    (nw.Array(nw.U8), [250, 1, 7]),
    (nw.Array(nw.I8), [-5, 4]),
    (nw.Array(nw.I64, 2), [[1, -2], [3, 2 ** 40]]),
    (nw.Array(nw.CDB), [1 + 2j]),
    (nw.Array(nw.TIMESTAMP), [T0]),
    (nw.Cluster(nw.U16, nw.SGL, nw.EXT, nw.TIMESTAMP), (7, 0.5, 2.0, T0)),
])
def test_container_roundtrip(dtype, values):
    assert nw.unpack_element(dtype, nw.pack_element(dtype, values)) == values
    assert nw.parse_td(dtype.td()) == dtype
