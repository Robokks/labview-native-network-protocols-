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
