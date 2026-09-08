"""Brief: what is this session doing, and what does it want from me.

Rendered into a tmux popup when a deck key is pressed, so landing in a
session starts with context instead of a bare prompt. Everything is read
from artifacts that already exist — the session log, git, and (optionally)
whatever task list you point it at — so a brief costs nothing to keep
current and lies only when those do.

Presentation is rebuilt for each press: terminal size, labels and repository
state can change independently of the transcript. The optional task lookup is
cached for a minute globally.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .paths import cache_dir

CACHE_DIR = cache_dir()

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
    from .transcripts import recent_texts
    return recent_texts(path, "claude", want)


def _last_texts_codex(path: Path, want: int = 2) -> list[str]:
    from .transcripts import recent_texts
    return recent_texts(path, "codex", want)


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
            hits.append(body)
        if len(hits) >= limit:
            break
    return hits


def document(session: str, project: str, label: str = "",
             roots=None, state: str = "", tasks_cmd=None, ask: str = "",
             codex_roots=None):
    from .decisions import decision_text, decision_support
    from .transcripts import pending_ask
    roots = roots or [Path.home() / ".claude/projects"]
    codex_roots = codex_roots or [Path.home() / ".codex/sessions"]
    fp, kind = _find_session(session, roots, codex_roots)
    texts = (_last_texts_codex(fp) if kind == "codex" else _last_texts(fp)) if fp else []
    tool = pending_ask(fp, kind) if fp else ""
    # A current tool prompt supersedes prose. Earlier messages are support only.
    decision = ask or tool or (decision_text(texts[0]) if texts else "")
    sections = []
    if decision:
        # A supplied brief on a key that is NOT asking anything (a background
        # fleet's roster) is a status report, and heading it "THE ASK" would
        # invent a question. Anything derived from a transcript, and every
        # alarm state, keeps the original heading.
        sections.append((
            "THE ASK · answer in the session"
            if not ask or state in ("attention", "blocked") else "STATUS",
            decision))
        support = decision_support(texts[0], decision) if texts and not tool and not ask else ""
        if support:
            sections.append(("CHOICES / RECOMMENDATION · from the session", support))
    elif state in ("attention", "blocked"):
        sections.append(("ATTENTION", "No concrete decision found in the latest message. "
                         "This flag may have been raised deliberately; open the session to inspect it."))
    else:
        sections.append(("STATUS", "No decision requested in the latest message."))
    if texts:
        # A complete opening paragraph is an extract, never an invented summary.
        import re
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", texts[0]) if p.strip()]
        candidates = [p for p in paragraphs if len(p) <= 500 and p != decision
                      and not re.match(r"^(?:#|[-*] |\d+[.)] |\*\*[^*]+\*\*$)", p)]
        intro = "\n\n".join(candidates[:2])
        if intro and intro != decision and intro != texts[0]:
            sections.append(("SUMMARY · excerpt from latest message", intro))
        sections.append(("WHERE IT LEFT OFF · full latest message", texts[0]))
        for txt in texts[1:]: sections.append(("EARLIER · supporting context", txt))
    elif not ask and not tool:
        sections.append(("CONTEXT", "No recent assistant messages found."))
    if state == "success" and fp:
        # Reuse the existing artifact collector, placing its complete values in
        # the scrollable document rather than appending a clipped shell footer.
        from .paths import contrib_bin
        import importlib.machinery
        helper = contrib_bin("tally-results")
        if helper:
            import importlib.util
            loader = importlib.machinery.SourceFileLoader("tally_results", helper)
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            for title, values in zip(("FILES WRITTEN", "COMMITS", "LINKS"), module.gather(fp)):
                if values:
                    sections.append(("RESULTS · " + title, "\n".join("- " + str(v) for v in values)))
    git = _git(project)
    if git: sections.append(("REPOSITORY", git))
    tasks = _tasks(project, tasks_cmd)
    if tasks: sections.append(("RELATED TASKS", "\n\n".join(tasks)))
    return {"label": label or os.path.basename(project.rstrip("/")) or project,
            "state": state or "idle", "project": project, "sections": sections}


def render(doc, width):
    from .popup import header, body, Group
    return Group(header(doc["label"], doc["state"], width, doc["project"]),
                 *(body(text, title, width) for title, text in doc["sections"]))


def build(session: str, project: str, label: str = "", roots=None,
          state: str = "", tasks_cmd=None, ask: str = "", codex_roots=None,
          width: int | None = None) -> str:
    doc = document(session, project, label, roots, state, tasks_cmd, ask, codex_roots)
    width = width or shutil.get_terminal_size((80, 24)).columns
    try:
        from .popup import Console, THEME
        import io
        out = io.StringIO()
        console = Console(file=out, width=width, theme=THEME, force_terminal=True,
                          color_system="truecolor")
        console.print(render(doc, width))
        return out.getvalue()
    except ImportError:
        # Core CLI remains usable on hosts without the optional Rich popup stack.
        return "\n\n".join([f"{doc['label']} · {doc['state'].upper()}"] +
                           [title + "\n" + text for title, text in doc['sections']])


def build_cached(session: str, project: str, label: str = "", roots=None,
                 state: str = "", tasks_cmd=None, ask: str = "", codex_roots=None,
                 width: int | None = None) -> str:
    # Do not cache presentation (terminal width, label, repo and
    # tasks can change independently of its mtime). Full identity prevents UUIDv7
    # collisions; old brief-<prefix>.ans files are intentionally never consulted.
    return build(session, project, label, roots, state, tasks_cmd, ask, codex_roots, width)
