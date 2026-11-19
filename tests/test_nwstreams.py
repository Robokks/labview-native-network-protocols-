import struct
from pathlib import Path

import pytest

import nwstreams as nw
from nwstreams.framing import Frame, FrameBuffer, MsgType
from nwstreams.locator import parse_listing_port

from fake_labview import FakeLabVIEW

FIX = Path(__file__).parent / "fixtures"


def frames(name):
    return FrameBuffer().feed((FIX / name).read_bytes())


def test_fixtures_parse_completely():
    for name in ("client_to_labview.bin", "labview_to_client.bin", "locator_reply.bin"):
        fb = FrameBuffer()
        assert fb.feed((FIX / name).read_bytes())
        assert fb.pending == 0


def test_frame_roundtrip_byte_exact():
    raw = (FIX / "client_to_labview.bin").read_bytes()
    assert b"".join(f.encode() for f in FrameBuffer().feed(raw)) == raw


def test_frame_buffer_handles_split_reads():
    raw = (FIX / "labview_to_client.bin").read_bytes()
    fb, out = FrameBuffer(), []
    for i in range(0, len(raw), 1337):
        out += fb.feed(raw[i:i + 1337])
    assert [f.type for f in out] == [f.type for f in frames("labview_to_client.bin")]


def test_lost_sync_raises():
    with pytest.raises(nw.ProtocolError):
        FrameBuffer().feed(b"XX" + b"\0" * 20)


def test_locator_listing_port():
    listing = [f for f in frames("locator_reply.bin") if b"NW_STREAMS" in f.payload][0]
    assert parse_listing_port(listing.payload) == 59114


def test_waveform_decode_from_capture():
    data = [f for f in frames("labview_to_client.bin") if f.type == MsgType.DATA]
    assert len(data) == 3
    prev = None
    for f in data:
        wfs = nw.decode_waveform_array(f.payload[16:])
        assert len(wfs) == 2
        for w in wfs:
            assert w.dt == pytest.approx(5e-5) and len(w.y) == 2000
            assert -1.2 < min(w.y) and max(w.y) < 1.2
        if prev is not None:
            assert wfs[0].t0 - prev == pytest.approx(0.1)
        prev = wfs[0].t0


def test_set_frequency_matches_capture():
    cmd = [f for f in frames("client_to_labview.bin") if f.type == MsgType.DATA][0]
    assert cmd.payload[12:] == nw.encode_message_dbl("Set Frequency", 10.1)
    assert nw.decode_message_dbl(cmd.payload[12:]) == ("Set Frequency", 10.1)


def test_link_create_template_matches_capture():
    creates = [f.payload for f in frames("client_to_labview.bin") if f.type == MsgType.LINK_CREATE]
    assert any(p.endswith(nw.TD_WAVEFORM_ARRAY_DBL) for p in creates)
    assert any(p.endswith(nw.TD_MESSAGE_CLUSTER) for p in creates)


# ---- end-to-end against the fake listener ----

def test_session_read_write_close():
    fake = FakeLabVIEW(n_frames=150)  # > reader buffer (100): exercises credit return
    with nw.connect(port=fake.port, url_host="EXAMPLE-PC1") as s:
        rd = s.open_reader("data/writer", "data/reader", nw.WAVEFORM_ARRAY)
        wr = s.open_writer("command/reader", "command/writer", nw.MESSAGE)
        assert rd.buffer_size == 100 and wr.buffer_size == 10
        wr.write(("Set Frequency", 20.0))
        got = [rd.read(timeout=5) for _ in range(150)]
    assert fake.done.wait(5)
    assert all(len(w) == 2 for w in got)
    assert nw.unpack_element(nw.MESSAGE, fake.commands[0]) == ("Set Frequency", 20.0)
    assert len(fake.closed_by_client) == 2
    assert fake.consumed == 150


