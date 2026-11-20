# nwstreams

Experimental pure-Python clients for three LabVIEW / NI communication protocols, worked out from
packet captures and tested live against LabVIEW 2026 (64-bit, Windows):

| Protocol | Class | Python → LabVIEW | LabVIEW → Python |
|---|---|---|---|
| **Network Streams** | `nwstreams.connect()` → `Reader` / `Writer` | ✓ | ✓ |
| **DataSocket** (`dstp://`, DataSocket Server, TCP 3015) | `nwstreams.DataSocketClient` | ✓ | ✓ |
| **Network-published Shared Variables** (`psp:`, Shared Variable Engine) | `nwstreams.SharedVariableClient` | ✓ | ✓ |

No dependencies, standard library only, Python ≥ 3.9.

> **Unofficial.** This project isn't affiliated with, endorsed by or supported by National Instruments
> or Emerson. The protocols are proprietary and undocumented. Everything here was inferred from captures
> and may break with other LabVIEW versions or configurations. Use it for test and lab systems.
> **Don't use it to control safety-relevant equipment.**

## Install

```powershell
py -m pip install https://github.com/Robokks/labview-native-network-protocols-/releases/download/v0.2.0/nwstreams-0.2.0-py3-none-any.whl
```

On Windows, `pip` on its own is often not on PATH; `py -m pip` (or `python -m pip`) always works.

## Documentation

| Document | Contents |
|---|---|
| [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) | Step by step with NI's example projects, plus troubleshooting |
| [docs/API.md](docs/API.md) | Every class, method, parameter, exception and data type |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each version |
| [docs/PROTOCOL_FINDINGS.md](docs/PROTOCOL_FINDINGS.md) | Network Streams wire protocol, as decoded |
| [docs/DATASOCKET_FINDINGS.md](docs/DATASOCKET_FINDINGS.md) | DataSocket wire protocol |
| [docs/SHARED_VARIABLE_FINDINGS.md](docs/SHARED_VARIABLE_FINDINGS.md) | Shared Variable (NI-PSP) wire protocol |
| [docs/TYPE_TEST_PLAN.md](docs/TYPE_TEST_PLAN.md) | How data types are captured and verified |

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

Here Python connects to endpoints that LabVIEW created and waits on (the client role).

### Python as Target.vi (LabVIEW Host.vi connects to Python)

`examples/python_target.py` takes the server role. It answers Host.vi's locator lookup on TCP 2343 and
serves `/data/writer` (waveforms) and `/command/reader` (`Set Frequency`). It was verified live with the
**unchanged** Host.vi on another PC (Host.vi's Target IP Address set to the Python PC): waves plotted,
frequency commands applied, and Read Backlog about 0 at 20 ms.

```powershell
py examples\python_target.py --stream-port 59200 --seconds 600
```

Requirements on the Python PC: port 2343 must be free. NI's **NI PSP Service Locator** service holds it on
every address, so run the script on a PC without NI software, or stop that service while testing. Inbound
TCP 2343 and the stream port must be allowed in the firewall. See
[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md#5-python-as-targetvi-labview-hostvi-connects-to-python).

### Data types

Type descriptors are generated from these building blocks, and decoded types round-trip byte for byte
against data LabVIEW 2026 sent:

| Kind | Types |
|---|---|
| Integers | `I8`, `I16`, `I32`, `I64`, `U8`, `U16`, `U32`, `U64` (enums arrive as their U8/U16 number) |
| Floating point | `SGL`, `DBL`, `EXT` (read as a Python float) |
| Complex | `CSG`, `CDB` (Python `complex`) |
| Time | `TIMESTAMP` → `nw.Timestamp` (`.unix`, `.lv`, `from_unix()`) |
| Other | `BOOLEAN`, `STRING`, `PATH`, `VARIANT` (attributes → `nw.LVVariant`) |
| Containers | `Array(t, ndims)`, `Cluster(...)`, `ERROR_CLUSTER`, `MESSAGE` |
| Waveform | `WAVEFORM` (DBL) → `nw.Waveform` with `.attributes`; `WAVEFORM_ARRAY` |

`nwstreams.parse_td()` turns a descriptor from the wire into one of these types. Unknown descriptors
raise `NotImplementedError` rather than guessing.

**Verification state.** DataSocket and Shared Variables: every type above (25 cases) verified in both
directions against LabVIEW. Network Streams: every type verified inside a Variant stream (LabVIEW → Python); as a
stream's own element type only DBL, DBL[], String[], Variant, the example's clusters and I8 so far.

## DataSocket

```python
with nw.DataSocketClient() as ds:           # DataSocket Server on 127.0.0.1:3015
    ds.open("dstp://localhost/wave1")       # read mode, receives pushed values
    print(ds.read("wave1"))
    ds.open("myitem", mode=ds.MODE_WRITE)
    ds.write("myitem", [1.0, 2.0, 3.0], dtype=nw.Array(nw.DBL))
```

The server allows one writer per item. A write-mode open of a busy item raises `DataSocketItemBusy`.
A Python read-mode open also blocks a LabVIEW writer that opens the item later (LabVIEW error 1142),
so start LabVIEW writers first.

Any type in the table above can be written and read (`dtype=` when it can't be inferred). 2-D and
larger arrays are column-major on the DataSocket wire and a variant's attributes travel in the value's
type descriptor; the client handles both, so values look the same as in LabVIEW.

## Shared Variables

```python
with nw.SharedVariableClient("Server Variables") as sv:   # one client per variable library
    sv.open(r"psp:\\localhost\Server Variables\Monitor Value 1")
    print(sv.read("Monitor Value 1"))
    sv.write("Monitor Value 1", 0.25)
```

All types in the table above are supported (`write(name, value, dtype=...)`, e.g. `dtype=nw.I16`).
The engine's port is found automatically (it moved between 59110 and 59111 on the test PC); pass
`port=` to skip the search.

## Polling instead of blocking

Every client queues what LabVIEW pushes. `read()` waits for the next value, while `drain()` returns
everything available now without waiting, which suits a GUI or control loop:

```python
for value in ds.drain("wave1"):        # also Reader.drain() and SharedVariableClient.drain(name)
    process(value)
if ds.closed.is_set():                 # connection lost (DataSocketClient / SharedVariableClient)
    reconnect()
```

## Tests

```powershell
py -m pip install pytest
py -m pytest
```

The tests replay protocol fixtures cut from captures (machine names and user paths replaced) and run
the clients against stand-in servers, so no LabVIEW is needed.

## Status

Version 0.2.0, alpha. Verified live on one Windows PC with LabVIEW 2026. See
[CHANGELOG.md](CHANGELOG.md) for what's new.

Not supported yet: connecting to LabVIEW endpoints created without a remote URL (they answer with an
undecoded `0x0C` handshake). The Python Target is an example script, not yet a library class.

## License

MIT. See [LICENSE](LICENSE).
