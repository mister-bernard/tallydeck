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
import sys
import threading
import time

from .hub import Hub
from .signal import Signal
from .render import fx
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

    def answer(self, sid: str, text: str) -> None:
        self.hub.answer(sid, text)

    def close(self) -> None:
        pass


class PipeLink:
    """Speak the hub protocol over a spawned command's stdio."""

    RECONNECT_AFTER = 3.0     # s between attempts once the pipe is gone

    def __init__(self, argv: list[str], log=lambda m: None):
        self.argv = list(argv)
        self.log = log
        self._signals: list[Signal] = []
        self._lock = threading.Lock()
        self._alive = False
        self.reconnects = 0
        self._last_try = 0.0
        self.proc = None
        self._spawn()

    def _spawn(self) -> None:
        self.proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            bufsize=1)
        self._alive = True
        self._last_try = time.monotonic()
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def reconnect(self) -> bool:
        """The hub died (a server-side restart, a dropped ssh): re-spawn the
        connect command, keeping the last snapshot on the keys meanwhile.
        Without this the deck froze on its last frame and the operator had
        to relaunch the client for every hub change."""
        if self.alive or time.monotonic() - self._last_try < self.RECONNECT_AFTER:
            return False
        try:
            if self.proc is not None:
                self.proc.kill()
        except OSError:
            pass
        self.log("[link] hub gone — reconnecting")
        try:
            self._spawn()
        except OSError as e:
            self._last_try = time.monotonic()
            self.log(f"[link] reconnect failed: {e}")
            return False
        self.reconnects += 1
        return True

    def _reader(self, proc) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
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
        if proc is self.proc:
            self._alive = False

    def poll(self) -> list[Signal]:
        with self._lock:
            return list(self._signals)

    def press(self, sid: str, long: bool = False) -> None:
        self._send({"type": "press", "id": sid, "long": long})

    def answer(self, sid: str, text: str) -> None:
        self._send({"type": "answer", "id": sid, "text": text})

    def _send(self, obj: dict) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive and self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        try:
            self.proc.terminate()
        except OSError:
            pass


# ── native notifications (deck machine) ─────────────────────────────────────

