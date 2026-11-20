"""EXPERIMENTAL: Python in place of 'Simple Network Streams - Target.vi'.

The UNCHANGED LabVIEW Host.vi connects to Python:
  1. locator responder on TCP 2343: answers Host's lookup, advertising NW_STREAMS on --stream-port
  2. stream server on --stream-port: accepts /data/writer + /command/reader, streams sine + square
     waveforms (2 x 2000 samples, dt 50 us) every --period-ms and applies 'Set Frequency'.

Run it on a SECOND device (any OS, Python >= 3.9, `pip install nwstreams`) and type that device's IP
into Host.vi's "Target IP Address". Port 2343 must be free there, so don't use a PC running NI software
(NI's "NI PSP Service Locator" owns 2343 on every address). Allow inbound TCP 2343 and the stream port
in the device's firewall.

    python python_target.py [--stream-port 59200] [--period-ms 20] [--seconds 600]
"""
import argparse
import math
import os
import random
import socket
import struct
import threading
import time

import nwstreams as nw
from nwstreams.framing import Frame, FrameBuffer, MsgType

PARAMS = bytes.fromhex("d00700000a000000")
N, DT = 2000, 5e-5
REFUSED_NO_ENDPOINT = 314050


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- locator (TCP 2343)
# NI's locator listing, captured from an NI PSP Service Locator (machine names removed).
_LISTING = bytes.fromhex(
    "8e000a000000230000005f5f4e495f44454641554c545f53545245414d5f4c495354454e45525f4e414d455f5f0a0000"
    "004e575f53545245414d53020000004e4907000000312e312e302e3001000200e6ea000000000200000030330b000000"
    "416c61726d53657276657244000000433a5c50726f6772616d2046696c65732028783836295c4e6174696f6e616c2049"
    "6e737472756d656e74735c5368617265645c5461676765725c7461677372762e6578650c00000032362e352e302e3439"
    "3333300200010017730200e6e800000000070000004369746164656c070000004349544144454c070000004c6f6f6b6f"
    "757407000000322e30302e303001000100177000000000040000004d4953430700000050524f4345535344000000433a"
    "5c50726f6772616d2046696c65732028783836295c4e6174696f6e616c20496e737472756d656e74735c536861726564"
    "5c5461676765725c7461677372762e6578650c00000032362e352e302e34393333300200010017770200e6e800000000"
    "140000004e495f526571756573745f50726f636573736f72140000004e495f524551554553545f50524f434553534f52"
    "140000004e6174696f6e616c20496e737472756d656e747309000000352e322e302e3838320200010017740200e6e900"
    "000000080000004e49446f6d61696e0f00000053454355524954595f444f4d41494e140000004e6174696f6e616c2049"
    "6e737472756d656e747307000000312e302e302e300200010017710200e6e71000000000000000000000000000000000"
    "0000000600000053797374656d0700000050524f4345535344000000433a5c50726f6772616d2046696c657320287838"
    "36295c4e6174696f6e616c20496e737472756d656e74735c5368617265645c5461676765725c7461677372762e657865"
    "0c00000032362e352e302e34393333300200010017770200e6e8000000000d0000005461676765724374726c456e7612"
    "000000434f4e54524f4c454e565f42524f5753455244000000433a5c50726f6772616d2046696c65732028783836295c"
    "4e6174696f6e616c20496e737472756d656e74735c5368617265645c5461676765725c7461677372762e6578650c0000"
    "0032362e352e302e34393333300200010017760200e6e8000000000d0000005461676765724374726c456e7613000000"
    "434f4e54524f4c454e565f5245414c54494d4544000000433a5c50726f6772616d2046696c65732028783836295c4e61"
    "74696f6e616c20496e737472756d656e74735c5368617265645c5461676765725c7461677372762e6578650c00000032"
    "362e352e302e34393333300200010017750200e6e8000000000900000054696d6553796e63310800000054696d655379"
    "6e63140000004e6174696f6e616c20496e737472756d656e747308000000342e372e302e31330200010017720200e6e6"
    "00000000"
)


def locator_listing(stream_port: int) -> bytes:
    """The captured locator listing with the NW_STREAMS port replaced by ours."""
    listing = bytearray(_LISTING)
    o = listing.find(b"NW_STREAMS") + len(b"NW_STREAMS")
    for _ in range(2):                       # vendor "NI", version "1.1.0.0"
        o += 4 + struct.unpack_from("<I", listing, o)[0]
    struct.pack_into(">H", listing, o + 4, stream_port)
    assert nw.locator.parse_listing_port(bytes(listing)) == stream_port
    return bytes(listing)


