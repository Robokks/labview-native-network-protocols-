# Network-published Shared Variables (NI-PSP): findings (2026-11-19)

Source: `shared variable.pcapng` (NI example `Data Communication\Shared Variable`, Server VI +
Variable Nodes client, both in one LabVIEW), plus a live Python probe (`tools/sv_probe.py`).

Variables are hosted by the **Shared Variable Engine** (`tagsrv.exe`, TCP **59110** on this PC).
LabVIEW connects to it with the **same `C7` Logos framing as Network Streams** (see PROTOCOL_FINDINGS.md §2).
A `psp:\\host\library\variable` URL in the DataSocket API uses this same protocol (not dstp/3015).

## Session
| Step | Frame | Payload |
|---|---|---|
| 1 | `E0` → `E1` | session open (contexts 042A/03EB) |
| 2 | `DF` | `d00700000a000000` |
| 3 | `E3` → `E4` | service `"<library>/PROCESS"` + `e9 03 01`, i.e. the SVE process for that library |
| 4 | `DD` (optional) | directory lookup: host + library + item names → reply says items live in `TaggerCtrlEnv/CONTROLENV_REALTIME` |
| 5 | `E3` (flags 1) → `E4` | service `"TaggerCtrlEnv/CONTROLENV_REALTIME"` + context `ea 03 02` + user block `"(Nobody)"` (replayed verbatim) |

Item messages then use context `03EA`. All integers are little-endian.

## Item messages
| Type | Dir | Payload | Meaning |
|---|---|---|---|
| `01` | C→S | `u32 itemId` + str `\\HOST\library\variable` + `0c000000 0000` | open item (client picks itemId) |
| `65` | S→C | `u32 itemId, u32 handle` | opened; server assigns the handle |
| `66` | S→C | `u32 itemId` + `04 1e 00 00 33 00 00 00` + `u32 bufferElems` + … | properties; Monitor Value 2 shows buffer 100 (`64000000`) |
| `68` (fl 3) | S→C | `u32 itemId` + 20 zero bytes | initial status |
| `02` | C→S | `u32 handle, u32 itemId, u16 flags` | subscribe; flags seen `0x84` (start), `0x80`, `0x2080` |
| `68` (fl 8) | S→C | `u32 itemId` + **value record** | **pushed update** |
| `03` | C→S | `u32 handle, u32 itemId, u32 1` + **value record** | **write** |
| `04` | C→S | `u32 handle, u32 itemId` | close item |
| `6A` | S→C | `u32 itemId` + 12 zero bytes | item closed |

**Value record (DBL):** `u32 12` (bytes that follow) + `u32 3` + `f64`. The meaning of `3` is unknown
(type tag or quality); only DBL has been seen.

## Verified live
- Python opened both example variables (handles 5, 6), subscribed, and received **37 updates in 8 s**
  from Shared Variable Server.vi (values 0–1, matching the client gauges). Read only, no writes sent.
- **Write confirmed live:** with Server.vi stopped, Python wrote Monitor Value 1 = 0.25 and
  Monitor Value 2 = 0.75 (`03` + DBL record). The engine pushed both back in the same session, and a
  second, independent Python session read 0.25 / 0.75 from the engine. The LabVIEW DataSocket API
  client (psp: URLs) showed 0.25 and 0.75 on its gauges (user confirmed).
- Not yet tested: other data types, the meaning of the
  subscribe flags, and finding the SVE port through the locator instead of hard-coding 59110.

## Library
`nwstreams.SharedVariableClient(library, port=59110)`: `open(name or psp URL)`, `read()`, `write()` (DBL),
`close()`. Tests replay the capture (hostname scrubbed) and run against a stand-in engine. Live check
2026-11-19: read 0.25 / 0.75 (left by the earlier probe), wrote 0.4 / 0.6, and a new session read
0.4 / 0.6. The LabVIEW client gauges showed 0.4 and 0.6 (user confirmed).

## Data types (2026-11-20, LabVIEW 2026, library `TypeTest Variables`, 25 variables)
Verified in BOTH directions, 25/25 each. Before each direction every variable was first set to a
different decoy value (confirmed by an independent session), so a failed write could not pass.
LabVIEW side: `labview/TypeTest/TypeTest SV Writer.vi` / `TypeTest SV Reader.vi` (DataSocket API with
`psp:\localhost\TypeTest Variables\<name>?sync=true`). Driver: `tools/sv_types.py`.

**Value record** := `u32 n` (bytes that follow) + `u32 kind` + data. Low 16 bits of kind:

| kind | types | data |
|---|---|---|
| `0x0003` | DBL, SGL, I8, I16, I32, U8, U16, U32 | always f64; high 16 bits = type: 0 DBL, 1 I8, 2 I16, 3 I32, 4 U8, 5 U16, 6 U32, 7 SGL |
| `0x0002` | Boolean | 1 byte, 01 = true |
| `0x1005` | String | u32 length + bytes, no padding |
| `0x100F` | Timestamp | u64 fraction + i64 seconds since 1904 |
| `0x1013` | I64, U64, EXT, CSG, CDB, Waveform, arrays, clusters, variants | self-describing variant (u32 len + descriptor + value) exactly as DataSocket: 2-D arrays column-major, variant attributes in the value's descriptor |

**Engine port moves.** tagsrv listened on 59110 on 2026-11-19 and on **59111** on 2026-11-20 (59110 was
then `lktsrv`). `SharedVariableClient` now tries 59110-59120 and keeps the first port whose engine
serves the library (`client.port`). The NI locator query used for Network Streams returned no
listing for this, so the port is still not looked up properly.

**LabVIEW side:** a DataSocket Read/Write with a timeout on a `psp:` URL fails with error 1183 unless
the URL ends in `?sync=true`.

Variables were created with `vi.lib\Utility\Variable\AddSharedVariableToLibrary.vi` and deployed with
the Application method `Library:Deploy Library` (`labview/TypeTest/Builder/TypeTest SV Library Builder.vi`).
