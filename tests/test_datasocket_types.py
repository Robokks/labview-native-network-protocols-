"""DataSocket specifics seen live with LabVIEW 2026's DataSocket Write (2026-11-20)."""
import nwstreams as nw
from nwstreams.datasocket import _ds_type, _msg, _user_attrs, _write_type
from nwstreams.types import Cluster, I32, STRING, U32, parse_td_body

# (6, item, value) messages the server pushed for items LabVIEW wrote
ARR2D = bytes.fromhex(
    "58000000 01080300 06000000 0003 06000000 0009 0e000000 0008 0200 06000000 0003"
    "06000000 0e000000 74797065735f6933326172723264"
    "02000000 03000000 01000000 04000000 02000000 05000000 03000000 06000000".replace(" ", ""))
VARATTR = bytes.fromhex(
    "56000000 01080300 06000000 0003 06000000 0009 24000000 0240 1c000000 0340 0e000000"
    "0009 04000000 756e6974 0009 01000000 5600 0204"
    "06000000 0d000000 74797065735f76617261747472 00 000000000000 0440".replace(" ", ""))


def decode(msg):
    """What DataSocketClient._read_one returns for one message."""
    t, p = parse_td_body(msg, 4)
    t = _ds_type(t)
    v = t.unpack(msg, p)[0]
    attrs = _user_attrs(t.fields[2])
    return (v[0], v[1], nw.LVVariant(v[2], attrs)) if attrs is not None else v


def test_2d_array_is_column_major_on_the_wire():
    assert decode(ARR2D) == (6, "types_i32arr2d", [[1, 2, 3], [4, 5, 6]])


def test_variant_attributes_travel_in_the_value_descriptor():
    op, item, v = decode(VARATTR)
    assert (op, item) == (6, "types_varattr")
    assert v.value == 2.5 and v.attributes == {"unit": "V"}


def test_write_matches_labview_layout():
    # a write (13, item, value, seq) carries the value exactly as LabVIEW's push (6, ...) does
    for msg, value, dtype in ((ARR2D, [[1, 2, 3], [4, 5, 6]], nw.Array(nw.I32, 2)),
                              (VARATTR, nw.LVVariant(2.5, nw.Attributes({"unit": "V"})), None)):
        t, v = _write_type(value, dtype)
        item = decode(msg)[1]
        ours = _msg(Cluster(I32, STRING, t, U32), (13, item, v, 1))
        theirs_value = msg[-(len(msg) - _value_offset(msg)):]
        assert ours[-(len(theirs_value) + 4):-4] == theirs_value


def _value_offset(msg):
    """Offset of the value bytes in a (6, item, value) message."""
    t, p = parse_td_body(msg, 4)
    _, p = I32.unpack(msg, p)
    _, p = STRING.unpack(msg, p)
    return p


def test_access_denied_opcode_51():
    """A remote DataSocket Server answers a refused write-mode open with (51, item)."""
    import socket, struct, threading
    import nwstreams as nw
    from nwstreams.datasocket import _msg
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    def run():
        c, _ = srv.accept(); buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    break
                buf += d
                while len(buf) >= 4 and len(buf) >= struct.unpack_from("<I", buf)[0]:
                    n = struct.unpack_from("<I", buf)[0]
                    v = nw.VARIANT.unpack(buf[:n], 0)[0]; buf = buf[n:]
                    if v == (2, 3):
                        c.sendall(_msg(nw.Cluster(nw.I32, nw.I32), (9, 3)))
                    elif v[0] == 4 and v[2] & 4:          # write bit: refuse like a remote server
                        c.sendall(_msg(nw.Cluster(nw.I32, nw.STRING), (51, v[1])))
                    elif v[0] == 4:
                        c.sendall(_msg(nw.Cluster(nw.I32, nw.STRING, nw.I32), (12, v[1], v[2])))
        except OSError:
            pass
        finally:
            c.close(); srv.close()
    threading.Thread(target=run, daemon=True).start()
    with nw.DataSocketClient(port=srv.getsockname()[1], timeout=1) as ds:
        try:
            ds.open("wave1", mode=ds.MODE_WRITE)
            raise AssertionError("expected DataSocketAccessDenied")
        except nw.DataSocketAccessDenied as e:
            assert "DefaultWriters" in str(e)
        assert ds.open("wave1") == "wave1"                # MODE_READ (3) is accepted
