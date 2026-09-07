"""Brief: what is this session doing, and what does it want from me.

Printed into a tmux popup when you press a key, so landing in a session
starts with context instead of a bare prompt. Everything here is read
from artifacts that already exist — the session log, git, the task
queue — so a brief costs nothing to keep current and lies only when
those sources do.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

WIDTH = 76


def _wrap(text: str, width: int = WIDTH, indent: str = "  ") -> list[str]:
    out, line = [], indent
    for word in text.split():
        if len(line) + len(word) + 1 > width and line.strip():
            out.append(line)
            line = indent + word
        else:
            line = f"{line} {word}" if line.strip() else indent + word
    if line.strip():
        out.append(line)
    return out


def _session_file(session: str, roots: list[Path]) -> Path | None:
    for root in roots:
        for hit in root.glob(f"*/{session}.jsonl"):
            return hit
    return None


def _last_texts(path: Path, want: int = 2, tail: int = 400_000) -> list[str]:
    """Most recent assistant messages, newest first."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                txt = " ".join(item["text"].split())
                if len(txt) > 40:
                    out.append(txt)
                    break
        if len(out) >= want:
            break
    return out


def _git(project: str) -> list[str]:
    if not os.path.isdir(os.path.join(project, ".git")):
        return []
    def run(*args):
        try:
            r = subprocess.run(["git", "-C", project, *args],
                               capture_output=True, text=True, timeout=4)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    out = []
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    last = run("log", "-1", "--format=%h %s (%cr)")
    dirty = run("status", "--porcelain")
    if branch:
        n = len([x for x in dirty.splitlines() if x.strip()])
        out.append(f"  branch {branch}" +
                   (f" · {n} uncommitted file{'s' if n != 1 else ''}"
                    if n else " · clean"))
    if last:
        out.append(f"  last   {last}")
    return out


def _tasks(project: str, limit: int = 4) -> list[str]:
    """Open queue items mentioning this project, newest first."""
    runner = Path.home() / "bin" / "my-task-queue.py"
    if not runner.is_file():
        return []
    name = os.path.basename(project.rstrip("/"))
    if not name:
        return []
    try:
        r = subprocess.run(["python3", str(runner), "list"],
                           capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return []
    hits = [ln.strip() for ln in r.stdout.splitlines()
            if name.lower() in ln.lower()][:limit]
    return [f"  {h[:WIDTH - 2]}" for h in hits]


def build(session: str, project: str, label: str = "",
          roots: list[Path] | None = None, state: str = "") -> str:
    roots = roots or [Path.home() / ".claude" / "projects"]
    title = label or os.path.basename(project.rstrip("/")) or project
    L: list[str] = []
    L.append("")
    L.append(f"  \033[1;96m{title}\033[0m   \033[2m{project}\033[0m")
    if state:
        L.append(f"  \033[2mstate\033[0m  {state}")
    L.append("")

    fp = _session_file(session, roots) if session else None
    if fp:
        age = time.time() - fp.stat().st_mtime
        mins = int(age // 60)
        L.append(f"  \033[1mWHERE IT LEFT OFF\033[0m  "
                 f"\033[2m({mins}m ago)\033[0m" if mins else
                 "  \033[1mWHERE IT LEFT OFF\033[0m")
        texts = _last_texts(fp)
        if texts:
            L += _wrap(texts[0][:600])
            if len(texts) > 1:
                L.append("")
                L.append("  \033[2mbefore that:\033[0m")
                L += ["\033[2m" + x + "\033[0m"
                      for x in _wrap(texts[1][:280])]
        else:
            L.append("  (no assistant messages in the recent log)")
        L.append("")

    git = _git(project)
    if git:
        L.append("  \033[1mREPO\033[0m")
        L += git
        L.append("")

    tasks = _tasks(project)
    if tasks:
        L.append("  \033[1mOPEN TASKS\033[0m")
        L += tasks
        L.append("")

    L.append("  \033[2many key → the live session\033[0m")
    L.append("")
    return "\n".join(L)