def test_refused_link_raises():
    fake = FakeLabVIEW(refuse=("data/writer",))
    with nw.connect(port=fake.port) as s:
        with pytest.raises(nw.LinkRefused) as e:
            s.open_reader("data/writer", "data/reader", nw.WAVEFORM_ARRAY)
    assert e.value.code == 314050


def test_writer_blocks_without_credit():
    fake = FakeLabVIEW()
    with nw.connect(port=fake.port) as s:
        wr = s.open_writer("command/reader", "command/writer", nw.MESSAGE)
        wr._credits = __import__("threading").Semaphore(0)  # simulate a full reader buffer
        with pytest.raises(TimeoutError):
            wr.write_raw(b"x", timeout=0.2)


def test_waveform_and_message_types_match_capture_templates():
    from nwstreams.types import link_tail, ROLE_READER, ROLE_WRITER
    assert link_tail(nw.WAVEFORM_ARRAY, 100, ROLE_READER) == nw.TD_WAVEFORM_ARRAY_DBL
    assert link_tail(nw.MESSAGE, 10, ROLE_WRITER) == nw.TD_MESSAGE_CLUSTER


# ---- generic types, checked byte-exact against captures of the modified example ----
from nwstreams.types import ROLE_READER, ROLE_WRITER, link_tail

TYPE_CASES = [
    ("type_dbl.bin", nw.DBL, lambda i: float(i)),
    ("type_dbl_array.bin", nw.Array(nw.DBL), lambda i: [float(k) for k in range(100)]),
    ("type_string_array.bin", nw.Array(nw.STRING), lambda i: [f"{k}.000000" for k in range(100)]),
    ("type_variant_string_array.bin", nw.VARIANT, lambda i: [f"{k}.000000" for k in range(100)]),
]


@pytest.mark.parametrize("fixture,dtype,expected", TYPE_CASES)
def test_generic_type_descriptor_and_values(fixture, dtype, expected):
    fs = frames(fixture)
    creates = [f.payload for f in fs if f.type == MsgType.LINK_CREATE]
    assert any(p.endswith(link_tail(dtype, 100, ROLE_READER)) for p in creates)
    assert any(p.endswith(link_tail(nw.MESSAGE, 10, ROLE_WRITER)) for p in creates)
    elements = [f.payload[12:] for f in fs if f.type == MsgType.DATA]
    cmds, data = elements[:2], elements[2:]
    assert nw.unpack_element(nw.MESSAGE, cmds[0]) == ("Set Frequency", 10.1)
    assert nw.unpack_element(nw.MESSAGE, cmds[1])[0] == "Stop"
    for i, e in enumerate(data):
        v = nw.unpack_element(dtype, e)
        assert v == expected(i)
        assert nw.pack_element(dtype, v) == e


def test_odd_length_strings_are_padded():
    assert nw.STRING.pack("abc") == b"\x03\x00\x00\x00abc\x00"
    assert nw.STRING.unpack(b"\x03\x00\x00\x00abc\x00", 0) == ("abc", 8)


def test_cluster_descriptor_matches_command_capture():
    assert nw.MESSAGE.td() == bytes.fromhex("1400000001080200060000000009060000000209")


def test_descriptor_parser_roundtrip():
    for t in (nw.DBL, nw.STRING, nw.VARIANT, nw.Array(nw.DBL), nw.Array(nw.STRING), nw.MESSAGE):
        assert nw.parse_td(t.td()) == t
    with pytest.raises(NotImplementedError):   # waveform descriptor not decoded yet
        nw.parse_td(nw.WAVEFORM_ARRAY.td())


CLUSTER_T = nw.Cluster(nw.DBL, nw.BOOLEAN, nw.Array(nw.DBL), nw.Array(nw.STRING),
                       nw.Array(nw.BOOLEAN), nw.ERROR_CLUSTER, nw.PATH,
                       nw.Array(nw.DBL, 2), nw.VARIANT)


