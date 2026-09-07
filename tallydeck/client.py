"""Client: connect a surface (deck / terminal / PNG) to a hub and run.

Two links:
  LocalLink — hub runs in-process (deck and agents on the same machine).
  PipeLink  — hub runs at the end of any command's stdio, canonically
              `ssh yourserver tallyd`. The deck machine needs no state,
              no credentials beyond SSH, no open ports.

The loop is event-ish: re-render on snapshot change; while any key is
flashing, tick at FRAME_INTERVAL so blinks animate; otherwise sleep.
Long-press (≥ 0.5 s) sends `long: true` — sources may treat that as
"dismiss" vs a short "ack / focus".
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from .hub import Hub
from .signal import Signal
from .view import View
from .render import theme

LONG_PRESS = 0.5


# ── links ────────────────────────────────────────────────────────────────────

class LocalLink:
    def __init__(self, hub: Hub):
        self.hub = hub

    def poll(self) -> list[Signal]:
        return self.hub.poll()

    def press(self, sid: str, long: bool = False) -> None:
        self.hub.press(sid, long)

    def close(self) -> None:
        pass


class PipeLink:
    """Speak the hub protocol over a spawned command's stdio."""

    def __init__(self, argv: list[str], log=lambda m: None):
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            bufsize=1)
        self.log = log
        self._signals: list[Signal] = []
        self._lock = threading.Lock()
        self._alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "snapshot":
                sigs = []
                for d in msg.get("signals", []):
                    try:
                        sigs.append(Signal.from_dict(d))
                    except ValueError:
                        continue
                with self._lock:
                    self._signals = sigs
            elif msg.get("type") == "hello":
                self.log(f"[link] connected to {msg.get('name')}")
        self._alive = False

    def poll(self) -> list[Signal]:
        with self._lock:
            return list(self._signals)

    def press(self, sid: str, long: bool = False) -> None:
        self._send({"type": "press", "id": sid, "long": long})

    def _send(self, obj: dict) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive and self.proc.poll() is None

    def close(self) -> None:
        try:
            self.proc.terminate()
        except OSError:
            pass


# ── run loop ─────────────────────────────────────────────────────────────────

def run(link, surface, view: View, poll_every: float = 2.0,
        once: bool = False, on_press_cmd: list[str] | None = None) -> None:
    """Drive `surface` from `link` until interrupted (or one frame if once).

    `on_press_cmd`: optional LOCAL command run on every short press, in
    addition to the hub-side action — this is how a key press reaches the
    machine the deck is plugged into (pop a terminal window, ring a bell,
    raise an app). It receives the signal as TALLY_* environment variables:
    TALLY_ID, TALLY_LABEL, TALLY_GROUP, TALLY_STATE, TALLY_PROJECT,
    TALLY_SESSION, TALLY_LONG.
    """
    pressed_at: dict[int, float] = {}
    held: set[int] = set()
    key_map: list[Signal | None] = []
    wake = threading.Event()      # poked by input so feedback is instant

    def local_action(sig: Signal, long: bool) -> None:
        if not on_press_cmd:
            return
        import os
        env = dict(os.environ,
                   TALLY_ID=sig.id, TALLY_LABEL=sig.label,
                   TALLY_GROUP=sig.group, TALLY_STATE=sig.state,
                   TALLY_PROJECT=str(sig.meta.get("project", "")),
                   TALLY_SESSION=str(sig.meta.get("session", "")),
                   TALLY_TMUX=str(sig.meta.get("tmux", "")),
                   TALLY_ACCOUNT=str(sig.meta.get("account", "")),
                   TALLY_LONG="1" if long else "0")
        try:
            subprocess.Popen(on_press_cmd, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def on_key(index: int, down: bool) -> None:
        if down:
            pressed_at[index] = time.monotonic()
            held.add(index)
        else:
            held.discard(index)
            t0 = pressed_at.pop(index, None)
            if t0 is not None and index < len(key_map):
                sig = key_map[index]
                if sig is not None:
                    long = (time.monotonic() - t0) >= LONG_PRESS
                    link.press(sig.id, long=long)
                    local_action(sig, long)
        wake.set()

    pages_now = [1]   # updated each frame; touch behavior depends on it

    _URGENCY = {"blocked": 2, "attention": 1}

    def on_touch(direction: int) -> None:
        if direction > 0:
            # Right point: cycle pages (wraps) when there are any.
            if pages_now[0] > 1:
                if view.page >= pages_now[0] - 1:
                    view.page = 0
                else:
                    view.page_next()
        else:
            # Left point is the beacon. Most urgent thing on ANOTHER page →
            # jump to that page first (see it in context); already visible →
            # service it directly.
            urgent = [s for s in signals if s.state in _URGENCY]
            if urgent:
                sig = max(urgent, key=lambda s: (_URGENCY[s.state],
                                                 s.priority, s.updated))
                if any(k is not None and k.id == sig.id for k in key_map):
                    link.press(sig.id, long=False)
                    local_action(sig, False)
                else:
                    view.jump_to(sig.id)
        wake.set()

    if hasattr(surface, "set_callbacks"):
        surface.set_callbacks(on_key=on_key, on_touch=on_touch)

    last_poll = 0.0
    signals: list[Signal] = []
    prev_frame = None
    try:
        while True:
            now = time.monotonic()
            if now - last_poll >= poll_every or not signals:
                signals = link.poll()
                last_poll = now

            layout = view.layout(signals)
            key_map = layout.keys
            pages_now[0] = layout.pages
            flashing = [s for s in layout.keys if s and s.wants_flash]
            wall = time.time()   # epoch, so all surfaces blink in phase
            lit = {s.id: theme.flash_lit(s.state, wall) for s in flashing}

            m = layout.meter
            pressed = frozenset(held)
            frame = ([(s.id, s.state, s.label, s.sublabel, s.progress,
                       round(float(s.meta.get('heat', 0) or 0), 2))
                      if s else None for s in layout.keys],
                     tuple(sorted(lit.items())), pressed, layout.summary,
                     layout.page, layout.pages,
                     None if m is None else tuple(sorted(
                         (k, v) for k, v in m.meta.items()
                         if isinstance(v, (str, int, float, bool)))),
                     int(wall * 0.5) if m is not None else 0,  # meter hatch tick
                     int(wall / 3) % 2 if any(
                         s and s.state in ("attention", "blocked")
                         and len(s.sublabel) > 30 for s in layout.keys)
                     else 0)                                    # ask page tick
            if frame != prev_frame:
                surface.show(layout, lit, t=wall, pressed=pressed)
                prev_frame = frame

            if once:
                return
            # A press interrupts the sleep so ring feedback is immediate;
            # a press also forces a re-poll so acks/state changes land fast.
            if wake.wait(theme.FRAME_INTERVAL if flashing
                         else min(0.25, poll_every / 4)):
                wake.clear()
                last_poll = 0.0
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(surface, "close"):
            surface.close()
        link.close()
