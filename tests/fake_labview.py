"""Minimal fake LabVIEW listener that behaves like the captured Target.vi side.

Serves one writer endpoint (waveform frames from the capture fixture, credit-limited)
and one reader endpoint (records received commands).
"""
import os
import socket
import struct
import threading
from pathlib import Path

from nwstreams.framing import Frame, FrameBuffer, MsgType

FIX = Path(__file__).parent / "fixtures"


def captured_data_payloads():
    fb = FrameBuffer()
    return [f.payload[12:] for f in fb.feed((FIX / "labview_to_client.bin").read_bytes())
            if f.type == MsgType.DATA]


class FakeLabVIEW:
    def __init__(self, n_frames=20, refuse=(), refuse_times=0):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.payloads = captured_data_payloads()
        self.n_frames, self.refuse, self.refuse_times = n_frames, refuse, refuse_times
        self.commands, self.closed_by_client, self.consumed = [], [], 0
        self.done = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        c, _ = self.srv.accept()
        fb = FrameBuffer(); lock = threading.Lock()
        send = lambda f: (lock.acquire(), c.sendall(f.encode()), lock.release())
        links = {}; credits = threading.Semaphore(0); handles = iter(range(0x200001, 0x200100))
        def pump(cli_h, srv_h):
            for i in range(self.n_frames):
                if not credits.acquire(timeout=5): return
                p = self.payloads[i % len(self.payloads)]
                try:
                    send(Frame(MsgType.DATA, struct.pack("<III", srv_h, cli_h, 1) + p))
                except OSError:
                    return
        try:
            while True:
                data = c.recv(65536)
                if not data: break
                for f in fb.feed(data):
                    t, pl = f.type, f.payload
                    if t == MsgType.SESSION_OPEN:
                        send(Frame(MsgType.SESSION_ACCEPT, src=0x03FA, dst=0x03F9))
                    elif t == MsgType.SERVICE_OPEN:
                        send(Frame(MsgType.PARAMS, bytes.fromhex("d00700000a000000"), 3, 0x03FA, 0x03F9))
                        send(Frame(MsgType.SERVICE_REPLY, bytes.fromhex("00000000e903e90302"), 0, 0x03FA, 0x03F9))
                    elif t == MsgType.LINK_CREATE:
                        cid, ch = pl[:8], struct.unpack_from("<I", pl, 8)[0]
                        n = struct.unpack_from("<I", pl, 12)[0]
                        url = pl[16:16 + n].decode()
                        if any(url.endswith(r) for r in self.refuse) or self.refuse_times > 0:
                            self.refuse_times -= 1
                            send(Frame(MsgType.LINK_REFUSED, cid + struct.pack("<II", ch, 314050)))
                            continue
                        sid, sh = os.urandom(8), next(handles)
                        is_data = url.endswith("data/writer")
                        buf = 100 if is_data else 10
                        links[cid] = (sid, sh, ch, is_data)
                        send(Frame(MsgType.LINK_ACCEPT, sid + cid + struct.pack("<IIII", sh, ch, buf, buf) + b"\x01"))
                    elif t == MsgType.LINK_CONFIRM:
                        sid, sh, ch, is_data = links[pl[:8]]
                        if is_data:
                            send(Frame(MsgType.WRITER_READY, sid + struct.pack("<II", sh, ch)))
                        else:
                            send(Frame(MsgType.CREDIT_GRANT, sid + struct.pack("<IIII", sh, ch, 10, 0)))
                    elif t == MsgType.CREDIT_GRANT:
                        sid, sh, ch, _ = links[pl[:8]]
                        for _ in range(struct.unpack_from("<I", pl, 16)[0]): credits.release()
                        threading.Thread(target=pump, args=(ch, sh), daemon=True).start()
                    elif t == MsgType.CONSUMED:
                        self.consumed += 1; credits.release()
                    elif t == MsgType.DATA:
                        src, dst, cnt = struct.unpack_from("<III", pl, 0)
                        self.commands.append(pl[12:])
                        send(Frame(MsgType.RECEIVED, struct.pack("<III", dst, src, cnt)))
                        send(Frame(MsgType.CONSUMED, struct.pack("<III", dst, src, cnt)))
                    elif t == MsgType.LINK_CLOSED:
                        self.closed_by_client.append(pl[:8])
                        send(Frame(MsgType.LINK_CLOSED_ACK, pl[:12]))
                    elif t == MsgType.SESSION_CLOSE:
                        raise EOFError
        except (EOFError, OSError):
            pass
        finally:
            c.close(); self.srv.close(); self.done.set()
