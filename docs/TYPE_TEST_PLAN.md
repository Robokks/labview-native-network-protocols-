# Data-type test plan (user builds and runs the VIs, Claude does the Python side)

Goal: learn the type-descriptor and flattened-data format for each LabVIEW type, so
`nwstreams` can build descriptors itself instead of copying them from a capture.

**Run 1 (agreed 2026-11-19): rows 1 `dbl`, 2 `i32`, 6 `str`, 7 `dbl1d`, 9 `clust` only.**

## VIs to build (two VIs in one project, like the Simple Network Streams example)

**TypeTest - Writer.vi**: one *Create Network Stream Writer Endpoint* per row below.
- writer name: `/types/<name>` (e.g. `/types/dbl`), **no reader URL** (it waits for a reader,
  like Target.vi's /data/writer)
- buffer size 10
- after connecting, write the 5 known values below (i = 0..4), then wait until Stop

**TypeTest - Reader.vi**: one *Create Network Stream Reader Endpoint* per row.
- reader name `/types/<name>-reader`, writer URL `//localhost/types/<name>`
- buffer size 10, read in a loop and show the values in indicators

Known values matter: they let Python's decoded values be checked exactly.

| # | name | LabVIEW type | value for element i |
|---|---|---|---|
| 1 | `dbl` | DBL scalar | `i + 0.5` |
| 2 | `i32` | I32 scalar | `-100 + i` |
| 3 | `u8` | U8 scalar | `250 + i` (250-254) |
| 4 | `sgl` | SGL scalar | `i * 0.25` |
| 5 | `bool` | Boolean | `i is odd` |
| 6 | `str` | String | `"Hello " + i` |
| 7 | `dbl1d` | 1D array DBL | `[0..9] + i` (10 elements) |
| 8 | `i32_2d` | 2D array I32 | 2×3 `[[1,2,3],[4,5,6]] * (i+1)` |
| 9 | `clust` | Cluster {I32 `n`, DBL `x`, String `s`} | `{i, i*1.5, "c" + i}` |
| 10 | `enum` | Enum U16 {Off, Low, High} | `i mod 3` |
| 11 | `time` | Timestamp | `2026-01-01 00:00:00 UTC + i s` |
| 12 | `wfm` | Waveform DBL, **no attributes** | t0 = 2026-01-01, dt = 0.001, Y = `[0..4] + i` |
| 13 | `var` | Variant holding a DBL | `i + 0.5` |

If 13 endpoints are too many at once, rows 1, 2, 6, 7 and 9 alone cover most of the
format. Split into several runs if needed.

## Capture procedure

1. Start the Wireshark capture on the loopback adapter, **before** either VI runs.
2. Run Writer.vi, then Reader.vi. Wait until all values appear, then stop both.
3. Stop the capture and save it as `types.pcapng`.
4. Tell me which rows were included.

## What Claude then does

1. Decode each link's descriptor and data from the capture, and check them against the table.
2. Write a descriptor and data encoder/decoder in `nwstreams` for these types.
3. With only Writer.vi running, connect Python as the reader for every type and check
   every value (Python takes Reader.vi's place).
