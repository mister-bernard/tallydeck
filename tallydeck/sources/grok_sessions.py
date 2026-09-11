"""Grok Build sessions → signals.

The third harness. Grok stores one directory per session under
~/.grok/sessions/<urlencoded-cwd>/<uuid>/ with `summary.json` + `updates.jsonl`.

Headless `--single` jobs (grok-oneshot, grok.sh --single) write the same
layout with `session_kind=headless`. Those are disposable: nobody answers
one, and a scoring run can leave dozens of them. Off by default
(`include_headless`), same reason Codex hides `codex exec`.

Tally bar is along the BOTTOM of the key (Claude: top, Codex: left/right
by account). Badge is X.

State from the tail of updates.jsonl:

  a tool_call still pending                 → WORKING
  agent thought/message, log still hot      → WORKING
  last agent message, log quiet             → SUCCESS
  mtime older than `stale`                  → dropped
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from ..signal import Signal, WORKING, SUCCESS, IDLE, ATTENTION
from ..paths import contrib_bin, state_dir
from ..titles import TitleSync, resolve_label, tmux_for
from .base import Source
from .claude_sessions import _age_str


def _iso_epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _update(rec: dict) -> dict:
    params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
    u = params.get("update")
    return u if isinstance(u, dict) else {}


def classify(records: list[dict]) -> str:
    """Whose move, from a chronological updates.jsonl tail."""
    pending: set[str] = set()
    saw_agent = False
    for rec in records:
        u = _update(rec)
        kind = u.get("sessionUpdate")
        if kind == "tool_call":
            tid = str(u.get("toolCallId") or "")
            if tid:
                pending.add(tid)
            saw_agent = False
        elif kind == "tool_call_update":
            tid = str(u.get("toolCallId") or "")
            st = str(u.get("status") or "").lower()
            if tid and st == "completed":
                pending.discard(tid)
            elif tid and st != "completed":
                pending.add(tid)
            saw_agent = False
        elif kind in ("agent_message_chunk", "agent_thought_chunk"):
            saw_agent = True
    if pending:
        return WORKING
    if saw_agent:
        return SUCCESS
    return IDLE


def _tail_records(path: Path, max_bytes: int = 65536) -> list[dict]:
    try:
        size = path.stat().st_size
        with open(path, "r", errors="replace") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            raw = fh.read()
    except OSError:
        return []
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _summary(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _child_activity(sess: Path) -> tuple[int, int]:
    """(n_children, n_still_running) from Grok's subagents/ metadata."""
    sub = sess / "subagents"
    n = running = 0
    if not sub.is_dir():
        return 0, 0
    for meta in sub.glob("*/meta.json"):
        try:
            d = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        n += 1
        if d.get("completed_at"):
            continue
        st = str(d.get("status") or "").lower()
        if st in ("completed", "failed", "cancelled", "done"):
            continue
        running += 1
    return n, running


def _cwd_from_group(name: str) -> str:
    try:
        return unquote(name)
    except Exception:
        return name


