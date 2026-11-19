"""Python in place of Host.vi of NI's 'Simple Network Streams' example.

1. In LabVIEW, run 'Simple Network Streams - Target.vi' only.
2. python simple_host.py --freq 20 --seconds 10
"""
import argparse
import statistics
import time

import nwstreams as nw

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, help="default: ask the NI locator on TCP 2343")
ap.add_argument("--freq", type=float, help="send 'Set Frequency' to Target.vi")
ap.add_argument("--seconds", type=float, default=10)
a = ap.parse_args()

with nw.connect(a.host, a.port, retry_for=30) as s:
    print(f"connected to LabVIEW on port {s.port}")
    data = s.open_reader("data/writer", "data/reader", nw.WAVEFORM_ARRAY, wait_for_peer=30)
    if a.freq is not None:
        cmd = s.open_writer("command/reader", "command/writer", nw.MESSAGE)
        cmd.write(("Set Frequency", a.freq))
        print(f"sent Set Frequency = {a.freq}")
    gaps, last, n, end = [], None, 0, time.monotonic() + a.seconds
    try:
        while time.monotonic() < end:
            wfs = data.read(timeout=5)
            now = time.perf_counter()
            if last:
                gaps.append((now - last) * 1e3)
            last, n = now, n + 1
            if n == 1 or n % 100 == 0:
                w = wfs[0]
                print(f"#{n}: {len(wfs)} waveforms, N={len(w.y)}, dt={w.dt}, t0={w.t0:.3f}")
    except nw.StreamClosed:
        print("Target.vi closed the stream")

if gaps:
    print(f"{n} elements, gap ms median {statistics.median(gaps):.2f}, max {max(gaps):.2f}")
