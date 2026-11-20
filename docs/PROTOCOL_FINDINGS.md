# NI Network Streams wire protocol: findings from captures (2026-11-19)

Source: `10 1 ms.pcapng` and `20 ms.pcapng`, LabVIEW 2026 64-bit, NI "Simple Network Streams"
example (Host.vi + Target.vi in one LabVIEW.exe). Tools: `tools/pcap_flows.py`, `tools/c7_decode.py`.
Both captures start at the TCP SYN, so the full handshake and teardown are recorded and no new capture is needed.

Status: **confirmed live on 2026-11-19 against the unchanged Target.vi** (`tools/nwstream_client.py`):

| Test | Result |
|---|---|
| T02 handshake | PASS: data and command links accepted by LabVIEW |
| T03 waveform decode | PASS (structure): 2 wf × 2000 samples, dt 50 µs, t0 +0.1 s per element. Values not yet compared against Target's own data |
| Data rate (20 ms write speed) | 497 frames / 10 s, no loss, median gap 19.97 ms (18.0–23.2 ms) |
| T06 Python command | PASS: `Set Frequency 20` sent, acknowledged in 1.3 ms, and the Target front panel showed 20 |
| Clean teardown (part of T07) | PASS in both directions (0x0A / 0x0B) |
| T04 20 ms, 1 min (shortened from 5) | PASS: 2972 frames, 49.5/s, 0 lost/dup (t0 continuous), gap p50 19.99 / p99 22.51 / max 45.07 ms, 4 gaps > 30 ms |
| T05 10 ms, 1 min (shortened from 5) | PASS: 5869 frames, 97.8/s, 0 lost/dup, gap p50 10.05 / p99 12.31 / max 16.38 ms, 0 gaps > 20 ms |
| T07 reconnect to a running Target | open (the example's Target ends on disconnect) |

### Corrections learned from live runs
- **Handle order:** every message carries `[sender's handle][receiver's handle]`. The first offline reading had this reversed.
- The client must wait for **E1** before sending DF/E3. Sending them together stalls the session.
- **Empty DF every 2 s is a keep-alive** (matches the 2000 in the DF parameters).
- **0x09 = link refused**, payload `id8 hdl4 code4`. Code 314050 is seen when Target's endpoint no longer exists.
- Target.vi's loops end when the reader disconnects (example design, error -314220), so **Target.vi must be restarted before every client run**.
- **Teardown (confirmed both ways):** `0x0A` = "link closed", payload = the **receiver's** `id8 hdl4`. The
  receiver echoes the same 12 bytes as `0x0B`, then the client sends `E5`. Python-initiated close: LabVIEW
  answered both 0x0A with 0x0B in < 0.1 ms. LabVIEW-initiated close: Python acks with 0x0B. (Corrects the
  earlier reading of 0x0B as "destroy endpoint".)
- One run saw Target.vi stop normally (no error) about 0.2 ms after the command. It did not reproduce on the
  next run and is likely a race on the example's `Target Stop` global. Watch for it in the soak tests.
- **Locator (2343) fixed:** same rule as 59114, so wait for E1 before DF/E3, then send `DD fd020304` and `DD 1800`. The reply is a `DD` listing (~1060 B) containing NW_STREAMS + u16 BE port. LabVIEW.exe advertises it even with no VI running. `--port` is now optional.

## 1. Connections

| Step | TCP | Purpose |
|---|---|---|
| 1 | client → **2343** (NI Logos locator / `nilocator`) | asks `LogosClassifieds/CLASSADS`; reply lists services incl. `__NI_DEFAULT_STREAM_LISTENER_NAME__` / `NW_STREAMS` / `NI` / `1.1.0.0` / **port 0xE6EA = 59114** (u16 BE) |
| 2 | client → **59114** (LabVIEW.exe) | ONE connection carries **both** streams (command + data), multiplexed by endpoint handles |

59114 is assigned dynamically, so a client should query 2343 rather than hard-code the port.

## 2. Frame grammar (all bytes in both captures parse; no resync needed)

```
off len field
 0   2  magic 'C7' (43 37)
 2   4  payload length, u32 BIG-endian
 6   2  flags (0 except in 0xDF: client 0x0001, server 0x0003)
 8   1  message type
 9   1  version = 0x01
10   2  source context (BE)   03F9/03FA during session open, then 03E9
12   2  dest context (BE)
14   n  payload. Integers inside are LITTLE-endian, strings are u32-LE length + bytes
```

## 3. Session state machine (client = Host.vi side, server = Target.vi side)

| # | Dir | Type | Payload | Meaning |
|---|---|---|---|---|
| 1 | C→S | E0 | — | open session |
| 2 | C→S | DF | `d0070000 0a000000` (2000, 10) | session params (timeout ms?, ?) |
| 3 | C→S | E3 | str `__NI_DEFAULT_STREAM_LISTENER_NAME__/NW_STREAMS` + `e903 02` | bind service, context 0x03E9 |
| 4 | S→C | E1, DF, E4 `00000000 e903e903 02` | | accept, status 0 |
| 5 | C→S | **01** | `id8 hdl4 str(remote URL) str(local URL) u32 tdLen u32 tdLen TD[tdLen] trailer` | create link |
| 6 | S→C | **02** | `srvId8 cliId8 srvHdl4 cliHdl4 buf4 buf4 01` | link accepted, buffer sizes |
| 7 | C→S | **03** | `cliId8 cliHdl4 srvHdl4 01` | link confirm |
| 8 | reader→writer | **05** | `id8 rdHdl4 wrHdl4 credits4 00000000` | initial credit = reader buffer (data: 100, command: 10) |
| 9 | S→C | 04 | `srvId8 srvHdl4 cliHdl4` | writer ready (data link) |
| 10 | writer→reader | **06** | `dstHdl4 srcHdl4 count4 ?4 flat-data` | element(s) |
| 11 | reader→writer | **07** then **08** | `dstHdl4 srcHdl4 count4` | per element: 07 = received, 08 = consumed / credit returned *(inferred)* |
| 12 | closer→peer | 0A | `peerId8 peerHdl4` | link closed |
| 13 | peer→closer | 0B | same 12 bytes | close acknowledged |
| 14 | C→S | E5 | — | close session |

Ids are random 8-byte values per run. Handles are chosen by each side (`0x00600001…`, `0x00700001…`).
Link-creation order differs between the captures (data first in 20 ms, command first in 10 ms),
so the order does not matter.

The counts match the handover: 332 (10 ms) / 288 (20 ms) data frames, each answered by exactly one 07 and one 08.

## 4. Payloads

**Command (Host→Target, 0x06, 48 bytes):**
`hdls(8) 01000000 24000000 | 0d000000 "Set Frequency" | 00 0e000000 0002 04 | <f64 LE 10.1>`
The value 10.1 (`3333333333332440`) matches the Host front panel. The variant header fields aren't decoded yet; only the f64 is substituted.

**Data element (Target→Host, 0x06, 32626 bytes):** flattened 1-D array of waveform(DBL), little-endian
```
u32 nWaveforms = 2          (Sine, Square)
per waveform:
  t0   16 B LV timestamp (u64 frac 2^-64, i64 s), steps +0.1 s per element
  dt   f64 = 5e-05
  N    u32 = 2000
  Y    N × f64
  attr variant (274 B; last one runs to the end): NI_ExpXDimension, NI_ExpTimeStamp,
       NI_ExpStartTimeStamp, NI_ExpIsRelativeTime, ...
```
All 620 frames from both captures decode to 2 × 2000 samples with sine/square ranges of about ±1.1.

## 5. Open items

- The attribute-variant grammar isn't decoded (skipped by fixed length). It's fine for this VI but not general.
- The type-descriptor (TD) and trailer in 0x01 are replayed verbatim. They were not built from the typedefs.
- The semantics of 07 vs 08 and of the DF parameters are inferred, not proven.
- Whether the `ni.dex://<host>` name must match the PC name exactly hasn't been tested (`--url-host` option).
- T02–T07 still need live runs (see the handover's acceptance table).

## 6. Data types (from dbl / dbl array / string array / var captures, 2026-11-19)

All four captures re-encode **byte-exact** with `nwstreams.types` (descriptor and every element).

Descriptor (TD) = `u32 len (incl. itself)` + body:

| Type | Body |
|---|---|
| DBL | `02 04` |
| String | `00 09` |
| Variant | `02 09` |
| 1-D array | `00 08` + u16 ndims + TD(element) |
| Cluster | `01 08` + u16 nfields + TD(field)… |

LINK_CREATE tail = `u32 len(TD)` + TD + `u32 buf` ×2 + `u32 role` (1 = reader, 2 = writer) + flag + `01 32`,
then `02 96 03 af` only when flag = `03`. Flag `03` = fixed-size numeric (DBL, DBL array), `01` otherwise.

Element (after the 12-byte handles/count header):
- DBL scalar: raw 8 bytes, **no length prefix**
- everything else: `u32 length (incl. itself)` + value
- numeric array: raw elements, count implied by the length (no dimension field)
- string: `u32 n` + bytes, **padded to an even length** (this is the "00" after "Set Frequency")
- string array: `u32 count` + strings
- variant: `u32 len (incl. itself)` + TD body of the contents + value (seen: DBL, 1-D string array)
- `Stop` command: variant code `05 03` + 4 zero bytes (probably Boolean or I32, not decoded, kept raw)

Not yet verified: integer types, Boolean, enum, timestamp, clusters as stream data, 2-D arrays,
numeric arrays inside variants, waveform descriptor internals (`02 40`/`03 40` codes).

**Live check, variant Target (2026-11-19):** `nwstreams` library (not the prototype script):
locator → session → `open_reader(..., VARIANT)` + `open_writer(..., MESSAGE)`. 496 elements / 10 s,
each decoded as 100 strings of random values in 0–1, which matches the Target front panel (user confirmed).
Set Frequency sent, both links closed with acknowledgement.

### Connecting to a *waiting* LabVIEW endpoint (created without remote URL), open
LabVIEW answers LINK_CREATE with **0x0C** instead of 0x02:
`lvId8 ourId8 lvHdl4 ourHdl4 str(our own local URL)`, with no buffer sizes.
Replying 0x03 (confirm) gets **0x09 refused, code 0xFFFB3494 = -314220** (signed). The correct
reply is unknown, so a LabVIEW↔LabVIEW capture of this case is needed.

### Cluster capture (`cluster data.pcapng`)
Data type: cluster {DBL, Boolean, DBL[], String[], Boolean[], error cluster {Boolean, I32, String},
Path, DBL[,], Variant}. Descriptor generated by `nwstreams` byte-exact, `parse_td` recovers it from
the capture, and all **3086 elements re-encode byte-exact**. Values match the Target front panel
(5, true, "sfs5d444", [true], error {true, 65646, ""}, 11×11 array, empty variant).

New rules:
- Codes: Boolean `02 01`, I32 `00 03`, Path `05 09`
- **Boolean = 2 bytes** (`ff 00` = true), so every item stays even-sized
- Arrays nested in a cluster carry `u32` dimension sizes (unlike the raw top-level DBL[] form)
- 2-D array: two `u32` dimension sizes, then row-major values
- Path: `u16 type (0)` + `u16 count` + count × (`u8 len` + name), padded to even
- **Empty variant** = `05 03 00 00 00 00`, which is also what the `Stop` command carries
- **Correction from live data:** Booleans *inside arrays* are 1 byte each, and the array is padded to even
  (a scalar Boolean is still 2 bytes). The capture only had 1-element Boolean arrays, which hid this.
- **Live check, cluster Target (2026-11-19):** 487 elements / 10 s, 0 decode errors, median gap 19.96 ms
  (max 57.7 ms), Set Frequency sent, both links closed with acknowledgement. Values included a 5- and
  6-element Boolean array, error {true, 65610, "hjgfyjfydtydgdgd"} and a 13×15 2-D array.

## 7. Python as Target, LabVIEW as Host (2026-11-20)

`examples/python_target.py` (offline bundle `python_target_bundle.zip`) answers Host.vi's locator lookup on
TCP 2343 and serves the stream. **Confirmed live:** the unchanged Host.vi on a second PC (Target IP
Address = the Python PC, over a direct USB-Ethernet link) plotted Python's sine and square waves, with
about 50 elements/s and the initial `Set Frequency 10.1` received.

- **Writer-ready ordering:** the Target (writer) must send `0x04` WRITER_READY **when the data link is
  confirmed (`0x03`)**. LabVIEW's reader waits for it before granting credits (`0x05`). Sending `0x04`
  only after credits (which suited the Python client) leaves LabVIEW waiting forever.
- Port 2343 on the Python machine must be free: NI's "NI PSP Service Locator" (service `lkClassAds`)
  holds it on every address. On the Host's PC the locator must keep running, because LabVIEW's Network
  Streams engine needs it to create endpoints. If it was stopped when Network Streams was first used,
  restart LabVIEW.
- Windows Firewall must allow inbound TCP 2343 and the stream port on the Python machine.
- **Pacing:** wait the period *after* each write (like LabVIEW's "Wait (ms)" in Target.vi), not a fixed
  catch-up schedule. With a fixed 20 ms schedule Python sent exactly 50 elements/s, Host.vi (20 ms wait +
  read + draw, about 21–22 ms per loop) fell behind, and Read Backlog filled up to its 100-element buffer
  (no loss, because credit flow control paused Python, but about 2 s of latency). With the wait after
  each write (about 45/s), Read Backlog stays about 0 at "Slow (20 ms)" (confirmed live).
- Host.vi's `Set Frequency` commands arrive on every knob change (24 in about 1 s while turning) and
  are applied to the next element.
- **LabVIEW 2020 (32-bit) Host.vi** works with the same Python Target unchanged (2026-11-20): same
  lookup, link, credit and writer-ready sequence as LabVIEW 2026 (64-bit). Waves plotted, frequency
  commands applied, Read Backlog 0 at "Slow (20 ms)".
- **LabVIEW 2016 (32-bit) Target.vi** on another PC works with the Python **client** (Python as Host)
  (2026-11-20): that PC's locator reported NW_STREAMS on port **59115**; 100 elements of 2 x 2000
  samples at a median 20.7 ms; `Set Frequency 25` applied on Target.vi. For a Target on another PC
  pass `url_host=<that PC's address>`, so the endpoint URLs read `ni.dex://<target>/data/writer`.
- **LabVIEW 2016 (32-bit) Host.vi** works with the Python Target (2026-11-20): it sends the locator
  lookup twice (2020/2026 once), then the same link, credit and writer-ready sequence. Waves plotted,
  frequency knob applied (user confirmed).

## 8. Contexts and JKI Network Stream (2026-11-20, LabVIEW 2020)

- A **context** is the part after `:` in `ni.dex://host:CTX/endpoint`. The session's `E3` service is
  `<ctx lower-case>/NW_STREAMS` (the default context is `__NI_DEFAULT_STREAM_LISTENER_NAME__/NW_STREAMS`).
- Every context is its own locator entry: `str(ctx) str("NW_STREAMS") str("NI") str("1.1.0.0") ... u16 BE port`;
  all contexts of one LabVIEW share its listener port (59114 here).
- LabVIEW **registers** contexts with NI's locator over local IPC, not TCP or UDP (nothing in the capture), so
  Python can't register one; it can connect to any registered context with the locator running.
- JKI's 2-way endpoint = context "My Name" with streams `<me>_Writer -> <remote>_Reader` and
  `<remote>_Writer -> <me>_Reader`, buffer 20, element `Cluster(Cluster(String, Variant, I32), Boolean, I32)`
  = {{Command, Arguments, Msg Id}, Wait for Ack?, Message ID}; ack = `(("Ack Response", empty, 0), False, id)`.
  Standard link handshake (no `0x0C`) even though the RT endpoint has no remote address.
- Live: Python as "PC" ↔ LabVIEW 2020 RT_Main.vi: 3 commands acknowledged, 13 received; RT_Main.vi showed
  "Cmd From Python" (user confirmed).
