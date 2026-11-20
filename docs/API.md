# API reference (nwstreams 0.4.1)

```python
import nwstreams as nw
```

All three clients use background receive threads. Values the LabVIEW side pushes are queued until you
`read()` or `drain()` them. Every client is a context manager, and leaving the `with` block closes it
cleanly.

---

## Network Streams

### `nw.connect(host="127.0.0.1", port=None, *, url_host=None, retry_for=0.0, timeout=5.0) -> Session`
Opens a session to LabVIEW's Network Streams listener.

| Parameter | Meaning |
|---|---|
| `port` | `None`: ask the NI locator on TCP 2343 for the `NW_STREAMS` port (usually 59114). |
| `url_host` | Machine name used in `ni.dex://<url_host>/…` endpoint URLs. Default: this PC's name. |
| `retry_for` | Seconds to keep retrying while LabVIEW isn't reachable yet. |
| `timeout` | Socket and handshake timeout in seconds. |

Named contexts: `connect(host, context="RT", local_context="PC")` opens service `rt/NW_STREAMS` and
builds URLs `ni.dex://<url_host>:RT/...` (remote) and `:PC/...` (Python's own). `nw.list_contexts(host)`
returns `{context: port}` from NI's locator. With `retry_for=`, `connect` waits until the context appears.

### `nw.JKIEndpoint(host, my_name, remote_name, *, retry_for=30.0, url_host=None, timeout=5.0)`
A JKI Network Stream 2-way endpoint (Python connects to the LabVIEW endpoint `remote_name`).

| Member | Description |
|---|---|
| `send(command, arguments=None, msg_id=0, wait_ack=False, timeout=5.0) -> int` | Send a command; with `wait_ack=True` block until acknowledged. Returns the message ID. |
| `receive(timeout=None) -> JKIMessage` | Next command (`.command`, `.arguments`, `.msg_id`, `.wants_ack`). Commands asking for an ack are acknowledged automatically. |
| `close()` | Close both links and the session. |

`nw.JKI_MESSAGE` is the element type `Cluster(Cluster(STRING, VARIANT, I32), BOOLEAN, I32)`.

### `Session`
| Method | Description |
|---|---|
| `open_reader(writer_url, reader_url, dtype, buffer_size=100, decoder=None, timeout=None, wait_for_peer=0.0) -> Reader` | Connect a Python reader to a LabVIEW **writer** endpoint. `writer_url` is LabVIEW's endpoint (e.g. `"data/writer"`), `reader_url` is Python's own name. |
| `open_writer(reader_url, writer_url, dtype, buffer_size=10, timeout=None, wait_for_peer=0.0) -> Writer` | Connect a Python writer to a LabVIEW **reader** endpoint. |
| `url(path) -> str` | Expand `"data/writer"` to `ni.dex://<url_host>/data/writer`. |
| `close()` | Close every link (LabVIEW acknowledges each one), then the session. |

`dtype` is a data type (see [Data types](#data-types)) or, for advanced use, raw descriptor bytes.
`wait_for_peer=N` resends the link request every 10 ms for up to N seconds while LabVIEW's endpoint
doesn't exist yet. That's what LabVIEW itself does, so it doesn't matter which side starts first.

### `Reader`
| Member | Description |
|---|---|
| `read(timeout=None)` | Next element, decoded. Raises `TimeoutError` or `StreamClosed`. |
| `read_raw(timeout=None) -> bytes` | Next element as flattened bytes. |
| `drain(max_items=None) -> list` | Every element available now, oldest first, without waiting. Raises `StreamClosed` once the writer has closed and nothing is left. |
| `available -> int` | Elements queued now. |
| `buffer_size` | Buffer size LabVIEW accepted for the link. |
| `close(timeout=2.0)` | Close this link only. |

Flow control: an element's buffer slot is returned to LabVIEW only when you read it, so a slow Python
consumer slows the LabVIEW writer down instead of losing data.

Decode failures: if the decoder raises on an element, `read()` and `drain()` return
`nw.RawElement(data, error)` in its place (the flattened bytes and the error text) and carry on with
the next elements. The element's slot is still returned to LabVIEW, so one bad element can't stall the
writer. Before 0.4.1, `drain()` lost every element of that call and `read()` raised the decoder's error.

### `Writer`
| Member | Description |
|---|---|
| `write(value, timeout=5.0)` | Encode and send one element. Blocks while LabVIEW's reader buffer is full, and raises `TimeoutError` after `timeout`. |
| `write_raw(flat: bytes, timeout=5.0)` | Send an already flattened element. |
| `buffer_size` | Buffer size LabVIEW accepted. |
| `close(timeout=2.0)` | Close this link only. |

### Exceptions
| Exception | When |
|---|---|
| `LinkRefused` (`.code`) | LabVIEW refused a link. Code 314050: the endpoint doesn't exist, isn't running yet, or is already connected. |
| `StreamClosed` | The other side closed the stream. |
| `ProtocolError` | Unexpected bytes on the wire. |

---

## DataSocket

### `nw.DataSocketClient(host="127.0.0.1", port=3015, timeout=5.0)`
Connects to the NI DataSocket Server (`cwdss.exe`). For a server on another PC, pass its address as
`host`. The address inside a `dstp://` URL is ignored, and only the item name is used.

| Member | Description |
|---|---|
| `open(url, mode=MODE_READ) -> str` | Open an item. `url` is `dstp://localhost/wave1` or just `wave1`. Returns the item name. |
| `MODE_READ` (3), `MODE_WRITE` (4) | Bit flags 1 = read, 2 = subscribe, 4 = write. `MODE_READ` works on local **and remote** servers. LabVIEW's own "Read" sends 7, which a remote server refuses. |
| `read(url, timeout=None)` | Next value the server pushed for the item. The current value arrives right after `open()`. |
| `drain(url) -> list` | Every value pushed since the last read or drain, without waiting. Raises `ConnectionError` once the connection is gone and nothing is left. |
| `write(url, value, dtype=None, wait_ack=True)` | Write a value. The type is inferred for float, str and lists of str, so pass `dtype=` otherwise. Waits for the server's acknowledgement. |
| `latest` | `dict` of the newest value per item. |
| `other` | `queue.Queue` of messages the client didn't handle, as `nw.DataSocketMessage`. |
| `closed` | `threading.Event`, set when the connection is gone. |
| `close()` | Close all items and disconnect. |

`DataSocketMessage` is the 2-tuple `(dtype, value)`, so `t, v = msg` still works, plus `.op`, `.item`
and `.error`. A message the client couldn't decode has `dtype` `None` and the raw message bytes as
`value`; `.item` is the item it belongs to when the message carries one (for example a pushed value of
an unsupported type), and `.error` says why decoding failed.

**Exception:** `DataSocketAccessDenied`, a subclass of `PermissionError`. The server refused the open
(opcode 51). A server on another PC allows writing only from its own localhost by default, so add this
PC to its DefaultWriters, or read with `MODE_READ`.

**Exception:** `DataSocketItemBusy`, a subclass of `TimeoutError`. A write-mode open got no reply,
because another client (usually a LabVIEW writer) is writing the item. The server allows one writer
per item.

Behaviour: the server doesn't send your own writes back to you, only to other readers.

---

## Shared Variables (network-published, `psp:`)

### `nw.SharedVariableClient(library, host="127.0.0.1", port=None, machine=None, timeout=5.0)`
One session to the Shared Variable Engine (`tagsrv.exe`) for **one variable library**.

| Parameter | Meaning |
|---|---|
| `library` | Library name as deployed, e.g. `"Server Variables"`. |
| `port` | `None`: try ports 59110–59120 and keep the one whose engine serves `library`. |
| `machine` | Machine name used in variable paths. Default: this PC's name. |

| Member | Description |
|---|---|
| `open(url, subscribe=True) -> str` | Open a variable: `psp:\\localhost\Lib\Var`, `Lib\Var` or `Var`. Subscribing makes the engine push every new value. |
| `read(name, timeout=None)` | Next pushed value. The current value arrives right after `open()`. |
| `drain(name) -> list` | Every update since the last read or drain, without waiting. Raises `ConnectionError` once the connection is gone and nothing is left. |
| `latest(name)` | Newest value seen. |
| `write(name, value, dtype=None)` | Write a value. Pass the variable's LabVIEW type for anything but DBL, I32 or String, e.g. `dtype=nw.I16` or `dtype=nw.Array(nw.I32, 2)`. |
| `properties(name) -> bytes` | Raw item properties from the engine (buffer size etc., not fully decoded). |
| `closed` | `threading.Event`, set when the connection is gone. |
| `close()` | Close all variables and the session. |

The library must be **deployed**, otherwise `open()` times out.

A value record the client can't decode is returned as `nw.RawRecord(kind, data)` (frozen dataclass:
the record's u32 kind field and its bytes), in place of the value, so nothing is dropped. Test for it
with `isinstance(value, nw.RawRecord)`. Before 0.4.1 it was the tuple `("raw", kind, data)`.

---

## Data types

Use these as `dtype` for Network Streams links, DataSocket writes and Shared Variable writes.

| Python object | LabVIEW type | Python value |
|---|---|---|
| `I8`, `I16`, `I32`, `I64`, `U8`, `U16`, `U32`, `U64` | integers (enums arrive as their number) | `int` |
| `SGL`, `DBL`, `EXT` | floating point (EXT: IEEE quad) | `float` |
| `CSG`, `CDB` | complex | `complex` |
| `BOOLEAN` | Boolean | `bool` |
| `STRING` | String | `str` (latin-1) |
| `PATH` | Path | `nw.LVPath` (`str()` gives `C:\dir\file`) |
| `TIMESTAMP` | Timestamp | `nw.Timestamp` (`.unix`, `.lv`, `Timestamp.from_unix(t)`) |
| `VARIANT` | Variant | the contained value; with attributes, `nw.LVVariant`. Empty variant: `None` |
| `Array(t, ndims=1)` | 1-D … n-D array | nested lists |
| `Cluster(t1, t2, …)` | Cluster | `tuple` |
| `ERROR_CLUSTER` | error in/out | `(status, code, source)` |
| `WAVEFORM` | Waveform (DBL) | `nw.Waveform(t0, dt, y, attributes)` |
| `WAVEFORM_ARRAY` | 1-D array of waveforms (the example's type) | `list[nw.Waveform]` |
| `MESSAGE` | the example's command cluster {String, Variant} | `(str, value)` |

Helpers:
- `nw.parse_td(bytes)` turns a type descriptor from the wire into one of these types.
- `nw.pack_element(dtype, value)` and `nw.unpack_element(dtype, bytes)` flatten a value and unflatten
  it again, exactly as Network Streams carries it.
- `nw.VariantValue(dtype, value)` puts an explicitly typed value into a variant.

Unknown descriptors raise `NotImplementedError` instead of being guessed.
