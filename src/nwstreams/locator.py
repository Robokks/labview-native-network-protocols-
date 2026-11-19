"""Discover the Network Streams listener port through the locator service on TCP 2343."""
from __future__ import annotations

import socket
import struct

from .framing import Frame, FrameBuffer, MsgType, ProtocolError, lv_string

LOCATOR_PORT = 2343
_PARAMS = bytes.fromhex("d00700000a000000")


def _recv_until(sock: socket.socket, fb: FrameBuffer, pred, timeout: float):
    sock.settimeout(timeout)
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise ProtocolError("locator closed the connection")
        for f in fb.feed(chunk):
            if pred(f):
                return f


def locate_port(host: str = "127.0.0.1", timeout: float = 3.0) -> int:
    """Return the TCP port advertised for the NW_STREAMS service."""
    with socket.create_connection((host, LOCATOR_PORT), timeout=timeout) as s:
        fb = FrameBuffer()
        s.sendall(Frame(MsgType.SESSION_OPEN, src=0x03F8, dst=0).encode())
        _recv_until(s, fb, lambda f: f.type == MsgType.SESSION_ACCEPT, timeout)
        s.sendall(Frame(MsgType.PARAMS, _PARAMS, flags=1, src=0x03F8, dst=0x03F0).encode()
                  + Frame(MsgType.SERVICE_OPEN,
                          lv_string(b"LogosClassifieds/CLASSADS") + bytes.fromhex("e90301"),
                          src=0x03F8, dst=0x03F0).encode())
        _recv_until(s, fb, lambda f: f.type == MsgType.SERVICE_REPLY, timeout)
        s.sendall(Frame(MsgType.LOCATOR_QUERY, bytes.fromhex("fd020304")).encode()
                  + Frame(MsgType.LOCATOR_QUERY, bytes.fromhex("1800")).encode())
        listing = _recv_until(s, fb, lambda f: b"NW_STREAMS" in f.payload, timeout).payload
        try:
            s.sendall(Frame(MsgType.SESSION_CLOSE).encode())
        except OSError:
            pass
    return parse_listing_port(listing)


def parse_listing_port(listing: bytes) -> int:
    i = listing.find(b"NW_STREAMS")
    if i < 0:
        raise ProtocolError("NW_STREAMS service not advertised")
    o = i + len(b"NW_STREAMS")
    for _ in range(2):  # vendor ("NI"), version ("1.1.0.0")
        n = struct.unpack_from("<I", listing, o)[0]
        o += 4 + n
    return struct.unpack_from(">H", listing, o + 4)[0]
