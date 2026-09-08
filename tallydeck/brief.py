"""Brief: what is this session doing, and what does it want from me.

Rendered into a tmux popup when a deck key is pressed, so landing in a
session starts with context instead of a bare prompt. Everything is read
from artifacts that already exist — the session log, git, and (optionally)
whatever task list you point it at — so a brief costs nothing to keep
current and lies only when those do.

Cached per session keyed on the log's (mtime, size): the brief is a pure
function of the log, so that key is correctness, not a staleness gamble.
The task-queue lookup (the slow part) is cached for a minute globally.
"""

from __future__ import annotations

import json
import os
import shlex
import zlib
import subprocess
import time
from pathlib import Path

from .paths import cache_dir
from .sources.claude_sessions import _tail_lines, _load

WIDTH = 74
CACHE_DIR = cache_dir()

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


def _codex_session_file(session: str, roots: list[Path]) -> Path | None:
    """Codex rollouts live at root/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl —
    the uuid is a filename suffix, not the stem, so this can't reuse
    `_session_file`'s glob."""
    for root in roots:
        for hit in root.rglob(f"rollout-*-{session}.jsonl"):
            return hit
    return None


def _find_session(session: str, roots: list[Path],
                   codex_roots: list[Path]) -> tuple[Path | None, str]:
    """(path, kind) — kind picks which transcript parser reads it. Claude
    checked first: cheap glob, and a session id collision across harnesses
    is not a real-world case worth optimizing for."""
    if not session:
        return None, ""
    fp = _session_file(session, roots)
    if fp:
        return fp, "claude"
    fp = _codex_session_file(session, codex_roots)
    if fp:
        return fp, "codex"
    return None, ""


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


def _last_texts_codex(path: Path, want: int = 2) -> list[str]:
    """Same job as `_last_texts`, for Codex's record shapes: a completed
    turn's answer lives in `event_msg/task_complete.last_agent_message`;
    mid-turn (no task_complete yet) falls back to the last assistant
    `response_item`. Mirrors `sources/codex_sessions.py::_last_agent_text`,
    just collecting up to `want` instead of only the newest.

    Codex writes a turn's final text TWICE — once as the `response_item`
    itself, once summarized onto the `task_complete` event right after —
    so consecutive duplicates are collapsed to one turn."""
    out: list[str] = []
    for ln in reversed(_tail_lines(path)):
        rec = _load(ln)
        if rec is None:
            continue
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        text = ""
        if rec.get("type") == "event_msg" and p.get("type") == "task_complete":
            text = str(p.get("last_agent_message") or "")
        elif rec.get("type") == "response_item" and p.get("type") == "message" \
                and p.get("role") == "assistant":
            text = " ".join(str(c.get("text", "")) for c in p.get("content", [])
                            if isinstance(c, dict))
        txt = " ".join(text.split())
        if len(txt) > 40 and (not out or out[-1] != txt):
            out.append(txt)
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


def _tasks(project: str, tasks_cmd: list[str] | None = None,
           limit: int = 3) -> list[str]:
    """Open queue items mentioning this project — via a 60s global cache,
    because spawning the command is the slowest thing a brief does.

    Optional hook: `tasks_cmd` is any argv that prints your task list on
    stdout (config `[brief] tasks_cmd`, or $TALLY_TASKS_CMD). Unset, the
    brief simply omits the section — nothing external is required.
    """
    name = os.path.basename(project.rstrip("/"))
    if not name:
        return []
    argv = list(tasks_cmd or [])
    if not argv:
        argv = shlex.split(os.environ.get("TALLY_TASKS_CMD", ""))
    if not argv:
        return []
    argv = [str(Path(a).expanduser()) if a.startswith("~") else a
            for a in argv]
    cache = CACHE_DIR / "tasks.txt"
    text = ""
    try:
        if cache.is_file() and time.time() - cache.stat().st_mtime < 60:
            text = cache.read_text()
    except OSError:
        pass
    if not text:
        try:
            r = subprocess.run(argv,
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
          roots: list[Path] | None = None, state: str = "",
          tasks_cmd: list[str] | None = None, ask: str = "",
          codex_roots: list[Path] | None = None) -> str:
    roots = roots or [Path.home() / ".claude" / "projects"]
    codex_roots = codex_roots or [Path.home() / ".codex" / "sessions"]
    title = label or os.path.basename(project.rstrip("/")) or project
    sc, dot = STATE_C.get(state, STATE_C["idle"])
    rule = f"  {RULE}{'─' * WIDTH}{R}"
    pad = "  "
    L: list[str] = [""]

    fp, kind = _find_session(session, roots, codex_roots)
    quiet = f" · quiet {_age(time.time() - fp.stat().st_mtime)}" if fp else ""

    # header: state dot, name, state chip
    chip = f"{sc}{state.upper()}{R}{DIM}{quiet}{R}" if state else ""
    L.append(f"{pad}{sc}{dot}{R}  {TITLE}{title}{R}   {chip}")
    L.append(f"{pad}   {PATH}{project}{R}")
    L.append(rule)

    # An explicitly raised ask (tally raise / a hook) comes first: it is the
    # reason the key was flashing, and it may be the only thing there is —
    # a raised flag need not have a session log behind it at all.
    if ask:
        L.append(f"{pad}{BOLD}THE ASK{R}")
        L.append("")
        for ln in _wrap(" ".join(ask.split())[:800], WIDTH - 4):
            L.append(f"{pad}{sc}▌{R} {ln}")
        L.append("")

    # the ask / where it left off — a block quote in the state color
    if fp:
        texts = _last_texts_codex(fp) if kind == "codex" else _last_texts(fp)
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
    tasks = _tasks(project, tasks_cmd)
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
                 roots: list[Path] | None = None, state: str = "",
                 tasks_cmd: list[str] | None = None, ask: str = "",
                 codex_roots: list[Path] | None = None) -> str:
    """Cache keyed on the session log's identity — same log, same brief."""
    roots = roots or [Path.home() / ".claude" / "projects"]
    codex_roots = codex_roots or [Path.home() / ".codex" / "sessions"]
    args = (session, project, label, roots, state, tasks_cmd, ask, codex_roots)
    fp, _ = _find_session(session, roots, codex_roots)
    if fp is None:
        return build(*args)
    try:
        st = fp.stat()
        stamp = (f"{st.st_mtime_ns}:{st.st_size}:{state}:"
                 f"{zlib.crc32(ask.encode('utf-8', 'replace')):08x}")
    except OSError:
        return build(*args)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"brief-{session[:8]}.ans"
    try:
        head, _, body = cache.read_text().partition("\n")
        if head == stamp:
            return body
    except OSError:
        pass
    out = build(*args)
    try:
        cache.write_text(stamp + "\n" + out)
    except OSError:
        pass
    return out
