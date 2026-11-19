# nwstreams

Experimental pure-Python clients for three LabVIEW / NI communication protocols, worked out from
packet captures and tested live against LabVIEW 2026 (64-bit, Windows):

| Protocol | Class | Python → LabVIEW | LabVIEW → Python |
|---|---|---|---|
| **Network Streams** | `nwstreams.connect()` → `Reader` / `Writer` | ✓ | ✓ |
| **DataSocket** (`dstp://`, DataSocket Server, TCP 3015) | `nwstreams.DataSocketClient` | ✓ | ✓ |
| **Network-published Shared Variables** (`psp:`, Shared Variable Engine) | `nwstreams.SharedVariableClient` | ✓ (DBL) | ✓ (DBL) |

No dependencies, standard library only, Python ≥ 3.9.

> **Unofficial.** This project isn't affiliated with, endorsed by or supported by National Instruments
> or Emerson. The protocols are proprietary and undocumented. Everything here was inferred from captures
> and may break with other LabVIEW versions or configurations. Use it for test and lab systems.
> **Don't use it to control safety-relevant equipment.**

## Network Streams

```python
import nwstreams as nw

# Python in place of Host.vi of NI's "Simple Network Streams" example (Target.vi running)
with nw.connect() as s:                     # finds the NW_STREAMS port via the locator (TCP 2343)
    data = s.open_reader("data/writer", "data/reader", nw.WAVEFORM_ARRAY, wait_for_peer=30)
    cmd = s.open_writer("command/reader", "command/writer", nw.MESSAGE)
    cmd.write(("Set Frequency", 20.0))
    for _ in range(100):
        sine, square = data.read(timeout=5)  # list of nw.Waveform (t0, dt, y)
```

Python connects to endpoints that LabVIEW created and waits on (the client role). Acting as the
endpoint LabVIEW connects to (Python as Target.vi) isn't supported yet.

### Data types

Type descriptors are generated from these building blocks, and decoded types round-trip byte for byte
against captures:

`DBL`, `I32` and `U32` (inside clusters), `BOOLEAN`, `STRING`, `PATH`, `VARIANT`, `Array(t, ndims)`,
`Cluster(...)`, `ERROR_CLUSTER`, `MESSAGE` (the example's command cluster), `WAVEFORM_ARRAY`
(descriptor copied from a capture).

`nwstreams.parse_td()` turns a descriptor from the wire into one of these types. Types not seen yet
(for example U8, SGL, I64, enum, timestamp, and I32 as a stream's own type) raise
`NotImplementedError` rather than guessing.

## DataSocket

```python
with nw.DataSocketClient() as ds:           # DataSocket Server on 127.0.0.1:3015
    ds.open("dstp://localhost/wave1")       # read mode, receives pushed values
    print(ds.read("wave1"))
    ds.open("myitem", mode=ds.MODE_WRITE)
    ds.write("myitem", [1.0, 2.0, 3.0], dtype=nw.Array(nw.DBL))
```

The server allows one writer per item. A write-mode open of a busy item raises `DataSocketItemBusy`.

## Shared Variables

```python
with nw.SharedVariableClient("Server Variables") as sv:   # one client per variable library
    sv.open(r"psp:\\localhost\Server Variables\Monitor Value 1")
    print(sv.read("Monitor Value 1"))
    sv.write("Monitor Value 1", 0.25)
```

Only DBL values are supported so far. The Shared Variable Engine port defaults to 59110; pass `port=`
if yours differs.

## Tests

```
pip install pytest
pytest
```

The tests replay protocol fixtures cut from captures (machine names and user paths replaced) and run
the clients against stand-in servers, so no LabVIEW is needed.

## Status

Version 0.1.1, alpha. Verified live on one Windows PC with LabVIEW 2026. Protocol notes are in the
project's `docs/` folder: `PROTOCOL_FINDINGS.md`, `DATASOCKET_FINDINGS.md` and
`SHARED_VARIABLE_FINDINGS.md`.

## License

MIT. See [LICENSE](LICENSE).
