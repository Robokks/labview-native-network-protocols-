# Changelog

All versions are alphas, verified live on LabVIEW 2026 (64-bit, Windows) with NI's example projects.

## Unreleased

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