class Notifier:
    """A raised question also lands as a native notification with a reply
    field on the machine holding the deck — the one surface that works
    with no tmux attached and no deck in reach. macOS via terminal-notifier
    (`brew install terminal-notifier`); silently inactive without it. The
    reply text goes back up the hub pipe as an `answer`, which the hub
    validates against the raised flag before recording."""

    def __init__(self, link, log=lambda m: None):
        import shutil
        self.link = link
        self.log = log
        self.bin = shutil.which("terminal-notifier")
        self._seen: dict[str, str] = {}
        self._threads: list = []

    @property
    def active(self) -> bool:
        return bool(self.bin)

    def offer(self, signals: list) -> None:
        if not self.bin:
            return
        live = set()
        for s in signals:
            if s.group != "sig" or s.state not in ("attention", "blocked"):
                continue
            stem = s.id.split("/", 1)[-1]
            if stem.startswith("ask-") or not (s.detail or "").strip():
                continue
            live.add(s.id)
            opts = [str(o) for o in (s.meta.get("options") or [])]
            key = f"{s.sublabel}|{'|'.join(opts)}"
            if self._seen.get(s.id) == key:
                continue
            self._seen[s.id] = key
            body = s.sublabel or s.detail
            if opts:
                body += "\n" + "  ".join(f"{i}·{o[:18]}" for i, o in enumerate(opts, 1))
            t = threading.Thread(target=self._ask, args=(s.id, s.label, body),
                                 daemon=True)
            t.start()
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
        for sid in [k for k in self._seen if k not in live]:
            self._seen.pop(sid, None)
            # Answered elsewhere / withdrawn: take the notification down so
            # a stale reply cannot be typed into it hours later.
            try:
                subprocess.Popen([self.bin, "-remove", f"tally-{sid}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def _ask(self, sid: str, title: str, body: str) -> None:
        try:
            r = subprocess.run(
                [self.bin, "-title", f"◆ {title}", "-message", body,
                 "-reply", "answer…", "-timeout", "3600",
                 "-group", f"tally-{sid}", "-sound", "default"],
                capture_output=True, text=True, timeout=3700)
            text = (r.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            return
        if not text or text.startswith("@"):      # @TIMEOUT / @CLOSED / …
            return
        self.log(f"[notify] answer for {sid}: {text[:40]}")
        self.link.answer(sid, text)


# ── run loop ─────────────────────────────────────────────────────────────────

def run(link, surface, view: View, poll_every: float = 2.0,
        once: bool = False, on_press_cmd: list[str] | None = None) -> None:
    """Drive `surface` from `link` until interrupted (or one frame if once).

    `on_press_cmd`: optional LOCAL command run on every short press, in
    addition to the hub-side action — this is how a key press reaches the
    machine the deck is plugged into (pop a terminal window, ring a bell,
    raise an app). It receives the signal as TALLY_* environment variables:
    TALLY_ID, TALLY_LABEL, TALLY_GROUP, TALLY_STATE, TALLY_PROJECT,
    TALLY_SESSION, TALLY_TMUX, TALLY_ACCOUNT, TALLY_SUBLABEL, TALLY_DETAIL,
    TALLY_LONG.
    """
    pressed_at: dict[int, float] = {}
    held: set[int] = set()
    fx_at: dict[int, float] = {}       # key index → when its fireworks began
    hold: dict[str, float] = {}        # signal id → flash held off until (mono)
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
                   TALLY_SUBLABEL=sig.sublabel, TALLY_DETAIL=sig.detail,
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
            if mural_now[0]:
                # The mural is not a key: any press lifts it for a look at
                # the plain grid (done/idle sessions), then it comes back.
                view.peek()
            elif t0 is not None and index < len(key_map):
                sig = key_map[index]
                if sig is not None:
                    long = (time.monotonic() - t0) >= LONG_PRESS
                    link.press(sig.id, long=long)
                    local_action(sig, long)
                    # The key answers the press itself: fireworks, and the
                    # blinking stops right away. The hub confirms the popup
                    # opened a poll or two later (meta.opened) and keeps it
                    # steady from there; this local hold bridges the gap.
                    fx_at[index] = time.monotonic()
                    hold[sig.id] = time.monotonic() + 6.0
        wake.set()

    pages_now = [1]   # updated each frame; touch behavior depends on it
    mural_now = [False]
    signals: list[Signal] = []   # bound before callbacks can fire
    notifier = Notifier(link, log=getattr(link, "log", lambda m: None))

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

    # Dials (Stream Deck +): a turn previews a candidate in its zone; nothing
    # is sent. State is plain data (dials.py) so the behaviour has tests.
    from .dials import step as dial_step, overlay as dial_overlay, expire as dial_expire
    zones_now: list = [[]]
    pending_dial: dict = {}
    DIAL_HINT = "preview · push: not wired yet"

    def on_dial(dial: int, delta: int) -> None:
        new = dial_step(zones_now[0], pending_dial, dial, delta)
        pending_dial.clear()
        pending_dial.update(new)
        wake.set()

    if hasattr(surface, "set_callbacks"):
        surface.set_callbacks(on_key=on_key, on_touch=on_touch, on_dial=on_dial)

    last_poll = 0.0
    prev_frame = None
    from .hub import Hub as _Hub
    code_mtime0 = _Hub.code_mtime()
    last_code_check = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - last_poll >= poll_every or not signals:
                if hasattr(link, "reconnect") and not getattr(link, "alive", True):
                    link.reconnect()
                signals = link.poll()
                last_poll = now
                notifier.offer(signals)

            layout = view.layout(signals)
            key_map = layout.keys
            pages_now[0] = layout.pages
            mural_now[0] = layout.mural
            if layout.zones is not None:
                zones_now[0] = layout.zones.meta.get("zones") or []
                if pending_dial:
                    live = dial_expire(pending_dial)
                    pending_dial.clear()
                    pending_dial.update(live)
                if pending_dial:
                    # Overlay onto a COPY: the hub's signal object is shared.
                    from dataclasses import replace as _replace
                    layout.zones = _replace(
                        layout.zones,
                        meta={**layout.zones.meta,
                              "zones": dial_overlay(zones_now[0], pending_dial,
                                                    hint=DIAL_HINT)})
            else:
                zones_now[0] = []
            mono = time.monotonic()
            for k in [k for k, t0 in fx_at.items() if mono - t0 > fx.LENGTH]:
                fx_at.pop(k, None)
            for k in [k for k, until in hold.items() if mono > until]:
                hold.pop(k, None)
            fx_phase = {k: (mono - t0) / fx.LENGTH for k, t0 in fx_at.items()}
            flashing = [s for s in layout.keys if s and s.wants_flash
                        and s.id not in hold and not s.meta.get("opened")]
            wall = time.time()   # epoch, so all surfaces blink in phase
            lit = {s.id: theme.flash_lit(s.state, wall) for s in flashing}

            m = layout.meter
            pressed = frozenset(held)
            if layout.zones is None:
                zkey = None
            else:
                from .render.zones import is_stale as _zstale
                zkey = (json.dumps(layout.zones.meta.get("zones"),
                                   sort_keys=True, default=str),
                        _zstale(layout.zones, wall))
            frame = (zkey,
                     [(s.id, s.state, s.label, s.sublabel, s.progress,
                       round(float(s.meta.get('heat', 0) or 0), 2))
                      if s else None for s in layout.keys],
                     tuple(sorted(lit.items())), pressed, layout.summary,
                     layout.page, layout.pages,
                     None if m is None else tuple(sorted(
                         (k, v) for k, v in m.meta.items()
                         if isinstance(v, (str, int, float, bool)))),
                     int(wall * 0.5) if m is not None else 0,  # meter hatch tick
                     ("mural", int(wall)) if layout.mural else None,  # cursor
                     tuple(sorted((k, int(p * 14)) for k, p in fx_phase.items())),
                     int(wall / 2) if any(
                         s and s.state in ("attention", "blocked")
                         and len(s.sublabel) > 55 for s in layout.keys)
                     else 0)                                    # ask page tick
            if frame != prev_frame:
                surface.show(layout, lit, t=wall, pressed=pressed, fx=fx_phase)
                prev_frame = frame

            if once:
                return
            # Renderers live here on the deck machine: when the checkout
            # changes (git pull), re-exec with the same argv so the new
            # surfaces/meter/mural load without a manual relaunch. Never
            # while a key is held.
            if time.monotonic() - last_code_check > 5.0:
                last_code_check = time.monotonic()
                if not held and _Hub.code_mtime() > code_mtime0 + 0.5:
                    import os
                    if hasattr(surface, "close"):
                        surface.close()
                    link.close()
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            # A press interrupts the sleep so ring feedback is immediate;
            # a press also forces a re-poll so acks/state changes land fast.
            if wake.wait(theme.FRAME_INTERVAL if (flashing or fx_phase)
                         else min(0.25, poll_every / 4)):
                wake.clear()
                last_poll = 0.0
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(surface, "close"):
            surface.close()
        link.close()
