"""One task title per session, shared by the deck, tmux and the manager.

G's complaint, 2026-09-08: every Codex key on the deck said "openclaw", and
so did the pane border in the cc manager — five sessions doing five different
things, all wearing the name of the directory they happened to start in. The
fix is not a rename: it is a single resolved title per session that every
surface reads.

WHERE THE TITLE COMES FROM — both harnesses already write one, we were just
not reading it:

  Claude Code   `{"type":"ai-title","aiTitle":"Aviation logs analysis"}`
                records in the session's own .jsonl. A real summary, updated
                as the conversation turns. It also pushes this into the pane
                title via OSC, which is why Claude panes already read well in
                tmux and Codex panes do not.
  Codex         `threads.title` in ~/.codex/state_*.sqlite — the first user
                message of the thread, with the AGENTS.md instruction bundle
                already skipped by Codex itself. Not a summary, so we take a
                headline off the front of it.

WHAT WINS. A name a human chose beats anything a model generated, because the
human chose it to find the thing again:

  1. `@tally_title` set on the pane by hand (any value we did not write)
  2. the window name, when the window holds exactly ONE pane, so the name is
     unambiguously about that session (a five-pane window named by hand names
     the WINDOW, not any one of the five sessions in it)
  3. the tmux session name, unless it is an auto one (`main`, `mainA`,
     `main-O`, `main-2`): `tally spawn <slug>` names sessions deliberately and
     the deck, the offer log and G all refer to them by that slug
  4. the harness title, headlined
  5. the directory basename — where we came in

WHERE IT GOES. `@tally_title` is a per-pane tmux user option: the application
in the pane cannot clobber it (Codex rewrites the real pane title on every
frame), tmux formats can read it, and it survives detach. From there
pane-border-format shows it in the manager and the deck's sources label their
keys with it. Windows that hold a single pane also get renamed when their
name is still a generic one, which carries the title into the window list and
the terminal window title. Sessions are NEVER renamed — every routing target,
the offer log and tally-popup-route are keyed on the session name.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

# tmux -F fields are split on this. Window names and titles carry anything a
# human types — spaces, pipes, colons — so the separator has to be something
# nobody types, and PRINTABLE: tmux renders a real control character in its
# output as the four literal characters "\037", which parses as nothing.
_SEP = "␟"                        # ␟, SYMBOL FOR UNIT SEPARATOR

# Session names `cc` hands out when nobody chose one. Everything else —
# c64, haystack-v5, pearlbridge, a `tally spawn` slug — was chosen.
_AUTO_SESSION = re.compile(r"^main(-[A-Za-z0-9]{1,3}|[A-Z])?$")

# Window names that name the program, not the work.
_GENERIC_WINDOW = {"", "1", "bash", "zsh", "sh", "fish", "claude", "claude-b",
                   "codex", "node", "python3", "python", "ssh", "mosh", "tmux",
                   "cc", "-"}

# Ours, so we know we may overwrite it. Anything else in @tally_title is a
# human's and is left alone forever.
SRC_AUTO = "tallydeck"

# Two lengths, deliberately. A 96px key wraps two short lines, so a key label
# is a headline; a pane border in the manager is as wide as the pane and can
# hold the whole subject. Same resolution, cut differently.
KEY_LIMIT = 24
TITLE_LIMIT = 64

# Conversational run-up. "So, tell me about yourself" is about yourself, not
# about so.
_FILLER = re.compile(
    r"^(?:so|ok|okay|hey|hi|yo|right|now|well|please|pls|quick(?:ly)?|"
    r"can you|could you|would you|i want you to|i'd like you to|"
    r"i would like you to|i need you to|let'?s|lets|go ahead and|"
    r"just|actually|btw|fyi)\b[\s,:—-]*", re.I)


def headline(text: str, limit: int = 24) -> str:
    """A key-sized headline off the front of a message.

    96 pixels of key, two wrapped lines. Machine tags (`[telegram reply ← …]`,
    `[tally] …`), markdown chrome and code spans say nothing at that size, and
    a message that opens with "So," wastes three of the characters that do.
    Cut on a word boundary — a title sheared mid-word reads as corruption.
    """
    t = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    t = re.sub(r"`[^`\n]*`", " ", t)
    t = re.sub(r"^\s*(?:\[[^\]]{0,80}\]|<[^>\n]{0,80}>)\s*", "", t)
    t = re.sub(r"^\s*#{1,6}\s*", "", t)
    t = t.split("\n\n")[0].strip().splitlines()[0] if t.strip() else ""
    t = _FILLER.sub("", t.strip())
    t = re.sub(r"[*_`#>]+", "", t)
    t = " ".join(t.split())
    # First sentence, when there is one and it is not the whole paragraph.
    first = re.split(r"(?<=[.!?])\s+", t)[0]
    if 4 <= len(first) <= limit + 12:
        t = first
    t = t.rstrip(" .,:;|·—-")
    if len(t) <= limit:
        return t
    cut = t[:limit + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp >= max(8, limit // 2)
            else t[:limit]).rstrip(" .,:;|·—-")


# ── Claude Code ─────────────────────────────────────────────────────────────

def claude_ai_title(lines: list[str]) -> str:
    """The session's own generated title, newest first.

    Claude Code rewrites this record as the conversation moves, so the last
    one in the tail is the current subject. Cheap: we are already holding the
    tail for the state classifier.
    """
    import json
    for ln in reversed(lines):
        if '"ai-title"' not in ln:
            continue
        try:
            rec = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict) and rec.get("type") == "ai-title":
            title = str(rec.get("aiTitle") or "").strip()
            # Claude Code's placeholder before it has titled anything.
            if title and title.lower() not in ("claude code", "untitled"):
                return title
    return ""


# ── Codex ───────────────────────────────────────────────────────────────────

def _codex_db(kind: str, home: str | os.PathLike | None = None) -> str:
    """Newest ~/.codex/<kind>_N.sqlite. The N is a schema generation Codex
    bumps on migrations; pinning one would silently stop working on upgrade."""
    root = Path(home or os.environ.get("CODEX_HOME")
                or (Path.home() / ".codex")).expanduser()
    found = sorted(glob.glob(str(root / f"{kind}_*.sqlite")))
    return found[-1] if found else ""


def _ro(path: str):
    """Read-only connection. Codex is writing to these while we read; WAL
    gives us a consistent snapshot and we never take a write lock."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)


