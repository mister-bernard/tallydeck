"""Claude Code sessions → signals.

Ports the cc-fkeys heuristics: scan ~/.claude/projects/*/*.jsonl, read the
tail, and infer state from the last record:

  last record "user"       → the human spoke last; Claude is processing → WORKING
  last record "assistant"  → Claude finished and is waiting on the human → ATTENTION
  anything else            → IDLE
  mtime older than `stale` → dropped entirely

Press: tries to focus a tmux pane whose cwd matches the session's project.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from ..signal import Signal, WORKING, ATTENTION, IDLE
from .base import Source

TAIL_BYTES = 65536
MAX_LINES = 200


def _tail_lines(path: Path) -> list[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    if size <= TAIL_BYTES:
        chunk = path.read_bytes()
    else:
        with open(path, "rb") as fh:
            fh.seek(-TAIL_BYTES, 2)
            chunk = fh.read()
    return chunk.decode("utf-8", errors="replace").strip().splitlines()[-MAX_LINES:]


def _last_record_type(lines: list[str]) -> str:
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            return rec.get("type", "")
    return ""


def _snippet(lines: list[str], limit: int = 120) -> str:
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
            continue
        content = (rec.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            content = [content]
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item["text"].split("<thinking>")[0].strip()
                if text:
                    return " ".join(text.split())[:limit]
    return ""


def _demunge(dirname: str) -> str:
    """'-home-me-projects-foo' → 'foo' (best-effort short name)."""
    return dirname.lstrip("-").split("-")[-1] or dirname


class ClaudeSessionsSource(Source):
    """opts: root (str), stale (sec, default 1800), group (default 'cc')."""

    group = "cc"

    def __init__(self, **opts):
        super().__init__(**opts)
        # Sessions for different Claude accounts live under different HOMEs,
        # and which account a session is spending is not visible anywhere on
        # the key otherwise — you can watch a session work with no idea which
        # of the two quotas it is draining.
        roots = opts.get("roots")
        if roots:
            self.roots = [(str(r.get("label", "")), Path(r["path"]).expanduser())
                          for r in roots]
        else:
            self.roots = [("", Path(opts.get(
                "root", Path.home() / ".claude" / "projects")).expanduser())]
        self.root = self.roots[0][1]      # kept: existing callers/tests read it
        self.stale = float(opts.get("stale", 1800))
        # Dwell: tool results are logged as "user" records, so mid-turn the
        # tail flaps assistant/user/assistant… Only a log that has been
        # QUIET for `dwell` seconds with an assistant tail is truly waiting
        # on the human; a fresh assistant tail is just Claude still working.
        self.dwell = float(opts.get("dwell", 15))

    def poll(self) -> list[Signal]:
        signals: list[Signal] = []
        now = time.time()
        for acct, root in self.roots:
            if not root.is_dir():
                continue
            signals.extend(self._scan(root, acct, now))
        return signals

    def _scan(self, root, acct: str, now: float) -> list[Signal]:
        signals: list[Signal] = []
        for proj_dir in root.iterdir():
            if not proj_dir.is_dir():
                continue
            for fp in proj_dir.glob("*.jsonl"):
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                if now - mtime > self.stale:
                    continue
                lines = _tail_lines(fp)
                last = _last_record_type(lines)
                state = {"user": WORKING, "assistant": ATTENTION}.get(last, IDLE)
                if state == ATTENTION and (now - mtime) < self.dwell:
                    state = WORKING
                # Full readable path, kept for tmux matching on press.
                full = "/" + proj_dir.name.lstrip("-").replace("-", "/")
                signals.append(Signal(
                    id=f"{self.group}/{fp.stem[:8]}",
                    label=_demunge(proj_dir.name)[:14],
                    sublabel=_age_str(now - mtime),
                    detail=_snippet(lines),
                    state=state,
                    updated=mtime,
                    group=self.group,
                    meta={"project": full, "session": fp.stem,
                          "account": acct,
                          # Resolved hub-side because only the hub can see
                          # tmux. Without it the deck machine would have to
                          # re-derive the pane over ssh on every press.
                          "tmux": self._pane_for(full)},
                ))
        # One key per project: keep the most recent session of each label.
        best: dict[str, Signal] = {}
        for s in signals:
            k = s.meta["project"]
            if k not in best or s.updated > best[k].updated:
                best[k] = s
        return list(best.values())


    # ── tmux resolution ──────────────────────────────────────────────────────

    _PANE_TTL = 15.0   # panes move rarely; a press must not wait on a scan

    def _panes(self) -> dict[str, str]:
        """{realpath(cwd): target} for every pane, cached briefly."""
        now = time.time()
        if now - getattr(self, "_pane_ts", 0.0) < self._PANE_TTL:
            return getattr(self, "_pane_cache", {})
        out_map: dict[str, str] = {}
        try:
            r = subprocess.run(
                ["tmux", "list-panes", "-a", "-F",
                 "#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}"],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    target, _, cwd = line.partition(" ")
                    if cwd:
                        out_map[os.path.realpath(cwd)] = target
        except (OSError, subprocess.TimeoutExpired):
            pass          # fail open: no target is better than no signal
        self._pane_cache, self._pane_ts = out_map, now
        return out_map

    def _pane_for(self, project: str) -> str:
        key = os.path.realpath(project) if os.path.exists(project) else project
        return self._panes().get(key, "")

    def on_press(self, sig: Signal, long: bool = False) -> bool:
        """Focus the tmux pane working in this project, if one exists."""
        project = sig.meta.get("project", "")
        if not project:
            return False
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-a", "-F",
                 "#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}"],
                capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if out.returncode != 0:
            return False
        proj_real = os.path.realpath(project) if os.path.exists(project) else project
        for line in out.stdout.strip().splitlines():
            target, _, cwd = line.partition(" ")
            cwd_real = os.path.realpath(cwd) if os.path.exists(cwd) else cwd
            if cwd_real == proj_real or cwd_real.startswith(proj_real + "/") \
               or proj_real.startswith(cwd_real + "/"):
                subprocess.run(["tmux", "switch-client", "-t", target],
                               capture_output=True, timeout=3)
                subprocess.run(["tmux", "select-window", "-t", target],
                               capture_output=True, timeout=3)
                return True
        return False


def _age_str(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"
