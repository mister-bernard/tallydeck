"""The hub: merges signals from sources, streams snapshots, routes presses.

Protocol (NDJSON, one object per line, over any bidirectional pipe —
stdio, an SSH session, socat, whatever):

  hub → client   {"type": "hello", "v": 1, "name": "..."}
  hub → client   {"type": "snapshot", "signals": [ {...}, ... ]}
  client → hub   {"type": "press", "id": "cc/foo", "long": false}
  client → hub   {"type": "answer", "id": "sig/foo", "text": "1"}
  client → hub   {"type": "ping"}   → hub replies {"type": "pong"}

Snapshots are full-state (a fleet is tens of signals, not thousands);
the hub sends one whenever the merged state changes, and a keepalive
snapshot every KEEPALIVE seconds regardless.

Security posture: the client can only name a signal id it wants pressed,
or answer a question the hub itself has raised. Actions run hub-side and
only if the hub's own sources/config defined them. An answer is accepted
only for a currently-raised question and is recorded — never executed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from .signal import Signal, rank
from .sources.base import Source
from .paths import hub_alive

PROTOCOL_VERSION = 1
KEEPALIVE = 20.0
OPEN_AFTER = 0.7      # s an action must survive to count as "opened"
HEARTBEAT = 5.0       # s between hub.alive touches while a client is connected


class Hub:
    def __init__(self, sources: list[Source], name: str = "tallyd",
                 tick: float = 2.0, log=lambda m: print(m, file=sys.stderr)):
        self.sources = sources
        self.name = name
        self.tick = tick
        self.log = log
        self._table: dict[str, Signal] = {}
        # Press actions in flight: signal id → (Popen, started). A decide
        # popup blocks its command until the operator answers, so a process
        # still alive after a beat means the popup is UP — the key can stop
        # shouting. A quick exit means it failed to open; keep flashing.
        self._inflight: dict[str, tuple] = {}

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
        for sid in list(self._inflight):
            proc, started = self._inflight[sid]
            if proc.poll() is not None or sid not in self._table:
                self._inflight.pop(sid, None)
                continue
            if now - started > OPEN_AFTER:
                s = self._table[sid]
                s.meta["opened"] = True
                s.flash = False          # steady while the popup is up
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
                proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                self._inflight[sid] = (proc, time.time())
            except OSError as e:
                self.log(f"[hub] action failed: {e}")
        else:
            self.log(f"[hub] press {sid} (no action)")

    # ── answers (from a native notification on the deck machine) ──────────

    def answer(self, sid: str, text: str) -> bool:
        """Record an operator answer to a raised question. Validated: the
        id must be a currently-raised watchdir question (not a session
        ask), and a bare digit must index its option list. Delivery and the
        flag clear go through tally-decide's own --deliver pass so every
        surface records answers the same way."""
        sig = self._table.get(sid)
        text = " ".join(str(text).split())[:500]
        if sig is None or sig.group != "sig" or not text:
            return False
        stem = sid.split("/", 1)[-1]
        if stem.startswith("ask-") or not (sig.detail or "").strip():
            return False
        opts = [str(o) for o in (sig.meta.get("options") or [])]
        if text.isdigit():
            n = int(text)
            if not opts or not 1 <= n <= len(opts):
                return False
            text = f"{n} — {opts[n - 1]}"
        from .paths import state_dir
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        adir = state_dir() / "answers"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / f"{stem}.json").write_text(json.dumps(
            {"id": stem, "answer": text, "at": ts, "label": sig.label,
             "via": "deck-notification"}))
        decide = self._decide_bin()
        if decide:
            try:
                subprocess.Popen([decide, "--deliver", stem, sig.label, text, ts],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                self.log(f"[hub] deliver failed: {e}")
        self.log(f"[hub] answer {sid}: {text[:60]}")
        return True

    @staticmethod
    def _decide_bin() -> str:
        import shutil
        from pathlib import Path
        here = Path(__file__).resolve().parent.parent / "contrib" / "tally-decide"
        for cand in (str(here), shutil.which("tally-decide") or ""):
            if cand and os.access(cand, os.X_OK):
                return cand
        return ""

    # ── presence ─────────────────────────────────────────────────────────────

    def heartbeat(self) -> None:
        """Touch hub.alive: 'a deck is connected right now'. The Signal
        notifier holds its fire while this is fresh — the operator is at
        the desk, the question is on the keys, no need to buzz the phone.
        Stale (or gone) means escalate."""
        try:
            p = hub_alive()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        except OSError:
            pass

    def _clear_heartbeat(self) -> None:
        try:
            hub_alive().unlink(missing_ok=True)
        except OSError:
            pass

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
                elif t == "answer":
                    self.answer(str(msg.get("id", "")), str(msg.get("text", "")))
                elif t == "ping":
                    send('{"type":"pong"}')
            stop.set()  # client hung up

        threading.Thread(target=reader, daemon=True).start()

        last_sent = ""
        last_time = 0.0
        last_beat = 0.0
        try:
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
                if now - last_beat > HEARTBEAT:
                    self.heartbeat()
                    last_beat = now
                stop.wait(self.tick)
        finally:
            self._clear_heartbeat()      # unplugged: the phone takes over