class CodexState:
    """Codex's own thread table and log stream, read for two facts.

    `threads` gives {thread id: title, cwd}. `logs` gives the link nothing
    else does: every log row carries `process_uuid`, which is literally
    `pid:<pid>:<uuid>` — so the operating-system pid of a running Codex, which
    we can tie to a tmux pane, names the thread it is running. Before this,
    five Codex sessions started in one directory were mutually
    indistinguishable and none of them could be routed to.
    """

    TTL = 15.0
    COLD_ROWS = 50000

    def __init__(self, home=None):
        self.home = home
        self._titles: dict[str, tuple[str, str]] = {}   # id → (title, cwd)
        self._titles_ts = 0.0
        self._pid_threads: dict[int, list[tuple[int, str]]] = {}
        self._last_log_id = -1

    # threads --------------------------------------------------------------

    def titles(self) -> dict[str, tuple[str, str]]:
        now = time.time()
        if now - self._titles_ts < self.TTL and self._titles:
            return self._titles
        self._titles_ts = now
        db = _codex_db("state", self.home)
        if not db:
            return self._titles
        try:
            with _ro(db) as con:
                self._titles = {
                    str(r[0]): (str(r[1] or ""), str(r[2] or ""))
                    for r in con.execute(
                        "select id, title, cwd from threads "
                        "order by updated_at desc limit 400")}
        except sqlite3.Error:
            pass                       # fail open: no title beats no deck
        return self._titles

    def title(self, thread_id: str) -> str:
        return self.titles().get(thread_id, ("", ""))[0]

    # pid → thread ---------------------------------------------------------

    def pid_threads(self) -> dict[int, list[tuple[int, str]]]:
        """{pid: [(row id, thread id), …] newest last}.

        Read incrementally — the log table only ever grows, so after the first
        pass each poll reads the handful of rows Codex appended since.
        """
        db = _codex_db("logs", self.home)
        if not db:
            return self._pid_threads
        try:
            with _ro(db) as con:
                since = self._last_log_id
                if since < 0:
                    # Cold start (cron runs this in a fresh process every
                    # minute): read back a bounded window instead of Codex's
                    # whole log history, which only ever grows. A session too
                    # quiet to appear in it falls back to the cwd match.
                    top = con.execute("select max(id) from logs").fetchone()
                    since = max(0, int(top[0] or 0) - self.COLD_ROWS)
                rows = con.execute(
                    "select id, process_uuid, thread_id from logs "
                    "where id > ? and thread_id is not null order by id",
                    (since,)).fetchall()
                self._last_log_id = max(self._last_log_id, since)
        except (sqlite3.Error, TypeError, ValueError):
            return self._pid_threads
        for rid, puid, tid in rows:
            self._last_log_id = max(self._last_log_id, int(rid))
            puid = str(puid or "")
            if not puid.startswith("pid:"):
                continue
            pid_s = puid.split(":", 2)[1]
            if not pid_s.isdigit():
                continue
            seq = self._pid_threads.setdefault(int(pid_s), [])
            if not seq or seq[-1][1] != str(tid):
                seq.append((int(rid), str(tid)))
                del seq[:-8]           # only the recent few can be current
        return self._pid_threads

    def thread_for_pid(self, pid: int, allowed: set[str]) -> str:
        """The thread this process is running, restricted to `allowed`.

        A Codex process logs against more than its foreground thread —
        compaction and subagent threads appear too — so the newest id is not
        automatically the right one. `allowed` is the set of rollouts the deck
        is actually showing, which is exactly the set a press could land in.
        """
        for _, tid in reversed(self.pid_threads().get(int(pid), [])):
            if tid in allowed:
                return tid
        return ""


