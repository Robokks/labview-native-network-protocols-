# Changelog

All versions are alphas, verified live on LabVIEW 2026 (64-bit, Windows) with NI's example projects.

## 0.4.1

Follow-ups reported by an integration consumer. Offline tests only; not re-verified live.

### Changed
- **Shared Variables:** a value record the client can't decode is now `nw.RawRecord(kind, data)`, a
  frozen dataclass, instead of the tuple `("raw", kind, data)`.
  **Backward compatibility:** code that detected the tuple (`isinstance(v, tuple) and v[0] == "raw"`)
  no longer matches and would treat the record as a value. Use `isinstance(v, nw.RawRecord)`.
- **Network Streams:** `Reader.drain()` and `Reader.read()` decode an element before acknowledging it.
  An element the decoder fails on is returned as `nw.RawElement(data, error)` in its place and is still
  acknowledged, so the writer's credits stay correct. Before, a decode error in `drain()` discarded
  every element of that call, all of them already acknowledged, and `read()` raised the decoder's
  exception after acknowledging the element. Code that caught the decoder's exception from `read()`
  should check for `RawElement` instead.

### Added
- **DataSocket:** `client.other` holds `nw.DataSocketMessage`, still the 2-tuple `(dtype, value)`, plus
  `.op`, `.item` and `.error`. A message that can't be decoded (dtype `None`, value = raw bytes) now
  carries the item it belongs to when the message names one.

### Fixed
- **DataSocket:** a malformed message (struct, index or Unicode errors, not only unknown descriptor
  codes) no longer stops the receive thread; it goes to `client.other`.
- **Docs:** README and Getting Started no longer say a Python DataSocket reader blocks LabVIEW writers that
  open the item later. That held for read mode 7, the default before 0.3.1; mode 3 has no write bit.

## 0.4.0

### Added
- **Network Streams contexts:** `connect(..., context=, local_context=)`, `locate_port(..., context=)`
  and `list_contexts()`. Endpoints in named contexts (`ni.dex://host:CTX/name`) are reachable with NI's
  locator running.
- **JKI Network Stream:** `nwstreams.jki` (`JKIEndpoint`, `JKI_MESSAGE`). Verified live with JKI's 2-way
  example in LabVIEW 2020: acknowledged commands both ways. 5 tests against a capture of the example.

## 0.3.1

### Changed
- **DataSocket read mode is now 3** (read + live updates) instead of LabVIEW's 7. Mode 7 includes the
  write bit, which a DataSocket Server on **another PC** refuses by default. Mode 3 works locally and
  remotely. `DataSocketClient.MODE_LABVIEW_READ` (7) is still available.

### Added
- `DataSocketAccessDenied`: raised when the server refuses an open with opcode 51 (for example a remote
  write without DefaultWriters permission). It used to show up as a misleading `DataSocketItemBusy`.
- Verified with **LabVIEW 2020 (32-bit)** across two PCs: Network Streams (LabVIEW Host.vi ↔ Python
  Target) and DataSocket in both directions (Writer.vi → Python, Python → Reader.vi).
- Verified with **LabVIEW 2016 (32-bit)**: its Target.vi on another PC with Python as Host (stream data
  plus Set Frequency), its Host.vi with the Python Target, and DataSocket both
  ways (Writer.vi → Python, Python → Reader.vi). Docs: pass `url_host=` for a Target on another PC.

## 0.3.0

### Added
- **Python as Network Streams Target** (`examples/python_target.py`): a locator responder on TCP 2343
  plus a stream server, so the unchanged LabVIEW Host.vi connects to Python. Verified live across two
  PCs: waveforms plotted on Host.vi, `Set Frequency` commands applied, Read Backlog about 0 at 20 ms.
- Docs: Getting Started section 5 (two-PC setup, firewall, service locator) and Protocol Findings
  section 7 (writer-ready ordering, pacing).

## 0.2.0

### Added
- **Many more LabVIEW data types:** I8, I16, I64, U8, U16, U64, SGL, EXT (IEEE quad), CSG, CDB,
  Timestamp (`nw.Timestamp`), enums (as their integer), Waveform DBL with decoded attributes
  (`nw.WAVEFORM`), and variant attributes (`nw.LVVariant`).
- **DataSocket:** all 25 test types verified LabVIEW → Python and Python → LabVIEW. Column-major
  n-D arrays and attributed variants are handled.
- **Shared Variables:** all 25 types readable and writable (`write(name, value, dtype=…)`), not just DBL.
- **Shared Variables:** the engine's port is found automatically by trying 59110–59120.
- **Polling helpers:** `drain()` on `Reader`, `DataSocketClient` and `SharedVariableClient` returns
  everything available now without waiting. `DataSocketClient.closed` and
  `SharedVariableClient.closed` events signal a lost connection.
- **Documentation:** API reference (`docs/API.md`), getting-started and troubleshooting guide
  (`docs/GETTING_STARTED.md`), and this changelog.

### Fixed
- A Network Streams element shorter than 4 bytes crashed the session's receive thread.
- One failing link no longer stops the whole session.
- Top-level 1-byte numbers are sent unpadded.

## 0.1.1
- MIT license added. No code changes.

## 0.1.0
- First release: Network Streams client (locator, session, reader/writer links with credit flow
  control, clean teardown), DataSocket client, Shared Variable client (DBL), generic LabVIEW type
  descriptors, and offline tests with scrubbed capture fixtures.