def _spawned_panes() -> dict[str, str]:
    """cwd → tmux target for live tally-spawn grok sessions."""
    root = state_dir() / "spawned"
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for fp in root.glob("*.json"):
        try:
            d = json.loads(fp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if str(d.get("harness") or "").lower() != "grok":
            continue
        cwd = os.path.realpath(str(d.get("cwd") or ""))
        target = str(d.get("target") or "")
        if cwd and target:
            out[cwd] = target
    return out


class GrokSessionsSource(Source):
    """opts: root, stale, stall, dwell, flash_for, socket, account,
    include_headless, sync_titles, sync_every."""

    group = "gk"

    def __init__(self, **opts):
        super().__init__(**opts)
        self.root = Path(opts.get("root", Path.home() / ".grok" / "sessions")
                         ).expanduser()
        self.stale = float(opts.get("stale", 1800))
        self.stall = float(opts.get("stall", 900))
        self.dwell = float(opts.get("dwell", 15))
        self.flash_for = float(opts.get("flash_for", 300))
        self.socket = str(opts.get("socket", "/tmp/tmux-1000/cc"))
        self.account = str(opts.get("account", "X"))
        self.include_headless = bool(opts.get("include_headless", False))
        self.burn_window = float(opts.get("burn_window", 600))
        self._samples: dict[str, list[tuple[float, int]]] = {}
        self.tmux = tmux_for(self.socket)
        self.sync = TitleSync(self.tmux, opts.get("sync_every")) \
            if opts.get("sync_titles", True) else None

    def _burn_rate(self, session: str, now: float, size: int) -> float:
        samples = self._samples.setdefault(session, [])
        samples.append((now, size))
        while samples and now - samples[0][0] > self.burn_window:
            samples.pop(0)
        if len(samples) < 2:
            return 0.0
        dt = samples[-1][0] - samples[0][0]
        return max(0.0, (samples[-1][1] - samples[0][1]) / max(dt, 1.0))

    def poll(self) -> list[Signal]:
        now = time.time()
        if not self.root.is_dir():
            return []
        panes = _spawned_panes()
        titled: list[tuple[str, str]] = []
        signals: list[Signal] = []
        for group in self.root.iterdir():
            if not group.is_dir():
                continue
            for sess in group.iterdir():
                if not sess.is_dir():
                    continue
                sig = self._one(sess, group.name, panes, now)
                if sig is None:
                    continue
                if sig.meta.get("tmux") and not sig.meta.get("oneshot"):
                    titled.append((sig.meta["tmux"], sig.label))
                signals.append(sig)
        if self.sync:
            self.sync.push(titled)
        return signals

    def _one(self, sess: Path, group: str, panes: dict[str, str],
             now: float) -> Signal | None:
        summary_p = sess / "summary.json"
        log_p = sess / "updates.jsonl"
        info = _summary(summary_p)
        kind = str(info.get("session_kind") or "")
        if kind == "subagent":
            # Child work is rolled into the parent tile, not a second key.
            return None
        if kind == "headless" and not self.include_headless:
            return None
        from ..park import is_parked
        uuid = str((info.get("info") or {}).get("id") or sess.name)
        if is_parked(uuid=uuid):
            return None
        cwd = str((info.get("info") or {}).get("cwd")
                  or _cwd_from_group(group))
        try:
            st = log_p.stat() if log_p.is_file() else summary_p.stat()
        except OSError:
            return None
        mtime = st.st_mtime
        active = _iso_epoch(str(info.get("last_active_at") or ""))
        if active:
            mtime = max(mtime, active)
        if now - mtime > self.stale:
            return None
        records = _tail_records(log_p) if log_p.is_file() else []
        state = classify(records)
        if state == WORKING and (now - mtime) > self.stall:
            state = IDLE
        last_text = ""
        chat = sess / "chat_history.jsonl"
        if chat.is_file() and state in (SUCCESS, IDLE):
            from ..transcripts import recent_texts
            from .claude_sessions import asks_question
            texts = recent_texts(chat, "grok", 1)
            last_text = texts[0] if texts else ""
            if last_text and asks_question(last_text):
                state = ATTENTION
        ended = state in (SUCCESS, ATTENTION)
        if ended and (now - mtime) < self.dwell:
            state, ended = WORKING, False
        oneshot = kind == "headless"
        if oneshot and state != WORKING:
            # A finished one-shot is not a key. Live ones are visibility only.
            return None
        pane = panes.get(os.path.realpath(cwd), "") if cwd else ""
        title = str(info.get("generated_title") or info.get("session_summary")
                    or "")
        info_pane = self.tmux.info(pane) if pane else None
        sess_name = pane.split(":", 1)[0] if pane else ""
        label = resolve_label(harness_title=title if not oneshot else "",
                              pane=info_pane, session_name=sess_name,
                              cwd=cwd) or "grok"
        rate = self._burn_rate(uuid, now, st.st_size)
        sub = _age_str(now - mtime)
        if rate >= 20:
            sub += f" · {rate * 60 / 1024:.0f}k/m"
        n_kids, n_run = _child_activity(sess)
        if n_run and state in (SUCCESS, IDLE):
            state = WORKING
        if state == SUCCESS:
            sub = f"done · {_age_str(now - mtime)}"
        if n_run:
            extra = f"{n_run} running"
            sub = extra if not sub else f"{extra} · {sub}"
        elif n_kids and state == SUCCESS:
            sub = f"{n_kids} agents done · " + sub if sub else f"{n_kids} agents done"
        if state == ATTENTION and last_text:
            import re as _re
            sub = _re.sub(r"[*_`#]+", "", last_text)[:280]
        action = None
        if not oneshot:
            route = contrib_bin("tally-popup-route")
            if route:
                action = {"type": "cmd", "argv": [
                    route, pane, uuid, cwd, label[:24], state, self.account,
                    f"{self.group}/{uuid}"]}
        return Signal(
            id=f"{self.group}/{uuid}",
            action=action,
            label=label[:24],
            sublabel=sub,
            state=state,
            updated=mtime,
            priority=-10 if oneshot else int(min(rate, 1_000_000)),
            group=self.group,
            meta={"project": cwd, "session": uuid,
                  "account": self.account, "harness": "grok",
                  "oneshot": oneshot, "exact_pane": bool(pane),
                  "tmux": pane},
        )
