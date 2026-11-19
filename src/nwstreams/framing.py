"""'C7' frame layer: 14-byte header, big-endian length, little-endian payload contents."""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"C7"
HEADER = struct.Struct(">2sIHBBHH")  # magic, length, flags, type, version, src ctx, dst ctx
HEADER_SIZE = HEADER.size  # 14
VERSION = 1
CTX_STREAM = 0x03E9


class MsgType:
    # session layer
    SESSION_OPEN = 0xE0
    SESSION_ACCEPT = 0xE1
    PARAMS = 0xDF          # 8-byte params; empty payload = 2 s keep-alive
    SERVICE_OPEN = 0xE3
    SERVICE_REPLY = 0xE4
    SESSION_CLOSE = 0xE5
    LOCATOR_QUERY = 0xDD
    # stream layer
    LINK_CREATE = 0x01
    LINK_ACCEPT = 0x02
    LINK_CONFIRM = 0x03
    WRITER_READY = 0x04
    CREDIT_GRANT = 0x05
    DATA = 0x06
    RECEIVED = 0x07
    CONSUMED = 0x08
    LINK_REFUSED = 0x09
    LINK_CLOSED = 0x0A
    LINK_CLOSED_ACK = 0x0B


class ProtocolError(IOError):
    pass


@dataclass(frozen=True)
class Frame:
    type: int
    payload: bytes = b""
    flags: int = 0
    src: int = CTX_STREAM
    dst: int = CTX_STREAM

    def encode(self) -> bytes:
        return HEADER.pack(MAGIC, len(self.payload), self.flags, self.type, VERSION,
                           self.src, self.dst) + self.payload


class FrameBuffer:
    """Accumulates TCP bytes and yields complete frames (TCP boundaries != frame boundaries)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        out = []
        while len(self._buf) >= HEADER_SIZE:
            magic, ln, fl, typ, _ver, src, dst = HEADER.unpack_from(self._buf, 0)
            if magic != MAGIC:
                raise ProtocolError(f"lost frame sync: {bytes(self._buf[:16]).hex()}")
            end = HEADER_SIZE + ln
            if len(self._buf) < end:
                break
            out.append(Frame(typ, bytes(self._buf[HEADER_SIZE:end]), fl, src, dst))
            del self._buf[:end]
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)


def lv_string(s: bytes) -> bytes:
    """LabVIEW flattened string: u32 LE length + bytes."""
    return struct.pack("<I", len(s)) + s
