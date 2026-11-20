# Getting started

These steps use NI's own LabVIEW example projects, which ship with LabVIEW
(`<LabVIEW>\examples\Data Communication\…`). No LabVIEW programming is needed.

## 1. Install

On Windows the `pip` command is often not on PATH, so run pip through the Python launcher:

```powershell
py -m pip install https://github.com/Robokks/labview-native-network-protocols-/releases/download/v0.4.0/nwstreams-0.4.0-py3-none-any.whl
py -c "import nwstreams; print(nwstreams.__version__)"
```

No other packages are needed. To run the tests, install from a source checkout instead:
`py -m pip install pytest`, then `py -m pytest` in the repository folder.

## 2. Network Streams: Python in place of Host.vi

1. Open `Network Streams\Simple Network Streams\Simple Network Streams.lvproj`.
2. Run **Simple Network Streams - Target.vi** only. Leave Host.vi closed.
3. Run `py examples\simple_host.py --freq 20 --seconds 10`.

You should see two waveforms of 2000 samples per element, about every 20 ms, and Target's Frequency
control changes to 20.

Target.vi ends its loops when the reader disconnects (the example is built that way), so **restart
Target.vi before every run**.

## 3. DataSocket

1. Start the **DataSocket Server** from the NI programs in the Start menu. It listens on TCP 3015.
2. Open `DataSocket\Simple DataSocket.lvproj`.
3. To **read**: run Writer.vi, then `py examples\datasocket_demo.py read`.
4. To **write**: stop Writer.vi, run Reader.vi, then `py examples\datasocket_demo.py write`.
   Reader.vi's graph shows a straight 0–39 line.

## 4. Shared Variables

1. Open `Shared Variable\Shared Variable.lvproj` and **deploy** `Server Variables.lvlib`
   (right-click it, then Deploy).
2. To **read**: run Shared Variable Server.vi, then `py examples\shared_variable_demo.py`.
3. To **write**: stop Server.vi (it overwrites the values every 500 ms), keep a client VI open, then run
   `py examples\shared_variable_demo.py 0.25 0.75`. The client's gauges show 0.25 and 0.75.

## 5. Python as Target.vi (LabVIEW Host.vi connects to Python)

Python plays the part of `Simple Network Streams - Target.vi`, and the unchanged Host.vi connects to it.

**The Python PC must have TCP port 2343 free.** NI's **NI PSP Service Locator** (service `lkClassAds`)
holds 2343 on every address of a PC with NI software. Two working setups:

- **Two PCs (verified):** LabVIEW Host.vi on PC A and Python on PC B, connected by network or a direct
  cable (for example a USB-Ethernet adapter). On PC B stop the NI PSP Service Locator (or use a PC
  without NI software). On PC A it **must keep running**, because LabVIEW's Network Streams need it to
  create endpoints.
- **Python PC without internet:** copy the script with the `nwstreams` folder next to it. Nothing has to be
  installed apart from Python 3.9+.

Steps:
1. On the Python PC, allow inbound TCP 2343 and 59200 in the firewall, for example from an
   administrator PowerShell:
   `New-NetFirewallRule -DisplayName "nwstreams Python Target" -Direction Inbound -Protocol TCP -LocalPort 2343,59200 -Action Allow`
2. Run `py examples\python_target.py --stream-port 59200 --seconds 600`. It prints
   `locator listening on TCP 2343` and `stream server listening on TCP 59200`.
3. On the LabVIEW PC, set Host.vi's **Target IP Address** to the Python PC's address and run Host.vi.
   The graph shows Python's sine and square waves, and the Frequency knob changes them.

If LabVIEW's Network Streams were first used while the NI PSP Service Locator was stopped, Host.vi hangs
in Create Endpoint. Start the service and restart LabVIEW.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `pip` is not recognized | pip's Scripts folder isn't on PATH | Use `py -m pip …` |
| `LinkRefused … -314050` | The LabVIEW endpoint doesn't exist (yet), or it ended after the last run | Start or restart the LabVIEW VI, or pass `wait_for_peer=30` |
| `TimeoutError: no answer to link request` | You connected to an endpoint created **without** a remote URL; that handshake isn't supported | Use the example's own Target.vi, which waits for a reader |
| `DataSocketItemBusy` | Another client (e.g. LabVIEW Writer.vi) is writing the item | Stop that writer, or open in read mode |
| LabVIEW error 1142 in a DataSocket writer | A Python read-mode open of the item came first | Start LabVIEW writers before Python readers |
| Shared Variable `open()` times out | The library isn't deployed, or the wrong library name was given | Deploy the `.lvlib` and check the name |
| Shared Variable client can't find the engine | The engine moved to an unusual port | Pass `port=` (find it with `Get-NetTCPConnection -State Listen`; the process is `tagsrv`) |
| Values look wrong or `NotImplementedError` | The data type hasn't been decoded yet | See the type table in [API.md](API.md), and open an issue with a capture |

## Capturing traffic for new types

1. Install Wireshark with Npcap and capture on the **Adapter for loopback traffic capture**,
   without a capture filter.
2. Start the capture before the VIs, use known values, and stop it after a few seconds.
3. `tools/pcap_flows.py` and `tools/c7_decode.py` in the original project split such captures into
   connections and messages. Remove machine names and user paths before sharing a capture.