def serve_locator(listing: bytes, stop: threading.Event, port: int = 2343):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("0.0.0.0", port))
    srv.listen(8)
    srv.settimeout(0.5)
    log(f"locator listening on TCP {port}")
    while not stop.is_set():
        try:
            c, addr = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=_locator_session, args=(c, addr, listing), daemon=True).start()
    srv.close()


def _locator_session(c, addr, listing):
    fb, my_ctx, peer = FrameBuffer(), 0x0403, 0
    try:
        while True:
            d = c.recv(65536)
            if not d:
                return
            for f in fb.feed(d):
                if f.type == 0xE0:
                    peer = f.src
                    c.sendall(Frame(0xE1, src=my_ctx, dst=peer).encode())
                elif f.type == 0xE3:
                    c.sendall(Frame(0xDF, PARAMS, flags=3, src=my_ctx, dst=peer).encode()
                              + Frame(0xE4, bytes.fromhex("00000000e903e90301"), src=my_ctx, dst=peer).encode())
                elif f.type == 0xDD and f.payload == bytes.fromhex("fd020304"):
                    c.sendall(Frame(0xDD, bytes.fromhex("fe0004")).encode())
                elif f.type == 0xDD and f.payload == bytes.fromhex("1800"):
                    c.sendall(Frame(0xDD, listing).encode())
                    log(f"locator: answered lookup from {addr[0]}")
                elif f.type == 0xE5:
                    return
    except OSError:
        pass
    finally:
        c.close()


# ---------------------------------------------------------------- stream server
class Link:
    def __init__(self, name, peer_id, peer_h, my_h, buf):
        self.name, self.peer_id, self.peer_h, self.my_h, self.buf = name, peer_id, peer_h, my_h, buf
        self.my_id = os.urandom(8)
        self.credits = threading.Semaphore(0)
        self.confirmed = False

    def hdls(self):
        return struct.pack("<II", self.my_h, self.peer_h)


