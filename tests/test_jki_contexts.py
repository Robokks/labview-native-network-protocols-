"""Contexts and JKI Network Stream messages, checked against a capture of JKI's 2-way example."""
import struct
from pathlib import Path

import nwstreams as nw
from nwstreams.framing import FrameBuffer, MsgType
from nwstreams.jki import JKI_MESSAGE, BUFFER
from nwstreams.locator import parse_listing_contexts, parse_listing_port
from nwstreams.types import ROLE_READER, ROLE_WRITER, link_tail

FIX = Path(__file__).parent / "fixtures"


def frames(name):
    return FrameBuffer().feed((FIX / name).read_bytes())


def creates():
    out = []
    for f in frames("jki_client.bin"):
        if f.type == MsgType.LINK_CREATE:
            p, a, urls = f.payload, 12, []
            for _ in range(2):
                n = struct.unpack_from("<I", p, a)[0]
                urls.append(p[a + 4:a + 4 + n].decode()); a += 4 + n
            out.append((urls, p[a:]))
    return out


def test_service_and_urls_use_contexts():
    svc = [f.payload for f in frames("jki_client.bin") if f.type == MsgType.SERVICE_OPEN][0]
    assert svc == nw.framing.lv_string(b"rt/NW_STREAMS") + bytes.fromhex("e90302")
    urls = sorted(u for u, _ in creates())
    assert urls == [["ni.dex://EXAMPLE-PC1:RT/RT_Reader", "ni.dex://EXAMPLE-PC1:PC/PC_Writer"],
                    ["ni.dex://EXAMPLE-PC1:RT/RT_Writer", "ni.dex://EXAMPLE-PC1:PC/PC_Reader"]]


def test_jki_link_tails_match_labview():
    tails = {u[0].rsplit("/", 1)[1]: t for u, t in creates()}
    assert tails["RT_Reader"] == link_tail(JKI_MESSAGE, BUFFER, ROLE_WRITER)
    assert tails["RT_Writer"] == link_tail(JKI_MESSAGE, BUFFER, ROLE_READER)


def test_jki_messages_roundtrip():
    msgs = [f.payload[12:] for name in ("jki_client.bin", "jki_server.bin")
            for f in frames(name) if f.type == MsgType.DATA]
    vals = [nw.unpack_element(JKI_MESSAGE, e) for e in msgs]
    assert all(nw.pack_element(JKI_MESSAGE, v) == e for v, e in zip(vals, msgs))
    sent = [v for v in vals if v[0][0] == "Cmd From PC"]
    acks = {v[2] for v in vals if v[0][0] == "Ack Response"}
    assert sent and all(v[1] is True and v[2] in acks for v in sent)
    assert ("Cmd From RT", "Arguments are variants. Wire anything!", 0) in [v[0] for v in vals]


def test_locator_contexts():
    listing = max((f.payload for f in frames("jki_locator.bin") if f.type == 0xDD), key=len)
    assert parse_listing_contexts(listing) == {"pc": 59114, "rt": 59114}
    assert parse_listing_port(listing, "RT") == 59114


def test_session_url_with_contexts():
    s = nw.Session.__new__(nw.Session)
    s.url_host, s.context, s.local_context = "HOST", "RT", "PC"
    assert s.url("RT_Reader") == "ni.dex://HOST:RT/RT_Reader"
    assert s.url("PC_Writer", local=True) == "ni.dex://HOST:PC/PC_Writer"
    s.context = s.local_context = None
    assert s.url("data/writer") == "ni.dex://HOST/data/writer"
