"""Presence: which sessions are in front of the operator right now.

The deck used to rank by state alone, so a Telegram group lane that had
finished an hour ago sat above the session G was working in, and a quiet
pane in a window he had open was simply absent. Presence is the missing
axis. The hub sees the tmux server, so it can say for every session key:

  VIEW      3  its pane is in a window an attached terminal is SHOWING
  ATTACHED  2  its tmux session is attached, but on another window
  LIVE      1  it runs on this box, nobody is looking at it
  NONE      0  no pane and no live process — a log, not a session

`meta.presence` carries the tier to the deck, where signal.sort_key puts
it ahead of state for everything that is not asking for the operator.

Three filters ride on the same table:

  * a session with no pane and no live process is dropped unless it is
    asking for the operator — a log that stopped is history, not work;
  * a background session (LIVE) that has been quiet for `background_quiet`
    seconds is dropped — it is running, but it is not what anyone is doing;
  * a Telegraph lane (its process carries TELEGRAPH_PANE_SESSION_ID) never
    turns amber from the "ends with a question" heuristic — its questions
    are addressed to the chat it answers, not to the operator. Hook asks,
    raised flags and the harness's own "waiting" status still count.

Real terminals are the ones `tally-popup-route` already trusts: attached,
active in the last 12 h, and not one of the router's own floating
sessions. Fail open: when tmux cannot be read, signals pass untouched.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .signal import Signal, ATTENTION, BLOCKED, SUCCESS, IDLE, OFFLINE

VIEW, ATTACHED, LIVE, NONE = 3, 2, 1, 0
URGENT = (BLOCKED, ATTENTION)
# Sources that emit one key per harness session. Raised flags (sig), fleets,
# the burn meter and exec boards are not sessions and are never filtered.
SESSION_GROUPS = ("cc", "codex", "cx", "grok", "gk")
LANE_ENV = b"TELEGRAPH_PANE_SESSION_ID="


class Presence:
    def __init__(self, socket: str = "/tmp/tmux-1000/cc",
                 background_quiet: float = 3 * 3600,
                 client_active: float = 12 * 3600, ttl: float = 2.0,
                 runner=subprocess.run):
        self.socket = socket
        self.background_quiet = float(background_quiet)
        self.client_active = float(client_active)
        self.ttl = float(ttl)
        self._run = runner
        self._ts = 0.0
        self._snap: dict | None = None
        self._lanes: dict[tuple, bool] = {}

    # ── tmux snapshot ────────────────────────────────────────────────────────

    def _tmux(self, *args) -> list[str] | None:
        try:
            r = self._run(["tmux", "-S", self.socket, *args],
                          capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        return r.stdout.splitlines()

    def snapshot(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        if self._snap is not None and now - self._ts < self.ttl:
            return self._snap
        clients = self._tmux("list-clients", "-F",
                             "#{client_session}|#{window_id}|#{client_activity}")
        panes = self._tmux("list-panes", "-a", "-F",
                           "#{session_name}:#{window_index}.#{pane_index}|"
                           "#{pane_id}|#{window_id}|#{session_name}|#{pane_pid}")
        if clients is None or panes is None:
            self._snap = None
            return None
        viewed: set[str] = set()
        attached: set[str] = set()
        for ln in clients:
            sess, _, rest = ln.partition("|")
            win, _, act = rest.partition("|")
            if not sess or sess.startswith(("_fl-", "fl-")):
                continue
            try:
                if now - float(act or 0) > self.client_active:
                    continue
            except ValueError:
                continue
            viewed.add(win)
            attached.add(sess)
        by_target: dict[str, dict] = {}
        windows_of: dict[str, set[str]] = {}
        for ln in panes:
            parts = ln.split("|")
            if len(parts) != 5:
                continue
            target, pane_id, win, sess, pid = parts
            by_target[target] = by_id = {"pane_id": pane_id, "window": win,
                                         "session": sess, "pid": pid}
            by_target[pane_id] = by_id
            windows_of.setdefault(sess, set()).add(win)
        # Grouped sessions share windows: a window counts as attached when ANY
        # attached session holds it, not only the one named in the pane row.
        attached_windows: set[str] = set()
        for sess in attached:
            attached_windows |= windows_of.get(sess, set())
        self._snap = {"panes": by_target, "viewed": viewed,
                      "attached": attached_windows}
        self._ts = now
        return self._snap

    def tier(self, sig: Signal, snap: dict) -> int:
        m = sig.meta or {}
        pane = snap["panes"].get(str(m.get("tmux") or ""))
        if pane is None:
            return LIVE if m.get("live") else NONE
        if pane["window"] in snap["viewed"]:
            return VIEW
        if pane["window"] in snap["attached"]:
            return ATTACHED
        return LIVE

    def is_lane(self, sig: Signal, snap: dict) -> bool:
        pane = snap["panes"].get(str((sig.meta or {}).get("tmux") or ""))
        if pane is None:
            return False
        key = (pane["pane_id"], pane["pid"])
        if key not in self._lanes:
            if len(self._lanes) > 512:
                self._lanes.clear()
            self._lanes[key] = _tree_has_env(pane["pid"], LANE_ENV)
        return self._lanes[key]

    # ── the pass ─────────────────────────────────────────────────────────────

    def apply(self, signals: list[Signal], now: float | None = None) -> list[Signal]:
        now = time.time() if now is None else now
        snap = self.snapshot(now)
        if snap is None:
            return signals                     # fail open
        out: list[Signal] = []
        for s in signals:
            m = s.meta
            if m.get("meter"):
                out.append(s)
                continue
            tier = self.tier(s, snap)
            m["presence"] = tier
            session_key = s.group in SESSION_GROUPS and bool(m.get("session"))
            if not session_key:
                out.append(s)
                continue
            lane = self.is_lane(s, snap)
            if lane:
                m["lane"] = True
                if s.state == ATTENTION and not m.get("asked"):
                    # The lane's question went to its chat. Done, not "you".
                    s.state, s.flash = SUCCESS, None
                    s.sublabel = "replied · " + _age(now - s.updated)
            if s.state in URGENT:
                out.append(s)
                continue
            if tier == NONE:
                continue                       # a log, not a running session
            if tier == LIVE and s.state in (SUCCESS, IDLE, OFFLINE) \
                    and now - s.updated > self.background_quiet:
                continue                       # running, but nobody's work
            out.append(s)
        return out


def _tree_has_env(pid: str, needle: bytes, limit: int = 32) -> bool:
    """Does any process in this pane's tree carry `needle` in its env?"""
    try:
        stack = [int(pid)]
    except ValueError:
        return False
    try:
        r = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True,
                           text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    kids: dict[int, list[int]] = {}
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen = 0
    while stack and seen < limit:
        p = stack.pop()
        seen += 1
        try:
            if needle in Path(f"/proc/{p}/environ").read_bytes():
                return True
        except OSError:
            pass
        stack.extend(kids.get(p, []))
    return False


def _age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"