def test_cluster_capture_descriptor_and_values():
    fs = frames("type_cluster.bin")
    creates = [f.payload for f in fs if f.type == MsgType.LINK_CREATE]
    assert any(p.endswith(link_tail(CLUSTER_T, 100, ROLE_READER)) for p in creates)
    data = [f.payload[12:] for f in fs if f.type == MsgType.DATA][2:]   # skip 2 commands
    assert len(data) == 7
    for e in data:
        v = nw.unpack_element(CLUSTER_T, e)
        assert nw.pack_element(CLUSTER_T, v) == e
    vals = [nw.unpack_element(CLUSTER_T, e) for e in data]
    last = next(v for v in vals if v[5][1] == 65646)   # element with the error set
    assert last[:6] == (5.0, True, [0.0], ["sfs5d444"], [True], (True, 65646, ""))
    assert len(last[6].parts) == 6
    assert len(last[7]) == 11 and all(len(r) == 11 for r in last[7])
    assert last[8] is None   # empty variant


def test_parse_descriptor_from_capture():
    for p in (f.payload for f in frames("type_cluster.bin") if f.type == MsgType.LINK_CREATE):
        if b"data/writer" in p:
            a = 12
            for _ in range(2):
                a += 4 + struct.unpack_from("<I", p, a)[0]
            assert nw.parse_td(p, a + 4) == CLUSTER_T


def test_boolean_path_2d_roundtrip():
    t = nw.Cluster(nw.BOOLEAN, nw.PATH, nw.Array(nw.DBL, 2), nw.VARIANT)
    v = (True, nw.LVPath(("C", "data", "file.txt")), [[1.0, 2.0], [3.0, 4.0]], None)
    assert nw.unpack_element(t, nw.pack_element(t, v)) == v
    assert str(nw.LVPath(("C", "data", "file.txt"))) == r"C:\data\file.txt"


def test_cluster_live_element_multi_boolean_array():
    # Boolean arrays are packed 1 byte per element, array padded to even (live element)
    e = (FIX / "type_cluster_live.bin").read_bytes()
    v = nw.unpack_element(CLUSTER_T, e)
    assert v[4] == [True, True, False, True, True]
    assert v[5] == (True, 65610, "hjgfyjfydtydgdgd")
    assert len(v[7]) == 13 and all(len(r) == 15 for r in v[7])
    assert nw.pack_element(CLUSTER_T, v) == e


def test_boolean_array_odd_length_padding():
    t = nw.Array(nw.BOOLEAN)
    assert t.pack([True, False, True]) == b"\x03\x00\x00\x00\xff\x00\xff\x00"
    assert t.unpack(t.pack([True, False, True]), 0) == ([True, False, True], 8)


def test_waveform_writer_encoding_matches_capture():
    data = [f.payload[12:] for f in frames("labview_to_client.bin") if f.type == MsgType.DATA]
    w = nw.unpack_element(nw.WAVEFORM_ARRAY, data[0])
    fresh = [nw.Waveform(x.t0, x.dt, x.y) for x in w]          # templates for attributes
    assert nw.pack_element(nw.WAVEFORM_ARRAY, fresh) == data[0]
    # writer-role link tail for the same type
    tail = link_tail(nw.WAVEFORM_ARRAY, 100, ROLE_WRITER)
    assert tail[:-15] == nw.TD_WAVEFORM_ARRAY_DBL[:-15] and tail[-7:-3] == b"\x02\x00\x00\x00"


def test_wait_for_peer_retries_until_endpoint_exists():
    fake = FakeLabVIEW(refuse_times=5)   # endpoint "appears" after 5 refusals
    with nw.connect(port=fake.port) as s:
        rd = s.open_reader("data/writer", "data/reader", nw.WAVEFORM_ARRAY, wait_for_peer=2)
        assert len(rd.read(timeout=5)) == 2


# ---- DataSocket (dstp://), from a capture of NI's 'Simple DataSocket' example ----

