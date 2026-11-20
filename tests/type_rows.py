"""The 25 type rows used by the LabVIEW TypeTest VIs (DataSocket and Shared Variables)."""
import nwstreams as nw

T0 = nw.Timestamp(3850070400)          # 2026-01-01 00:00:00 UTC
ROWS = [
    ("i8", nw.I8, -5), ("i16", nw.I16, -300), ("i32", nw.I32, -100),
    ("i64", nw.I64, -5000000000), ("u8", nw.U8, 250), ("u16", nw.U16, 65000),
    ("u32", nw.U32, 4000000000), ("u64", nw.U64, 10000000000), ("sgl", nw.SGL, 0.25),
    ("dbl", nw.DBL, 0.5), ("ext", nw.EXT, 1.5), ("csg", nw.CSG, 1.5 + 2.5j),
    ("cdb", nw.CDB, 1.5 - 2.5j), ("bool", nw.BOOLEAN, True), ("str", nw.STRING, "Hello 1"),
    ("time", nw.TIMESTAMP, T0),
    ("wfm", nw.WAVEFORM, nw.Waveform(T0.lv, 0.001, (1.0, 2.0, 3.0))),
    ("u8arr", nw.Array(nw.U8), [250, 1, 7]), ("i16arr", nw.Array(nw.I16), [1, -2, 3]),
    ("dblarr", nw.Array(nw.DBL), [0.5, 1.5, 2.5]), ("strarr", nw.Array(nw.STRING), ["a", "bc"]),
    ("i32arr2d", nw.Array(nw.I32, 2), [[1, 2, 3], [4, 5, 6]]),
    ("timearr", nw.Array(nw.TIMESTAMP), [T0]),
    ("clust", nw.Cluster(nw.I32, nw.DBL, nw.STRING), (7, 1.5, "c7")),
    ("varattr", nw.VARIANT, nw.LVVariant(2.5, nw.Attributes({"unit": "V"}))),
]


def same(a, b):
    if isinstance(a, nw.Waveform) and isinstance(b, nw.Waveform):
        return (a.t0, a.dt, tuple(a.y), dict(a.attributes)) == (b.t0, b.dt, tuple(b.y), dict(b.attributes))
    if isinstance(a, nw.LVVariant) and isinstance(b, nw.LVVariant):
        return a.value == b.value and dict(a.attributes) == dict(b.attributes)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b
