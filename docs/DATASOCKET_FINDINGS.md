# DataSocket (dstp://) wire protocol: findings (2026-11-19)

Source: `datasocket.pcapng` (NI "Simple DataSocket" example, Writer + Reader in one LabVIEW,
item `wave1`), plus live tests against the NI DataSocket Server (`cwdss`, TCP **3015**) on this PC.
Code: `nwstreams/src/nwstreams/datasocket.py` (`nwstreams.DataSocketClient`).

## Framing
Each message is a **flattened LabVIEW variant**: `u32 length (incl. itself)` + type descriptor +
value, using the same type grammar as Network Streams (see PROTOCOL_FINDINGS.md §6). The value is a
cluster whose first I32 is the opcode. New type code: **U32 = `01 03`**.

## Messages
| Opcode | Direction | Value | Meaning |
|---|---|---|---|
| 1, 2 | C→S | `(1, 3)`, `(2, 3)` | hello, protocol version 3 |
| 9 | S→C | `(9, 3)` | hello ack |
| 4 | C→S | `(4, item, mode)` | open item; mode 4 = LabVIEW "Write", 7 = "Read" (server ignores other values such as 100) |
| 56 | S→C | `(56, item)` | sent before the open ack (meaning unknown) |
| 12 | S→C | `(12, item, mode)` | open ack |
| 13 | C→S | `(13, item, value, seq:U32)` | write |
| 14 | S→C | `(14, seq:U32, 1)` | write ack |
| 6 | S→C | `(6, item, value)` | **new value pushed to readers** (confirmed live, ~10/s from Writer.vi) |
| 10 | S→C | `(10,)` | unknown, seen once |
| 5 | C→S | `(5, item)` | close item |
| 3 | C→S | `(3,)` | disconnect |

The item name has no `dstp://host/` prefix on the wire (`wave1`).

## Behaviour
- Opening an item pushes its current value immediately (0 for a new item).
- A write isn't echoed back to the writing connection, only to other readers.
- Only one writer per item: while LabVIEW Writer.vi had `wave1` open, a second open in mode 4
  got **no reply at all** (no error). An open in mode 7 was still acknowledged.
- Two LabVIEW VIs in the same LabVIEW instance share one connection, so the capture never showed
  opcode 6. It was found with a live Python reader.

## Verified live (Python ↔ DataSocket Server)
- Read `wave1` from LabVIEW Writer.vi: 40-DBL arrays, about every 100 ms.
- Write → read across two Python connections, all values exact: DBL, String, DBL[], String[],
  Cluster{DBL, String}.
- **Python → LabVIEW:** with Writer.vi stopped, Python wrote a 0..39 DBL ramp to `wave1` (149 writes / 15 s,
  all acknowledged). LabVIEW Reader.vi plotted the straight 0–39 line (user screenshot, 2026-11-19).

## Data types (2026-11-20, LabVIEW 2026, `labview/TypeTest/TypeTest DS Writer.vi` / `DS Reader.vi`)
25 types verified in BOTH directions (LabVIEW -> Python and Python -> LabVIEW, 25/25 each):
I8, I16, I32, I64, U8, U16, U32, U64, SGL, DBL, EXT, CSG, CDB, Boolean, String, Timestamp,
Waveform DBL, 1D U8/I16/DBL/String/Timestamp arrays, 2D I32 array, cluster {I32, DBL, String},
variant with an attribute. Driver: `tools/ds_types.py` (modes roundtrip / write / read).

- **2D and larger arrays are column-major on the wire** (dims are still rows, columns): LabVIEW
  [[1,2,3],[4,5,6]] arrives as dims 2,3 then 1,4,2,5,3,6. Network Streams don't do this.
- **A variant with attributes is unwrapped**: the value field carries the contents' type with the
  attributes in its descriptor (`02 40` blocks before `02 04`), not a variant (`02 09`). The client
  returns it as `LVVariant(value, attributes)` and writes `LVVariant` the same way.
- **A Python open in mode 7 blocks a LabVIEW writer**: LabVIEW's DataSocket Open (Write) got error
  1142 on items a Python client already had open in mode 7. Open order matters: LabVIEW writer first.
- Tests restarted `cwdss.exe` before each run so no item value could carry over (not checked whether the server keeps values after clients disconnect).