def _ds_messages(name):
    b = (FIX / name).read_bytes()
    o, out = 0, []
    while o + 4 <= len(b):
        n = struct.unpack_from("<I", b, o)[0]
        if o + n > len(b):
            break
        out.append((b[o:o + n], nw.VARIANT.unpack(b, o)[0]))
        o += n
    return out


def test_datasocket_client_messages_decode_and_reencode():
    msgs = _ds_messages("ds_client_to_server.bin")
    ops = [v[0] for _, v in msgs]
    assert ops[:3] == [1, 2, 4] and ops[-2:] == [5, 3] and ops.count(13) == 177
    raw, first_write = next((r, v) for r, v in msgs if v[0] == 13)
    assert first_write[1] == "wave1" and len(first_write[2]) == 40 and first_write[3] == 104
    assert first_write[2][1] == pytest.approx(0.0998, abs=1e-4)          # sin(0.1)
    t = nw.Cluster(nw.I32, nw.STRING, nw.Array(nw.DBL), nw.U32)
    assert nw.VARIANT.pack(nw.types.VariantValue(t, first_write)) == raw


def test_datasocket_server_messages_decode():
    vals = [v for _, v in _ds_messages("ds_server_to_client.bin")]
    assert vals[0] == (9, 3) and vals[1] == (12, "wave1", 4)
    assert vals[2] == (14, 104, 1)


def _fake_ds_server(ack_opens: bool):
    """Tiny DataSocket server: answers hello; acknowledges opens only if ack_opens."""
    import socket as _s, threading as _t
    from nwstreams.datasocket import _msg
    srv = _s.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    def run():
        c, _ = srv.accept()
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    break
                buf += d
                while len(buf) >= 4 and len(buf) >= struct.unpack_from("<I", buf, 0)[0]:
                    n = struct.unpack_from("<I", buf, 0)[0]
                    v = nw.VARIANT.unpack(buf[:n], 0)[0]; buf = buf[n:]
                    if v == (2, 3):
                        c.sendall(_msg(nw.Cluster(nw.I32, nw.I32), (9, 3)))
                    elif v[0] == 4 and ack_opens:
                        c.sendall(_msg(nw.Cluster(nw.I32, nw.STRING, nw.I32), (12, v[1], v[2])))
        except OSError:
            pass
        finally:
            c.close(); srv.close()
    _t.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def test_datasocket_busy_item_raises_clear_error():
    port = _fake_ds_server(ack_opens=False)
    with nw.DataSocketClient(port=port, timeout=0.3) as ds:
        with pytest.raises(nw.DataSocketItemBusy) as e:
            ds.open("dstp://localhost/wave1", mode=ds.MODE_WRITE)
        assert "another client" in str(e.value)
        with pytest.raises(TimeoutError) as e2:              # read mode: plain timeout
            ds.open("wave1", mode=ds.MODE_READ)
        assert not isinstance(e2.value, nw.DataSocketItemBusy)


def test_datasocket_open_acknowledged():
    port = _fake_ds_server(ack_opens=True)
    with nw.DataSocketClient(port=port, timeout=1) as ds:
        assert ds.open("dstp://localhost/wave1", mode=ds.MODE_WRITE) == "wave1"


# ---- Shared Variables (NI-PSP), from a capture of NI's 'Shared Variable' example ----
from nwstreams import sharedvariable as svm