# ── tmux ────────────────────────────────────────────────────────────────────

class PaneInfo:
    __slots__ = ("target", "pane_id", "session", "window", "window_index",
                 "window_panes", "title", "title_src", "window_src")

    def __init__(self, target, pane_id, session, window, window_index,
                 window_panes, title, title_src, window_src=""):
        self.target = target
        self.pane_id = pane_id
        self.session = session
        self.window = window
        self.window_index = window_index
        self.window_panes = window_panes
        self.title = title             # @tally_title
        self.title_src = title_src     # @tally_title_src
        self.window_src = window_src   # @tally_window_src

    @property
    def manual_title(self) -> str:
        """A title we did not write is a human's and outranks everything."""
        return self.title if self.title and self.title_src != SRC_AUTO else ""

    @property
    def manual_window(self) -> str:
        """A window name WE wrote is a copy of the title, not a decision about
        it. Reading it back as if a human had chosen it would freeze the label
        at whatever the title said the first time we synced."""
        if self.window_src == SRC_AUTO:
            return ""
        if self.window in _GENERIC_WINDOW or self.window.isdigit():
            return ""
        return self.window


class Tmux:
    """One `list-panes -a` per window of time, shared by both sources."""

    TTL = 10.0

    def __init__(self, socket: str = "/tmp/tmux-1000/cc"):
        self.socket = socket
        self._panes: dict[str, PaneInfo] = {}
        self._ts = 0.0

    def _run(self, *args, timeout=3) -> str:
        try:
            r = subprocess.run(["tmux", "-S", self.socket, *args],
                               capture_output=True, text=True, timeout=timeout)
            return r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""                  # fail open: tmux gone, deck still runs

    def panes(self, force: bool = False) -> dict[str, PaneInfo]:
        now = time.time()
        if not force and now - self._ts < self.TTL:
            return self._panes
        fmt = _SEP.join((
            "#{session_name}:#{window_index}.#{pane_index}", "#{pane_id}",
            "#{session_name}", "#{window_index}", "#{window_panes}",
            "#{@tally_title_src}", "#{@tally_window_src}", "#{@tally_title}",
            "#{window_name}"))
        out: dict[str, PaneInfo] = {}
        for ln in self._run("list-panes", "-a", "-F", fmt).splitlines():
            f = ln.split(_SEP)
            if len(f) < 9:
                continue
            try:
                npanes = int(f[4] or 1)
            except ValueError:
                npanes = 1
            # window_name last: it is the only field a human types into.
            out[f[0]] = PaneInfo(f[0], f[1], f[2], _SEP.join(f[8:]), f[3],
                                 npanes, f[7], f[5], f[6])
        if out:
            self._panes, self._ts = out, now
        return self._panes

    def info(self, target: str) -> PaneInfo | None:
        return self.panes().get(target)

    def set_pane_title(self, target: str, title: str) -> None:
        self._run("set-option", "-p", "-t", target, "@tally_title", title)
        self._run("set-option", "-p", "-t", target, "@tally_title_src",
                  SRC_AUTO)

    def rename_window(self, session: str, index: str, name: str) -> None:
        target = f"{session}:{index}"
        self._run("rename-window", "-t", target, name)
        self._run("set-option", "-w", "-t", target, "@tally_window_src",
                  SRC_AUTO)


