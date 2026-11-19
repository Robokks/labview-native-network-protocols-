"""DataSocket read/write with NI's 'Simple DataSocket' example.

Read  : run Writer.vi, then  python datasocket_demo.py read
Write : stop Writer.vi, run Reader.vi, then  python datasocket_demo.py write
        (Reader.vi's graph shows a straight 0..39 ramp)
"""
import sys
import time

import nwstreams as nw

URL = "dstp://localhost/wave1"
mode = sys.argv[1] if len(sys.argv) > 1 else "read"

with nw.DataSocketClient() as ds:
    if mode == "read":
        ds.open(URL, mode=ds.MODE_READ)
        for _ in range(20):
            v = ds.read(URL, timeout=5)
            print(f"{len(v)} values: {v[0]:.4f}, {v[1]:.4f}, ... {v[-1]:.4f}")
    else:
        ds.open(URL, mode=ds.MODE_WRITE)    # raises DataSocketItemBusy if Writer.vi still runs
        for _ in range(100):
            ds.write(URL, [float(k) for k in range(40)], dtype=nw.Array(nw.DBL))
            time.sleep(0.1)
        print("wrote 100 ramps")
