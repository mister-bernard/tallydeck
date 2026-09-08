"""Codex (OpenAI) sessions → signals.

The second harness on the deck. Codex writes a rollout log per session under
~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl, and its record types
say whose move it is far more plainly than Claude Code's do:

  event_msg/task_started            → a turn is running        → WORKING
  response_item/*, item_completed   → mid-turn                 → WORKING
  event_msg/task_complete, asks     → it wants an answer       → ATTENTION
  event_msg/task_complete, reports  → finished; nothing to do  → SUCCESS
  event_msg/turn_aborted            → interrupted, idle now    → IDLE
  nothing conversational                                       → IDLE
  mtime older than `stale`          → dropped entirely

The same ask/report distinction as Claude sessions (`asks_question`), so
amber means the same thing on both harnesses — the only difference on the
key is the tally bar down its left edge.

`session_meta.originator` separates the two ways Codex runs: `codex-tui` is a
person's session in a pane, `codex_exec` is a disposable one-shot from
codex-oneshot.sh. One-shots are off by default (`include_exec`): nobody
answers a one-shot, and four of them would push the fleet off an 8-key deck.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ..signal import Signal, WORKING, ATTENTION, SUCCESS, IDLE
from ..paths import contrib_bin
from ..titles import CodexState, TitleSync, resolve_label, tmux_for
from .base import Source
from .claude_sessions import _tail_lines, _load, asks_question, _age_str

# Bookkeeping: true of the log, silent about whose move it is.
_SKIP_EVENTS = ("token_count", "item_started", "item_updated",
                "agent_reasoning_delta", "agent_message_delta")


def classify(lines: list[str]) -> tuple[str, str]:
    """(state, last agent text) from the tail of a rollout log."""
    for ln in reversed(lines):
        rec = _load(ln)
        if rec is None:
            continue
        t = rec.get("type")
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if t == "event_msg":
            pt = p.get("type")
            if pt in _SKIP_EVENTS:
                continue
            if pt == "task_complete":
                text = str(p.get("last_agent_message") or "")
                return (ATTENTION if asks_question(text) else SUCCESS), text
            if pt == "turn_aborted":
                return IDLE, ""
            return WORKING, ""            # task_started, item_completed, …
        if t == "response_item":
            return WORKING, ""            # a message/tool call mid-turn
        # session_meta, turn_context, world_state, token_usage_record: skip
    return IDLE, ""


def _last_agent_text(lines: list[str], limit: int = 300) -> str:
    """What it said last — the sublabel of an ATTENTION key is the ask."""
    for ln in reversed(lines):
        rec = _load(ln)
        if rec is None:
            continue
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if rec.get("type") == "event_msg" and p.get("type") == "task_complete":
            msg = str(p.get("last_agent_message") or "")
            if msg:
                return " ".join(msg.split())[:limit]
        if rec.get("type") == "response_item" and p.get("type") == "message" \
                and p.get("role") == "assistant":
            text = " ".join(str(c.get("text", "")) for c in p.get("content", [])
                            if isinstance(c, dict))
            if text.strip():
                return " ".join(text.split())[:limit]
    return ""


def signal_id(uuid: str) -> str:
    """The whole thread id, because this is an identity and not a display.

    Codex thread ids are UUIDv7: the leading half is a millisecond timestamp.
    `cc codex` opens five panes at once, so all five threads shared their
    first hex digits — keyed on `uuid[:8]` they all became `cx/01a08020`, the
    hub's merge kept whichever it saw last, and four live sessions never
    reached the deck (G, 2026-09-08: "why doesn't the active Codex session
    show up"). Any truncation reintroduces that; the key's LABEL is what has
    to be short, not its id.
    """
    clean = "".join(c for c in str(uuid) if c.isalnum() or c in "-_.")
    return clean[:48] or "unknown"


def _meta(path: Path) -> dict:
    """The rollout's session_meta payload: uuid, cwd, originator, timestamp."""
    try:
        with open(path, "r", errors="replace") as fh:
            rec = json.loads(fh.readline() or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    p = rec.get("payload")
    return p if isinstance(p, dict) else {}


def _iso_epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _proc_start(pid: int) -> float | None:
    """Epoch seconds when a pid started — the anchor that ties a live Codex
    process to the rollout it opened."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = float(stat[stat.rfind(")") + 2:].split()[19])
        for ln in Path("/proc/stat").read_text().splitlines():
            if ln.startswith("btime "):
                return float(ln.split()[1]) + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        pass
    return None


class CodexSessionsSource(Source):
    """opts: root, stale, stall, dwell, flash_for, socket, account,
    include_exec, codex_home, sync_titles, sync_every."""

    group = "cx"

    def __init__(self, **opts):
        super().__init__(**opts)
        self.root = Path(opts.get("root", Path.home() / ".codex" / "sessions")
                         ).expanduser()
        self.stale = float(opts.get("stale", 1800))
        self.stall = float(opts.get("stall", 900))
        self.dwell = float(opts.get("dwell", 15))
        self.flash_for = float(opts.get("flash_for", 300))
        self.socket = str(opts.get("socket", "/tmp/tmux-1000/cc"))
        # The badge on the key: Codex is Account O in tokenburn, and the whole
        # point of the badge is knowing whose quota a session is draining.
        self.account = str(opts.get("account", "O"))
        self.include_exec = bool(opts.get("include_exec", False))
        self.burn_window = float(opts.get("burn_window", 600))
        self._samples: dict[str, list[tuple[float, int]]] = {}
        # Titles: Codex's own thread table, and the pane table both session
        # sources share. `sync_titles` writes the resolved title back into
        # tmux so the manager's pane borders say what the deck says.
        self.state = CodexState(opts.get("codex_home"))
        self.tmux = tmux_for(self.socket)
        self.sync = TitleSync(self.tmux, opts.get("sync_every")) \
            if opts.get("sync_titles", True) else None

    # ── polling ──────────────────────────────────────────────────────────────

    def poll(self) -> list[Signal]:
        now = time.time()
        signals: list[Signal] = []
        rollouts = self._recent_rollouts(now)
        panes = self._codex_panes(rollouts, now)
        titled: list[tuple[str, str]] = []
        for fp in rollouts:
            try:
                st = fp.stat()
            except OSError:
                continue
            mtime, size = st.st_mtime, st.st_size
            meta = _meta(fp)
            uuid = str(meta.get("session_id") or fp.stem[-36:])
            is_exec = str(meta.get("originator", "")).endswith("exec") \
                or meta.get("source") == "exec"
            if is_exec and not self.include_exec:
                continue
            lines = _tail_lines(fp)
            state, _ = classify(lines)
            if state == WORKING and (now - mtime) > self.stall:
                state = IDLE               # "working" with no output = stalled
            ended = state in (ATTENTION, SUCCESS)
            # Dwell: a task_complete written a second ago may be followed by
            # the next turn's task_started. Let the log settle first.
            if ended and (now - mtime) < self.dwell:
                state, ended = WORKING, False
            if is_exec and state == ATTENTION:
                state = WORKING            # nobody answers a one-shot
            cwd = str(meta.get("cwd") or "")
            pane = panes.get(uuid, "")
            # The title Codex itself keeps for this thread, ranked against the
            # names a human chose (see titles.resolve_label). Without it every
            # key in a five-pane window said "openclaw" — the directory they
            # all started in (G, 2026-09-08).
            info = self.tmux.info(pane) if pane else None
            harness_title = "" if is_exec else self.state.title(uuid)
            label = resolve_label(harness_title=harness_title, pane=info,
                                  cwd=cwd) or "codex"
            if pane and not is_exec:
                titled.append((pane, label))   # same topic on every surface
            rate = self._burn_rate(uuid, now, size)
            sub = _age_str(now - mtime)
            if rate >= 20:
                sub += f" · {rate * 60 / 1024:.0f}k/m"
            if state == SUCCESS:
                sub = f"done · {_age_str(now - mtime)}"
            snip = _last_agent_text(lines)
            if state == ATTENTION and snip:
                sub = snip[:280]
            flash = None
            if state == ATTENTION and (now - mtime) > self.flash_for:
                flash = False
            signals.append(Signal(
                id=f"{self.group}/{signal_id(uuid)}",
                action=self._action(pane, cwd, label, state, uuid),
                label=label[:24],
                sublabel=sub,
                detail=snip,
                flash=flash,
                state=state,
                updated=mtime,
                priority=-10 if is_exec else int(min(rate, 1_000_000)),
                group=self.group,
                meta={"project": cwd, "session": uuid,
                      "account": self.account,
                      # The one marker every surface keys the Codex look off.
                      "harness": "codex",
                      "oneshot": is_exec,
                      "exact_pane": bool(pane), "tmux": pane},
            ))
        if self.sync:
            self.sync.push(titled)
        return signals

    def _action(self, pane: str, cwd: str, label: str, state: str,
                uuid: str) -> dict | None:
        """Press → the same hub-side router every other session key uses.

        The uuid is deliberately NOT passed: the router's paneless fallback
        resumes with `claude --resume <uuid>`, which for a Codex session is a
        confusing failure. No live pane → the key still reports, it just has
        nowhere to send you."""
        if not pane:
            return None
        route = contrib_bin("tally-popup-route")
        if not route:
            return None
        return {"type": "cmd", "argv": [
            route, pane, "", cwd, label[:24], state, self.account,
            f"{self.group}/{signal_id(uuid)}"]}

    def _recent_rollouts(self, now: float) -> list[Path]:
        """Rollouts touched inside the stale window. The tree is one directory
        per day, so only the newest few can qualify — walking all of history
        every poll would be the whole cost of this source."""
        out: list[Path] = []
        if not self.root.is_dir():
            return out
        days: list[Path] = []
        for year in sorted((p for p in self.root.iterdir() if p.is_dir()),
                           reverse=True)[:1]:
            for month in sorted((p for p in year.iterdir() if p.is_dir()),
                                reverse=True)[:2]:
                days.extend(sorted((p for p in month.iterdir() if p.is_dir()),
                                   reverse=True)[:2])
        for day in days[:3]:
            for fp in day.glob("rollout-*.jsonl"):
                try:
                    if now - fp.stat().st_mtime <= self.stale:
                        out.append(fp)
                except OSError:
                    continue
        return out

    def _burn_rate(self, key: str, now: float, size: int) -> float:
        samples = self._samples.setdefault(key, [])
        samples.append((now, size))
        while samples and now - samples[0][0] > self.burn_window:
            samples.pop(0)
        if len(samples) < 2:
            return 0.0
        dt = samples[-1][0] - samples[0][0]
        return max(0.0, (samples[-1][1] - samples[0][1]) / max(dt, 1.0))

    # ── tmux resolution ──────────────────────────────────────────────────────

    _PANE_TTL = 15.0
    # Slack on the "after" test only. The rollout is written on the session's
    # first turn, but the process we can see is the inner binary, which the
    # node wrapper re-execs — measured 20 s behind its own first rollout. A
    # rollout from a PREVIOUS session in the same directory is minutes to
    # hours older and still excluded.
    _LAG_S = 300.0

    def _codex_panes(self, files=None, now: float | None = None) -> dict[str, str]:
        """{rollout uuid: pane target}.

        Codex puts no session id in its environment and does not hold its
        rollout open, but it does say which thread each of its processes is
        running: every row it writes to ~/.codex/logs_*.sqlite is stamped
        `pid:<pid>:<uuid>`. A pid we can find under a tmux pane therefore
        names its thread outright — no inference, and it holds for five
        sessions started in the same directory at the same second, which is
        precisely the case that used to collapse (main-O: five Codex panes,
        all in /home/openclaw, none routable, every key labelled "openclaw").

        The old cwd+start-order heuristic stays as the fallback for a process
        that has not logged a thread yet (a pane launched but never prompted):
        a rollout belongs to a process when they share a directory and the
        rollout began AFTER the process did. It still refuses to guess between
        two live processes in one directory — a guess must never route.
        """
        now = now or time.time()
        if now - getattr(self, "_cp_ts", 0.0) < self._PANE_TTL:
            return getattr(self, "_cp_cache", {})
        rollouts: list[tuple[float, str, str]] = []
        for fp in (self._recent_rollouts(now) if files is None else files):
            meta = _meta(fp)
            started = _iso_epoch(meta.get("timestamp"))
            uuid = str(meta.get("session_id") or "")
            cwd = str(meta.get("cwd") or "")
            exec_run = str(meta.get("originator", "")).endswith("exec") \
                or meta.get("source") == "exec"
            if uuid and started and cwd and not exec_run:
                rollouts.append((started, uuid, cwd))
        allowed = {r[1] for r in rollouts}
        out: dict[str, str] = {}
        unresolved: list[tuple[str, str, float, int]] = []
        for pr in self._live_codex_procs():
            tid = self.state.thread_for_pid(pr[3], allowed)
            if tid and tid not in out:
                out[tid] = pr[0]
            elif not tid:
                unresolved.append(pr)
        by_cwd: dict[str, list[tuple[str, str, float, int]]] = {}
        for pr in unresolved:
            by_cwd.setdefault(pr[1], []).append(pr)
        taken = set(out)
        for cwd, procs in by_cwd.items():
            if len(procs) != 1:
                continue
            target, _, proc_start, _pid = procs[0]
            mine = [r for r in rollouts if r[2] == cwd and r[1] not in taken
                    and r[0] >= proc_start - self._LAG_S]
            if mine:
                out[max(mine)[1]] = target
        self._cp_cache, self._cp_ts = out, now
        return out

    def _live_codex_procs(self) -> list[tuple[str, str, float, int]]:
        """[(pane target, cwd, start epoch, pid)] for every interactive
        `codex` running under a tmux pane on our socket."""
        out: list[tuple[str, str, float, int]] = []
        try:
            r = subprocess.run(
                ["tmux", "-S", self.socket, "list-panes", "-a", "-F",
                 "#{session_name}:#{window_index}.#{pane_index} #{pane_pid}"],
                capture_output=True, text=True, timeout=3)
            ps = subprocess.run(["ps", "-eo", "pid=,ppid="],
                                capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return out
        kids: dict[int, list[int]] = {}
        for ln in ps.stdout.splitlines():
            parts = ln.split()
            if len(parts) == 2:
                kids.setdefault(int(parts[1]), []).append(int(parts[0]))
        for ln in (r.stdout or "").strip().splitlines():
            target, _, pid_s = ln.partition(" ")
            if not pid_s.strip().isdigit():
                continue
            stack, seen = [int(pid_s)], 0
            while stack and seen < 64:
                pid = stack.pop()
                seen += 1
                stack.extend(kids.get(pid, []))
                try:
                    if Path(f"/proc/{pid}/comm").read_text().strip() != "codex":
                        continue
                    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                    if b"exec" in cmd[1:2]:          # one-shot, not a session
                        continue
                    cwd = os.path.realpath(f"/proc/{pid}/cwd")
                except OSError:
                    continue
                start = _proc_start(pid)
                if start:
                    out.append((target, cwd, start, pid))
        return out