_TMUX: dict[str, Tmux] = {}


def tmux_for(socket: str) -> Tmux:
    """One pane table per socket per process — both session sources want the
    same list-panes output, and asking tmux twice a second for it twice is
    two subprocesses per poll for nothing."""
    t = _TMUX.get(socket)
    if t is None:
        t = _TMUX[socket] = Tmux(socket)
    return t


# ── the resolver ────────────────────────────────────────────────────────────

def resolve_label(*, harness_title: str = "", pane: PaneInfo | None = None,
                  session_name: str = "", cwd: str = "",
                  limit: int = KEY_LIMIT) -> str:
    """The one precedence order, applied identically on both harnesses."""
    if pane is not None:
        manual = pane.manual_title
        if manual:
            return headline(manual, limit)
        # A window name only names a session when there is one session in it.
        if pane.window_panes == 1 and pane.manual_window:
            return headline(pane.manual_window, limit)
        session_name = session_name or pane.session
    if session_name and not _AUTO_SESSION.match(session_name):
        return headline(session_name, limit)
    if harness_title:
        h = headline(harness_title, limit)
        if h:
            return h
    if session_name:
        return headline(session_name, limit)
    return headline(os.path.basename(cwd.rstrip("/")) or cwd, limit)


class TitleSync:
    """Push resolved titles back into tmux, so the manager and the deck agree.

    Change-only and throttled: a title that has not moved costs no tmux calls
    at all. Churn here would be visible as a pane border redrawing under G's
    cursor every two seconds.
    """

    EVERY = 20.0
    WINDOW_LIMIT = 32

    def __init__(self, tmux: Tmux, every: float | None = None):
        self.tmux = tmux
        self.every = self.EVERY if every is None else float(every)
        self._ts = 0.0

    def push(self, pairs, force: bool = False) -> int:
        """pairs: iterable of (pane target, title). Returns writes made."""
        now = time.time()
        if not force and now - self._ts < self.every:
            return 0
        self._ts = now
        panes = self.tmux.panes(force=True)
        writes = 0
        for target, title in pairs:
            title = " ".join(str(title or "").split())[:120]
            info = panes.get(target)
            if not title or info is None or info.manual_title:
                continue
            if info.title != title:
                self.tmux.set_pane_title(target, title)
                info.title = title
                info.title_src = SRC_AUTO
                writes += 1
            # A single-pane window can carry the title in its name, where the
            # window list and the terminal window title can both see it. A
            # window a human has named is left alone.
            short = title[:self.WINDOW_LIMIT]
            if info.window_panes == 1 and not info.manual_window \
                    and info.window != short:
                self.tmux.rename_window(info.session, info.window_index, short)
                info.window, info.window_src = short, SRC_AUTO
                writes += 1
        return writes


# ── deck-independent sync ───────────────────────────────────────────────────

def sync_once(config_path=None) -> int:
    """Build the configured sources, poll them once, let them push titles.

    The hub only runs while G's deck is connected; tmux and the manager are
    there all day. Run from cron, this keeps the pane borders and window names
    current with the deck unplugged. Sources push titles from their own poll,
    so this is exactly one poll and no second code path.
    """
    from . import config as _config
    from .sources import make as make_source
    cfg = _config.load(config_path)
    n = 0
    for spec in cfg["sources"]:
        spec = dict(spec)
        kind = spec.pop("kind", "")
        if kind not in ("claude-sessions", "codex-sessions"):
            continue               # only session sources carry titles
        try:
            n += len(make_source(kind, **spec).poll())
        except Exception:
            continue               # one broken source must not stop the rest
    return n


if __name__ == "__main__":            # python3 -m tallydeck.titles
    import sys
    print(f"[tally-titles] {sync_once()} sessions", file=sys.stderr)
    sys.exit(0)
