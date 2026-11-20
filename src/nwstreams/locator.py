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


DEFAULT_CONTEXT = "__NI_DEFAULT_STREAM_LISTENER_NAME__"


def _listing(host: str, timeout: float) -> bytes:
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
    return listing


def locate_port(host: str = "127.0.0.1", timeout: float = 3.0, context: str = None) -> int:
    """Return the TCP port advertised for a Network Streams context.

    context=None is LabVIEW's default context. A named context (e.g. "RT", as used by the JKI
    Network Stream library's "My Name") is its own locator entry; the name is matched
    case-insensitively (LabVIEW registers it in lower case)."""
    return parse_listing_port(_listing(host, timeout), context)


def list_contexts(host: str = "127.0.0.1", timeout: float = 3.0) -> "dict[str, int]":
    """All Network Streams contexts the locator advertises, with their ports."""
    return parse_listing_contexts(_listing(host, timeout))


def parse_listing_contexts(listing: bytes) -> "dict[str, int]":
    out, o = {}, 0
    key = lv_string(b"NW_STREAMS")
    while True:
        i = listing.find(key, o)
        if i < 0:
            return out
        # the context name is the length-prefixed string right before "NW_STREAMS"
        name = None
        for back in range(4, 260):
            j = i - back
            if j < 0:
                break
            n = struct.unpack_from("<I", listing, j)[0]
            if n == back - 4:
                name = listing[j + 4:i].decode("latin-1")
                break
        p = i + len(key)
        for _ in range(2):  # vendor ("NI"), version ("1.1.0.0")
            p += 4 + struct.unpack_from("<I", listing, p)[0]
        if name is not None:
            out[name] = struct.unpack_from(">H", listing, p + 4)[0]
        o = i + len(key)


def parse_listing_port(listing: bytes, context: str = None) -> int:
    contexts = parse_listing_contexts(listing)
    name = DEFAULT_CONTEXT if context is None else context.lower()
    for k, port in contexts.items():
        if k.lower() == name.lower():
            return port
    if context is None and contexts:          # older listings: take the first NW_STREAMS entry
        return next(iter(contexts.values()))
    raise ProtocolError(f"Network Streams context {name!r} not advertised "
                        f"(advertised: {sorted(contexts)}); is that endpoint running?")
