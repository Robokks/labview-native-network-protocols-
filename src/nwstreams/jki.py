"""JKI Network Stream (2-way endpoint) on top of nwstreams. Experimental.

Decoded from a capture of JKI's "Network Stream 2-Way Communication Example" (PC_UI.vi and
RT_Main.vi, LabVIEW 2020). Each endpoint is a Network Streams *context* named after its
"My Name" and owns two streams:

    <me>_Writer  ->  <remote>_Reader
    <remote>_Writer  ->  <me>_Reader          (URLs: ni.dex://<host>:<context>/<name>, buffer 20)

Element type: Cluster(Cluster(Command: String, Arguments: Variant, Msg Id: I32),
                      Wait for Ack?: Boolean, Message ID: I32)
A message sent with "Wait for Ack?" true is answered with
(("Ack Response", <empty variant>, 0), False, <same Message ID>).

Python can play the endpoint that CONNECTS (the one with a Remote IP Address, like PC_UI.vi).
The endpoint without an address (RT_Main.vi) must be LabVIEW: it registers its context with
NI's locator over local IPC, which Python can't do.

    with JKIEndpoint("169.254.16.200", my_name="PC", remote_name="RT") as ep:
        ep.send("Cmd From PC", "hello", wait_ack=True)
        print(ep.receive(timeout=5))
"""
from __future__ import annotations

import queue
import random
import threading
from dataclasses import dataclass
from typing import Any, Optional

from .session import StreamClosed, connect
from .types import BOOLEAN, I32, STRING, VARIANT, Cluster

JKI_MESSAGE = Cluster(Cluster(STRING, VARIANT, I32), BOOLEAN, I32)
ACK_COMMAND = "Ack Response"
BUFFER = 20


@dataclass
class JKIMessage:
    command: str
    arguments: Any
    msg_id: int = 0          # the cluster's "Msg Id" field (user data)
    wants_ack: bool = False
    message_id: int = 0      # transport-level ID used to match acks


class JKIEndpoint:
    def __init__(self, host: str, my_name: str, remote_name: str, *, retry_for: float = 30.0,
                 url_host: Optional[str] = None, timeout: float = 5.0):
        """Connect to the LabVIEW JKI endpoint `remote_name` on `host` (waits up to retry_for
        seconds for it to appear at NI's locator)."""
        self.my_name, self.remote_name = my_name, remote_name
        self.session = connect(host, retry_for=retry_for, timeout=timeout,
                               url_host=url_host or host, context=remote_name, local_context=my_name)
        self._tx = self.session.open_writer(f"{remote_name}_Reader", f"{my_name}_Writer",
                                            JKI_MESSAGE, buffer_size=BUFFER, wait_for_peer=retry_for)
        self._rx = self.session.open_reader(f"{remote_name}_Writer", f"{my_name}_Reader",
                                            JKI_MESSAGE, buffer_size=BUFFER, wait_for_peer=retry_for)
        self._inbox: "queue.Queue[JKIMessage]" = queue.Queue()
        self._acks: "dict[int, threading.Event]" = {}
        self._stop = threading.Event()
        threading.Thread(target=self._pump, name="jki-rx", daemon=True).start()

    # ---- public API ----
    def send(self, command: str, arguments: Any = None, msg_id: int = 0,
             wait_ack: bool = False, timeout: float = 5.0) -> int:
        """Send a command; with wait_ack=True block until the remote endpoint acknowledges it."""
        mid = random.randint(1, 2**31 - 1)
        ev = self._acks[mid] = threading.Event() if wait_ack else None
        self._tx.write(((command, arguments, msg_id), bool(wait_ack), mid), timeout=timeout)
        if wait_ack and not ev.wait(timeout):
            self._acks.pop(mid, None)
            raise TimeoutError(f"no ack for {command!r} within {timeout} s")
        return mid

    def receive(self, timeout: Optional[float] = None) -> JKIMessage:
        """Next command from the remote endpoint (acks are handled internally)."""
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no message within timeout") from None

    def close(self) -> None:
        self._stop.set()
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- internals ----
    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                (cmd, args, msg_id), wants_ack, mid = self._rx.read(timeout=0.5)
            except TimeoutError:
                continue
            except (StreamClosed, OSError):
                return
            if cmd == ACK_COMMAND and not wants_ack:
                ev = self._acks.pop(mid, None)
                if ev:
                    ev.set()
                continue
            if wants_ack:
                try:
                    self._tx.write(((ACK_COMMAND, None, 0), False, mid))
                except (StreamClosed, OSError, TimeoutError):
                    pass
            self._inbox.put(JKIMessage(cmd, args, msg_id, wants_ack, mid))