def test_shared_variable_capture_messages():
    c = frames("sv_client_to_engine.bin")
    s = frames("sv_engine_to_client.bin")
    opens = [f.payload for f in c if f.type == svm.Msg.OPEN]
    assert opens[0] == (struct.pack("<I", 1) + nw.framing.lv_string(
        rb"\\EXAMPLE-PC1\Server Variables\Monitor Value 1") + svm._OPEN_TAIL)
    writes = [f.payload for f in c if f.type == svm.Msg.WRITE]
    assert len(writes) == 106
    for p in writes:                       # handle, item, 1, DBL record in 0..1
        h, item, one = struct.unpack_from("<III", p, 0)
        v = svm.parse_record(p, 12)
        assert one == 1 and 0.0 <= v <= 1.0 and p[12:] == svm.dbl_record(v)
    subs = [f.payload for f in c if f.type == svm.Msg.SUBSCRIBE]
    assert subs[0] == struct.pack("<IIH", 3, 1, svm.SUBSCRIBE_FLAGS)
    updates = [f for f in s if f.type == svm.Msg.UPDATE and f.flags == 8]
    assert len(updates) == 62
    assert all(0.0 <= svm.parse_record(f.payload, 4) <= 1.0 for f in updates)
    props = {struct.unpack_from("<I", f.payload)[0]: f.payload[4:] for f in s if f.type == svm.Msg.PROPERTIES}
    assert struct.unpack_from("<I", props[2], 8)[0] == 100     # Monitor Value 2 buffer size


def test_psp_url_parsing():
    assert svm.parse_psp_url(r"psp:\\localhost\Server Variables\Monitor Value 1") == \
        ("localhost", "Server Variables", "Monitor Value 1")
    assert svm.parse_psp_url("Monitor Value 1", "Lib") == (None, "Lib", "Monitor Value 1")
    assert svm.parse_psp_url(r"Lib\X") == (None, "Lib", "X")


def _fake_sve():
    """Stand-in Shared Variable Engine: session, open, subscribe, write echo, close."""
    import socket as _s, threading as _t
    srv = _s.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    state = {"values": {}, "closed": []}
    def run():
        c, _ = srv.accept(); fb = FrameBuffer(); handles = iter(range(5, 100))
        send = lambda f: c.sendall(f.encode())
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    break
                for f in fb.feed(d):
                    t, p = f.type, f.payload
                    if t == 0xE0:
                        send(Frame(0xE1, src=0x03EB, dst=0x042A))
                    elif t == 0xE3:
                        if b"PROCESS" in p:
                            send(Frame(0xDF, bytes.fromhex("d00700000a000000"), 3, 0x03EB, 0x042A))
                        send(Frame(0xE4, bytes.fromhex("00000000e903e90301"), 0, 0x03EB, 0x042A))
                    elif t == svm.Msg.OPEN:
                        item = struct.unpack_from("<I", p)[0]; h = next(handles)
                        state["values"].setdefault(item, 0.5)
                        send(Frame(0x65, struct.pack("<II", item, h), src=0x03EA, dst=0x03EA))
                        send(Frame(0x66, struct.pack("<I", item) + bytes(18), src=0x03EA, dst=0x03EA))
                    elif t == svm.Msg.SUBSCRIBE:
                        h, item = struct.unpack_from("<II", p)
                        send(Frame(0x68, struct.pack("<I", item) + svm.dbl_record(state["values"][item]),
                                   flags=8, src=0x03EA, dst=0x03EA))
                    elif t == svm.Msg.WRITE:
                        h, item, _ = struct.unpack_from("<III", p)
                        state["values"][item] = svm.parse_record(p, 12)
                        send(Frame(0x68, struct.pack("<I", item) + p[12:], flags=8, src=0x03EA, dst=0x03EA))
                    elif t == svm.Msg.CLOSE:
                        state["closed"].append(struct.unpack_from("<II", p))
        except OSError:
            pass
        finally:
            c.close(); srv.close()
    _t.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], state


def test_shared_variable_client_end_to_end():
    port, state = _fake_sve()
    with nw.SharedVariableClient("Server Variables", port=port, machine="EXAMPLE-PC1", timeout=2) as sv:
        sv.open(r"psp:\\localhost\Server Variables\Monitor Value 1")
        assert sv.read("Monitor Value 1") == 0.5          # current value after subscribe
        sv.write("Monitor Value 1", 0.25)
        assert sv.read("Monitor Value 1") == 0.25
        with pytest.raises(ValueError):
            sv.open(r"psp:\\localhost\Other Lib\X")
    import time; time.sleep(0.2)
    assert state["closed"] == [(5, 1)]
