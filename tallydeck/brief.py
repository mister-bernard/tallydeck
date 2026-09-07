"""Brief: what is this session doing, and what does it want from me.

Rendered into a tmux popup when a deck key is pressed, so landing in a
session starts with context instead of a bare prompt. Everything is read
from artifacts that already exist — the session log, git, the task queue —
so a brief costs nothing to keep current and lies only when those do.

Cached per session keyed on the log's (mtime, size): the brief is a pure
function of the log, so that key is correctness, not a staleness gamble.
The task-queue lookup (the slow part) is cached for a minute globally.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

WIDTH = 74
CACHE_DIR = Path.home() / ".tallydeck" / "cache"

# ── palette (matches the deck) ───────────────────────────────────────────────

R = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
TITLE = "\033[1;38;2;242;246;255m"          # bright white, bold
PATH = "\033[38;2;110;116;138m"             # cool gray
RULE = "\033[38;2;44;48;62m"
EARLIER = "\033[38;2;120;126;148m"
STATE_C = {
    "blocked": ("\033[38;2;229;72;77m", "●"),
    "attention": ("\033[38;2;255;178;36m", "●"),
    "working": ("\033[38;2;62;155;255m", "●"),
    "success": ("\033[38;2;67;183;93m", "●"),
    "idle": ("\033[38;2;110;116;138m", "○"),
    "offline": ("\033[38;2;70;74;90m", "○"),
}
CYAN = "\033[38;2;0;229;255m"


def _wrap(text: str, width: int, prefix: str = "") -> list[str]:
    out, line = [], ""
    for word in text.split():
        cand = f"{line} {word}".strip()
        if len(cand) > width and line:
            out.append(prefix + line)
            line = word
        else:
            line = cand
    if line:
        out.append(prefix + line)
    return out


def _session_file(session: str, roots: list[Path]) -> Path | None:
    for root in roots:
        for hit in root.glob(f"*/{session}.jsonl"):
            return hit
    return None


def _last_texts(path: Path, want: int = 2, tail: int = 400_000) -> list[str]:
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


def _git(project: str) -> str:
    if not os.path.isdir(os.path.join(project, ".git")):
        return ""
    def run(*args):
        try:
            r = subprocess.run(["git", "-C", project, *args],
                               capture_output=True, text=True, timeout=4)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    if not branch:
        return ""
    dirty = len([x for x in run("status", "--porcelain").splitlines()
                 if x.strip()])
    last = run("log", "-1", "--format=%h %s (%cr)")
    parts = [branch, f"{dirty} uncommitted" if dirty else "clean"]
    if last:
        parts.append(last[:44])
    return "  ·  ".join(parts)


def _tasks(project: str, limit: int = 3) -> list[str]:
    """Open queue items mentioning this project — via a 60s global cache,
    because spawning the runner is the slowest thing a brief does."""
    name = os.path.basename(project.rstrip("/"))
    if not name:
        return []
    cache = CACHE_DIR / "tasks.txt"
    text = ""
    try:
        if cache.is_file() and time.time() - cache.stat().st_mtime < 60:
            text = cache.read_text()
    except OSError:
        pass
    if not text:
        runner = Path.home() / "bin" / "my-task-queue.py"
        if not runner.is_file():
            return []
        try:
            r = subprocess.run(["python3", str(runner), "list"],
                               capture_output=True, text=True, timeout=8)
            text = r.stdout
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(text)
        except (OSError, subprocess.TimeoutExpired):
            return []
    hits = []
    for ln in text.splitlines():
        if name.lower() in ln.lower():
            # strip runner chrome down to the readable tail
            body = ln.strip()
            if "—" in body:
                body = body.split("—", 1)[1].strip()
            hits.append(body[:WIDTH - 6])
        if len(hits) >= limit:
            break
    return hits


def _age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


# ── rendering ────────────────────────────────────────────────────────────────

def build(session: str, project: str, label: str = "",
          roots: list[Path] | None = None, state: str = "") -> str:
    roots = roots or [Path.home() / ".claude" / "projects"]
    title = label or os.path.basename(project.rstrip("/")) or project
    sc, dot = STATE_C.get(state, STATE_C["idle"])
    rule = f"  {RULE}{'─' * WIDTH}{R}"
    pad = "  "
    L: list[str] = [""]

    fp = _session_file(session, roots) if session else None
    quiet = f" · quiet {_age(time.time() - fp.stat().st_mtime)}" if fp else ""

    # header: state dot, name, state chip
    chip = f"{sc}{state.upper()}{R}{DIM}{quiet}{R}" if state else ""
    L.append(f"{pad}{sc}{dot}{R}  {TITLE}{title}{R}   {chip}")
    L.append(f"{pad}   {PATH}{project}{R}")
    L.append(rule)

    # the ask / where it left off — a block quote in the state color
    if fp:
        texts = _last_texts(fp)
        L.append(f"{pad}{BOLD}WHERE IT LEFT OFF{R}")
        L.append("")
        if texts:
            for ln in _wrap(texts[0][:640], WIDTH - 4):
                L.append(f"{pad}{sc}▌{R} {ln}")
            if len(texts) > 1:
                L.append("")
                for ln in _wrap(texts[1][:240], WIDTH - 6):
                    L.append(f"{pad}  {EARLIER}{ln}{R}")
        else:
            L.append(f"{pad}{DIM}(no recent messages in the log){R}")
        L.append("")

    git = _git(project)
    tasks = _tasks(project)
    if git or tasks:
        L.append(rule)
    if git:
        L.append(f"{pad}{DIM}repo {R} {git}")
    for t in tasks:
        L.append(f"{pad}{DIM}task {R} {CYAN}▸{R} {t}")
    if git or tasks:
        L.append("")

    return "\n".join(L)


def build_cached(session: str, project: str, label: str = "",
                 roots: list[Path] | None = None, state: str = "") -> str:
    """Cache keyed on the session log's identity — same log, same brief."""
    roots = roots or [Path.home() / ".claude" / "projects"]
    fp = _session_file(session, roots) if session else None
    if fp is None:
        return build(session, project, label, roots, state)
    try:
        st = fp.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}:{state}"
    except OSError:
        return build(session, project, label, roots, state)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"brief-{session[:8]}.ans"
    try:
        head, _, body = cache.read_text().partition("\n")
        if head == stamp:
            return body
    except OSError:
        pass
    out = build(session, project, label, roots, state)
    try:
        cache.write_text(stamp + "\n" + out)
    except OSError:
        pass
    return out
