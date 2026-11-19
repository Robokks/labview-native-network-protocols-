r"""Read and write network-published Shared Variables of NI's 'Shared Variable' example.

Deploy 'Server Variables.lvlib' (and run 'Shared Variable Server.vi' to see live values).

    python shared_variable_demo.py              # read for 5 s
    python shared_variable_demo.py 0.25 0.75    # write (stop Server.vi first, or it overwrites them)
"""
import sys
import time

import nwstreams as nw

NAMES = ["Monitor Value 1", "Monitor Value 2"]

with nw.SharedVariableClient("Server Variables") as sv:
    for n in NAMES:
        sv.open(rf"psp:\\localhost\Server Variables\{n}")
    if len(sys.argv) == 3:
        for n, v in zip(NAMES, map(float, sys.argv[1:])):
            sv.write(n, v)
        time.sleep(0.5)                      # let the engine push the new values back
        print("written; engine now reports:", {n: sv.latest(n) for n in NAMES})
    else:
        end = time.time() + 5
        while time.time() < end:
            for n in NAMES:
                try:
                    print(f"{n}: {sv.read(n, timeout=1):.6f}")
                except TimeoutError:
                    pass
