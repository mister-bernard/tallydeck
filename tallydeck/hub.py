"""The hub: merges signals from sources, streams snapshots, routes presses.

Protocol (NDJSON, one object per line, over any bidirectional pipe —
stdio, an SSH session, socat, whatever):

  hub → client   {"type": "hello", "v": 1, "name": "..."}
  hub → client   {"type": "snapshot", "signals": [ {...}, ... ]}
  client → hub   {"type": "press", "id": "cc/foo", "long": false}
  client → hub   {"type": "ping"}   → hub replies {"type": "pong"}

Snapshots are full-state (a fleet is tens of signals, not thousands);
the hub sends one whenever the merged state changes, and a keepalive
snapshot every KEEPALIVE seconds regardless.

Security posture: the client can only name a signal id it wants pressed.
Actions run hub-side and only if the hub's own sources/config defined
them. Nothing arriving on the wire is ever executed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

from .signal import Signal, rank
from .sources.base import Source

PROTOCOL_VERSION = 1
KEEPALIVE = 20.0


class Hub:
    def __init__(self, sources: list[Source], name: str = "tallyd",
                 tick: float = 2.0, log=lambda m: print(m, file=sys.stderr)):
        self.sources = sources
        self.name = name
        self.tick = tick
        self.log = log
        self._table: dict[str, Signal] = {}

    # ── state assembly ───────────────────────────────────────────────────────

    def poll(self) -> list[Signal]:
        """Ask every source for its current signals; merge into the table."""
        now = time.time()
        fresh: dict[str, Signal] = {}
        for src in self.sources:
            try:
                for sig in src.poll():
                    sig.group = sig.group or src.group
                    fresh[sig.id] = sig
            except Exception as e:  # a broken source must not take down the hub
                self.log(f"[hub] source {src.group!r} error: {e}")
        # Sources are authoritative for their own group: a signal disappears
        # when its source stops emitting it or its ttl lapses.
        self._table = {
            sid: s for sid, s in fresh.items() if not s.expired(now)
        }
        return rank(list(self._table.values()))

    def snapshot_line(self, signals: list[Signal]) -> str:
        return json.dumps(
            {"type": "snapshot", "signals": [s.to_dict() for s in signals]},
            separators=(",", ":"),
        )

    # ── press routing ────────────────────────────────────────────────────────

    def press(self, sid: str, long: bool = False) -> None:
        sig = self._table.get(sid)
        if sig is None:
            self.log(f"[hub] press for unknown signal {sid!r}")
            return
        # First give the owning source a chance (e.g. ack / clear a file).
        for src in self.sources:
            if src.group == sig.group and src.on_press(sig, long):
                return
        # Then the signal's own declared action.
        act = sig.action or {}
        if act.get("type") == "cmd" and isinstance(act.get("argv"), list):
            argv = [str(a) for a in act["argv"]]
            self.log(f"[hub] press {sid} → {' '.join(argv)}")
            try:
                subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:
                self.log(f"[hub] action failed: {e}")
        else:
            self.log(f"[hub] press {sid} (no action)")

    # ── stdio server ─────────────────────────────────────────────────────────

    def serve_stdio(self) -> None:
        """Serve the protocol on stdin/stdout until EOF."""
        out = sys.stdout
        lock = threading.Lock()

        def send(line: str) -> None:
            with lock:
                out.write(line + "\n")
                out.flush()

        send(json.dumps({"type": "hello", "v": PROTOCOL_VERSION,
                         "name": self.name}))

        stop = threading.Event()

        def reader() -> None:
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                if t == "press":
                    self.press(str(msg.get("id", "")), bool(msg.get("long")))
                elif t == "ping":
                    send('{"type":"pong"}')
            stop.set()  # client hung up

        threading.Thread(target=reader, daemon=True).start()

        last_sent = ""
        last_time = 0.0
        while not stop.is_set():
            signals = self.poll()
            line = self.snapshot_line(signals)
            now = time.time()
            if line != last_sent or (now - last_time) > KEEPALIVE:
                try:
                    send(line)
                except BrokenPipeError:
                    break
                last_sent, last_time = line, now
            stop.wait(self.tick)