class TargetSession:
    ENDPOINTS = {"data/writer": ("data", 100), "command/reader": ("command", 10)}

    def __init__(self, sock, args, state):
        self.c, self.args, self.state = sock, args, state
        self.lock = threading.Lock()
        self.links = {}          # by name
        self.by_my_h = {}
        self.handles = iter(range(0x00C00001, 0x00C00100))
        self.my_ctx, self.peer_ctx = 0x0426, 0
        self.alive = True

    def send(self, f):
        with self.lock:
            self.c.sendall(f.encode())

    def run(self):
        fb = FrameBuffer()
        threading.Thread(target=self._keepalive, daemon=True).start()
        try:
            while self.alive:
                d = self.c.recv(1 << 16)
                if not d:
                    break
                for f in fb.feed(d):
                    self.on_frame(f)
        except OSError:
            pass
        finally:
            self.alive = False
            self.c.close()
            log("stream session ended")

    def _keepalive(self):
        while self.alive:
            time.sleep(2)
            try:
                self.send(Frame(0xDF, src=self.my_ctx, dst=self.peer_ctx))
            except OSError:
                return

    def on_frame(self, f):
        t, p = f.type, f.payload
        if t == 0xE0:
            self.peer_ctx = f.src
            self.send(Frame(0xE1, src=self.my_ctx, dst=self.peer_ctx))
        elif t == 0xE3:
            self.send(Frame(0xDF, PARAMS, flags=3, src=self.my_ctx, dst=self.peer_ctx))
            self.send(Frame(0xE4, bytes.fromhex("00000000e903e90302"), src=self.my_ctx, dst=self.peer_ctx))
        elif t == MsgType.LINK_CREATE:
            self.on_create(p)
        elif t == MsgType.LINK_CONFIRM:
            link = self.by_my_h.get(struct.unpack_from("<I", p, 12)[0])
            if link and not link.confirmed:
                link.confirmed = True
                if link.name == "command":      # we are the reader: grant our buffer as credits
                    self.send(Frame(MsgType.CREDIT_GRANT, link.my_id + link.hdls() + struct.pack("<II", link.buf, 0)))
                else:                           # we are the writer: announce ready, LabVIEW then grants credits
                    self.send(Frame(MsgType.WRITER_READY, link.my_id + link.hdls()))
        elif t == MsgType.CREDIT_GRANT:          # Host's data reader grants credits to our writer
            link = self.by_my_h.get(struct.unpack_from("<I", p, 12)[0])
            if link:
                for _ in range(struct.unpack_from("<I", p, 16)[0]):
                    link.credits.release()
                log(f"data link ready, {struct.unpack_from('<I', p, 16)[0]} credits")
                if not getattr(link, "pumping", False):
                    link.pumping = True
                    threading.Thread(target=self.pump, args=(link,), daemon=True).start()
        elif t == MsgType.CONSUMED:
            link = self.by_my_h.get(struct.unpack_from("<I", p, 4)[0])
            if link and link.name == "data":
                for _ in range(struct.unpack_from("<I", p, 8)[0]):
                    link.credits.release()
        elif t == MsgType.DATA:                  # command from Host
            src, dst, cnt = struct.unpack_from("<III", p, 0)
            link = self.by_my_h.get(dst)
            if link:
                self.send(Frame(MsgType.RECEIVED, link.hdls() + struct.pack("<I", cnt)))
                self.send(Frame(MsgType.CONSUMED, link.hdls() + struct.pack("<I", cnt)))
                msg, val = nw.unpack_element(nw.MESSAGE, p[12:])
                log(f"command from Host.vi: {msg!r} = {val!r}")
                if msg == "Set Frequency" and isinstance(val, float):
                    self.state["freq"] = val
        elif t == MsgType.LINK_CLOSED:           # Host closed a link: acknowledge
            self.send(Frame(MsgType.LINK_CLOSED_ACK, p[:12]))
        elif t == 0xE5:
            self.alive = False

    def on_create(self, p):
        cid, ch = p[:8], struct.unpack_from("<I", p, 8)[0]
        n = struct.unpack_from("<I", p, 12)[0]
        remote = p[16:16 + n].decode("latin-1")
        path = remote.split("://", 1)[-1].split("/", 1)[-1]
        if path not in self.ENDPOINTS or self.links.get(self.ENDPOINTS[path][0]):
            self.send(Frame(MsgType.LINK_REFUSED, cid + struct.pack("<II", ch, REFUSED_NO_ENDPOINT)))
            return
        name, buf = self.ENDPOINTS[path]
        link = Link(name, cid, ch, next(self.handles), buf)
        self.links[name] = link
        self.by_my_h[link.my_h] = link
        self.send(Frame(MsgType.LINK_ACCEPT, link.my_id + cid + struct.pack("<IIII", link.my_h, ch, buf, buf) + b"\x01"))
        log(f"accepted link {path} (buffer {buf})")

    def pump(self, link):
        t_sig, n = 0.0, 0
        while self.alive and self.state["running"]:
            if not link.credits.acquire(timeout=1):
                continue
            f = self.state["freq"]
            ts = [t_sig + k * DT for k in range(N)]
            sine = [math.sin(2 * math.pi * f * t) + random.uniform(-0.1, 0.1) for t in ts]
            square = [(1.0 if math.sin(2 * math.pi * f * t) >= 0 else -1.0) + random.uniform(-0.1, 0.1) for t in ts]
            elem = nw.pack_element(nw.WAVEFORM_ARRAY, [nw.Waveform(t_sig, DT, sine), nw.Waveform(t_sig, DT, square)])
            try:
                self.send(Frame(MsgType.DATA, link.hdls() + struct.pack("<I", 1) + elem))
            except OSError:
                return
            n += 1
            t_sig += N * DT
            if n == 1 or n % 250 == 0:
                log(f"sent {n} waveform elements (frequency {f})")
            # Wait AFTER each write, like LabVIEW's "Wait (ms)" in Target.vi: the loop period is then
            # period + work time, so a Host.vi reading at the same nominal speed keeps up (backlog ~0).
            # A fixed schedule that catches up sends slightly faster and fills Host's 100-element buffer.
            time.sleep(self.args.period_ms / 1000)


def serve_stream(args, state, stop):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.stream_port))
    srv.listen(4)
    srv.settimeout(0.5)
    log(f"stream server listening on TCP {args.stream_port}")
    while not stop.is_set():
        try:
            c, addr = srv.accept()
        except socket.timeout:
            continue
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log(f"stream session from {addr[0]}:{addr[1]}")
        threading.Thread(target=TargetSession(c, args, state).run, daemon=True).start()
    srv.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream-port", type=int, default=59200)
    ap.add_argument("--period-ms", type=float, default=20)
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--frequency", type=float, default=10.0)
    ap.add_argument("--no-locator", action="store_true")
    ap.add_argument("--locator-port", type=int, default=2343, help="2343 for LabVIEW; other values for testing")
    args = ap.parse_args(argv)
    state = {"freq": args.frequency, "running": True}
    stop = threading.Event()
    threads = [threading.Thread(target=serve_stream, args=(args, state, stop), daemon=True)]
    if not args.no_locator:
        threads.append(threading.Thread(target=serve_locator, args=(locator_listing(args.stream_port), stop, args.locator_port),
                                        daemon=True))
    for t in threads:
        t.start()
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    state["running"] = False
    stop.set()
    time.sleep(0.6)
    log("Python Target stopped")


if __name__ == "__main__":
    main()
