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
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from .paths import state_dir

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

# ONE length, everywhere. G, 2026-09-08: "realistically we don't have that
# many characters for titles on the Stream Deck, so we shouldn't use whole
# sentences — let's just make them really concise and nice." A pane border
# could hold more, but then the border and the key would say different
# things about the same session, which is the confusion this whole file
# exists to remove.
KEY_LIMIT = 24
TITLE_LIMIT = KEY_LIMIT

# Conversational run-up. "So, tell me about yourself" is about yourself, not
# about so.
_FILLER = re.compile(
    r"^(?:so|ok|okay|hey|hi|yo|right|now|well|please|pls|quick(?:ly)?|"
    r"can you|could you|would you|i want you to|i'd like you to|"
    r"i would like you to|i need you to|let'?s|lets|go ahead and|"
    r"just|actually|btw|fyi)\b[\s,:—-]*", re.I)

# Words that carry no topic. G, 2026-09-08: "we don't have that many
# characters for titles on the Stream Deck, so we shouldn't use whole
# sentences — let's just make them really concise and nice." Two to four
# words, ~12-20 characters. Getting there from a chat opener means throwing
# away the scaffolding of the sentence and keeping the nouns: "I'm just
# testing if this shows up in the thingamajig properly" is about a
# thingamajig, and every other word in it is grammar.
_STOP = frozenset("""
a an the this that these those there here it its it's is are was were be been
being am i i'm i've i'd i'll me my mine we we're our ours us you you're you've
your yours he him his she her hers they them their theirs
and or but if then than so as because since while when where what which who
whom whose why how of in on at to for with without from by about into over
under again further once all any both each few more most other some such no
nor not only own same too very can could shall should will would may might
must do does did doing done have has had having get gets got getting go goes
going went make makes made making let lets put puts run runs ran
please just really quite pretty maybe perhaps actually basically simply kind
sort thing things stuff bit lot lots way ways
tell tells told say says said see sees saw look looks think thinks want wants
need needs know knows like likes working up out off down now new
show shows showing shown properly correctly exactly currently still again
also even ever back first last next best better good great nice pretty
heard hear everything something anything nothing yes yeah nope thanks thank
""".split())

# Never worth a key: a bare number, a single letter, punctuation.
_WORD = re.compile(r"[A-Za-z0-9][\w.+#/@-]*")


def _clean(text: str) -> str:
    """Strip everything that is chrome rather than subject."""
    t = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    t = re.sub(r"`[^`\n]*`", " ", t)
    t = re.sub(r"^\s*(?:\[[^\]]{0,80}\]|<[^>\n]{0,80}>)\s*", "", t)
    t = re.sub(r"^\s*#{1,6}\s*", "", t)
    t = t.split("\n\n")[0].strip().splitlines()[0] if t.strip() else ""
    t = _FILLER.sub("", t.strip())
    t = re.sub(r"[*_`#>]+", "", t)
    return " ".join(t.split()).strip(" .,:;|·—-")


def _fit(text: str, limit: int) -> str:
    """Word-boundary truncation — a title sheared mid-word reads as damage."""
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp >= max(6, limit // 3)
            else text[:limit]).rstrip(" .,:;|·—-")


def headline(text: str, limit: int = 24) -> str:
    """A 2-4 word topic for a 96-pixel key.

    Text that is already a title — Claude Code's ai-title, a window a human
    named, a spawn slug — passes through untouched; it is short and it is
    about the subject already. A sentence does not: it is mostly grammar, and
    the first 24 characters of one ("I'm just testing if this") name nothing.
    Those get reduced to the content words, in the order they were written,
    which is the closest thing to a topic available without asking a model
    what the conversation is about.
    """
    t = _clean(text)
    if not t:
        return ""
    if len(t) <= limit and not t.endswith("?"):
        return t                        # already a title; leave it alone
    # First sentence first: a question or a one-liner followed by detail.
    first = re.split(r"(?<=[.!?])\s+", t)[0].rstrip(" .!?,:;")
    if 4 <= len(first) <= limit:
        return first[:1].upper() + first[1:]
    # Bare numbers are never a topic ("… (Run separately?): 1 — 1").
    words = [w for w in _WORD.findall(t) if w.lower() not in _STOP
             and len(w) > 1 and not w.isdigit()][:8]
    topic = _pack(words, limit)
    # A path, a slug or an identifier IS the subject of the sentence it is in.
    # If packing had to skip one to fit a couple of short verbs, the verbs
    # were the wrong choice — "Read directory Answer" says nothing that
    # "autopilot/PLAYBOOK.md" does not say better. Only a genuinely long token
    # earns this: ordinary words like "categorization" must not displace the
    # three that came before them.
    if words:
        anchor = max(words, key=len)
        if len(anchor) >= max(12, limit * 2 // 3) and len(anchor) <= limit \
                and anchor not in topic.split():
            topic = _pack(words[words.index(anchor):], limit)
    if len(topic) < 3:                  # nothing but grammar: fall back
        topic = _fit(t, limit)
    topic = _fit(topic, limit)
    return topic[:1].upper() + topic[1:] if topic else ""


# Words that join two halves of a phrase and say nothing on their own. They
# survive _STOP because they carry meaning IN the middle of a title ("Postgres
# vs SQLite"), but a title that ENDS on one is a sentence cut off mid-thought:
# "Self-hosted storage vs" is what the packer produced when the other half did
# not fit in 24 characters, and it reads as damage rather than as a topic.
_DANGLING = frozenset(
    "vs versus via per plus using and or but with for to"
    # Prepositions and articles cannot dangle out of the REDUCER — they are
    # stopwords, so they never survive into a packed topic. They can dangle
    # out of `trim`, which does not filter anything: a Claude ai-title cut at
    # 24 characters gave the key "Truncated responses from".
    " from of in on at by into over under after before without about"
    " than that the a an".split())


def trim(text: str, limit: int = KEY_LIMIT) -> str:
    """Shorten something that is ALREADY a title. Never reduce it.

    Reduction throws away every word that carries no topic, which is the
    right move on a sentence and destruction on a title: Claude Code's
    "Getting everything up and running" is 33 characters, so it missed the
    pass-through by nine, got shredded to its one non-stopword, and G's deck
    key read `Running`. A title is short because someone already did the
    choosing; all it can need is a word-boundary cut, and no trailing
    connective left dangling by that cut.
    """
    words = _fit(_clean(text), limit).split()
    while len(words) > 1 and words[-1].lower() in _DANGLING:
        words.pop()
    return " ".join(words)


def _pack(words: list[str], limit: int, most: int = 4) -> str:
    """As many of these words, in order, as fit — skipping any that do not."""
    out: list[str] = []
    for w in words:
        if len(" ".join(out + [w])) <= limit:
            out.append(w)
            if len(out) == most:
                break
    while len(out) > 1 and out[-1].lower() in _DANGLING:
        out.pop()
    return " ".join(out)


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
    # Rows to look back through on a cold start. Every live session logs
    # steadily (an analytics POST every few minutes), so a few thousand rows
    # covers the whole fleet; a session quiet for longer than that falls back
    # to the cwd match, which is what the fallback is for.
    COLD_ROWS = 4000

    def __init__(self, home=None, cache: bool = True):
        self.home = home
        self._titles: dict[str, tuple[str, str]] = {}   # id → (title, cwd)
        self._titles_ts = 0.0
        self._pid_threads: dict[int, list[tuple[int, str]]] = {}
        self._last_log_id = -1
        # Only the real Codex home shares a cache. A test (or anything else)
        # pointing at its own directory must not write that directory's row
        # ids into the file the live hub reads.
        self._cache = cache and home is None
        self._load_cache()

    # The cron pass is a fresh process every minute, and the backward scan it
    # would otherwise start from cost two seconds against a 74 MB log. What it
    # learns is durable — a pid's thread does not change — so it is kept on
    # disk and each run reads only the rows Codex appended since.
    def _cache_path(self) -> Path:
        return state_dir() / "codex-pid-threads.json"

    def _load_cache(self) -> None:
        if not self._cache:
            return
        try:
            rec = json.loads(self._cache_path().read_text())
            if rec.get("db") != _codex_db("logs", self.home):
                return                 # Codex migrated: the ids are not ours
            self._pid_threads = {int(p): [tuple(x) for x in seq]
                                 for p, seq in rec.get("pids", {}).items()}
            self._last_log_id = int(rec.get("last_id", -1))
        except (OSError, ValueError, TypeError, AttributeError):
            self._pid_threads, self._last_log_id = {}, -1

    def _save_cache(self) -> None:
        if not self._cache or self._last_log_id < 0:
            return
        # Only pids that still exist: the map would otherwise grow forever
        # with every Codex process the machine has ever run.
        live = {str(p): seq for p, seq in self._pid_threads.items()
                if Path(f"/proc/{p}").exists()}
        try:
            from .paths import write_json_atomic
            write_json_atomic(self._cache_path(),
                              {"db": _codex_db("logs", self.home),
                               "last_id": self._last_log_id, "pids": live})
        except OSError:
            pass                       # a cache we cannot write is not an error

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
                    # Cold start — the cron pass is a fresh process every
                    # minute. Walk BACKWARDS from the newest row and stop:
                    # only a thread that logged recently can be running in a
                    # pane now, and Codex's log is 74 MB after one busy day,
                    # so "the last N ids" (which still reads every row in
                    # that id range, body column and all) cost seconds per
                    # run and grew with the file.
                    rows = con.execute(
                        "select id, process_uuid, thread_id from logs "
                        "where thread_id is not null order by id desc limit ?",
                        (self.COLD_ROWS,)).fetchall()
                    rows.reverse()
                else:
                    rows = con.execute(
                        "select id, process_uuid, thread_id from logs "
                        "where id > ? and thread_id is not null order by id",
                        (since,)).fetchall()
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
        if rows:
            self._save_cache()
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


# ── the rename ledger ───────────────────────────────────────────────────────

# Every bulk retitle this system performs writes what it did to
# ~/.tallydeck/title-renames-<stamp>.json, one record per pane:
#
#   {"pane": "%276", "old_title": "Getting everything up and running",
#    "old_source": "tallydeck", "new_title": "Storage expansion"}
#
# That file is the receipt, and it is the ONLY evidence that a title on a
# pane right now came from us rather than from a human — a retitle done
# outside `Tmux.set_pane_title` (a one-off script, an agent with a shell)
# writes @tally_title without the matching @tally_title_auto, and from then
# on the value looks exactly like something G typed.
#
# The consequence, seen on G's 2026-09-08 screenshot: main:1.4 read
# "Storage expansion" while the session in it had long since moved on to
# "Getting everything up and running", and nothing could ever correct it —
# resolve_label returns a manual title unconditionally and TitleSync refuses
# to overwrite one. A wrong title that cannot self-heal is worse than no
# title: G reads the deck to know which session is which.
_LEDGER_TTL = 30.0
_ledger: dict = {"ts": 0.0, "sig": None, "by_pane": {}}


def authored_titles(state: Path | None = None) -> dict[str, frozenset[str]]:
    """{pane id: titles tallydeck itself put on that pane}.

    Both sides of each record count. `new_title` is what we wrote; an
    `old_title` whose `old_source` was "tallydeck" was ours too, so a ledger
    that got replayed or half-applied still cannot leave a value we authored
    looking human.
    """
    root = Path(state) if state is not None else state_dir()
    files = sorted(glob.glob(str(root / "title-renames-*.json")))
    try:
        sig = tuple((f, os.stat(f).st_mtime_ns) for f in files)
    except OSError:
        sig = tuple(files)
    now = time.time()
    if _ledger["sig"] == sig and now - _ledger["ts"] < _LEDGER_TTL:
        return _ledger["by_pane"]
    by_pane: dict[str, set[str]] = {}
    for fp in files:
        try:
            recs = json.loads(Path(fp).read_text())
        except (OSError, ValueError):
            continue                   # a torn receipt is not a human's title
        if not isinstance(recs, list):
            continue
        for r in recs:
            if not isinstance(r, dict):
                continue
            pane = str(r.get("pane") or "")
            if not pane:
                continue
            seen = by_pane.setdefault(pane, set())
            new = str(r.get("new_title") or "").strip()
            if new:
                seen.add(new)
            old = str(r.get("old_title") or "").strip()
            if old and str(r.get("old_source") or "") == SRC_AUTO:
                seen.add(old)
    frozen = {k: frozenset(v) for k, v in by_pane.items()}
    _ledger.update(ts=now, sig=sig, by_pane=frozen)
    return frozen


def record_renames(records: list[dict], state: Path | None = None) -> str:
    """Append a receipt for a batch of retitles. Returns the file written.

    Anything that sets @tally_title outside `Tmux.set_pane_title` must call
    this, or the value it wrote becomes permanently sticky.
    """
    root = Path(state) if state is not None else state_dir()
    root.mkdir(parents=True, exist_ok=True)
    fp = root / f"title-renames-{time.strftime('%Y%m%d-%H%M%S')}.json"
    fp.write_text(json.dumps(list(records), ensure_ascii=False, indent=2))
    _ledger["sig"] = None              # next read rebuilds
    return str(fp)


# ── tmux ────────────────────────────────────────────────────────────────────

class PaneInfo:
    """A pane, and the two titles on it.

    We keep a copy of what we last wrote (`@tally_title_auto`) beside the
    live value (`@tally_title`). Anything that makes them differ — G typing
    `tmux set-option -p @tally_title "Deck titles"`, another agent setting one
    — is by definition not ours, and is never overwritten. A flag saying
    "tallydeck wrote this" could not tell the difference: the flag would still
    be sitting there from OUR last write when a human overwrote the value.

    `authored` is the escape hatch for the one case that breaks: a title THIS
    SYSTEM wrote without the handshake, which the rename ledger names (see
    `authored_titles`). Ours is ours however it got there, and ours is always
    correctable.
    """

    __slots__ = ("target", "pane_id", "session", "window", "window_index",
                 "window_panes", "title", "title_auto", "window_auto",
                 "authored")

    def __init__(self, target, pane_id, session, window, window_index,
                 window_panes, title, title_auto, window_auto="",
                 authored=frozenset()):
        self.target = target
        self.pane_id = pane_id
        self.session = session
        self.window = window
        self.window_index = window_index
        self.window_panes = window_panes
        self.title = title             # @tally_title
        self.title_auto = title_auto   # @tally_title_auto — our last write
        self.window_auto = window_auto  # @tally_window_auto — our last rename
        self.authored = authored       # titles the ledger says we wrote here

    @property
    def manual_title(self) -> str:
        """A title we did not write is a human's and outranks everything."""
        if not self.title or self.title == self.title_auto:
            return ""
        if self.title in self.authored:
            return ""                  # ours, just written without the flag
        return self.title

    @property
    def manual_window(self) -> str:
        """A window name WE wrote is a copy of the title, not a decision about
        it. Reading it back as if a human had chosen it would freeze the label
        at whatever the title said the first time we synced."""
        if self.window and self.window == self.window_auto:
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
        self._by_id: dict[str, PaneInfo] = {}
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
            "#{@tally_title_auto}", "#{@tally_window_auto}", "#{@tally_title}",
            "#{window_name}"))
        out: dict[str, PaneInfo] = {}
        authored = authored_titles()
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
                                 npanes, f[7], f[5], f[6],
                                 authored.get(f[1], frozenset()))
        if out:
            self._panes, self._ts = out, now
            self._by_id = {p.pane_id: p for p in out.values() if p.pane_id}
        return self._panes

    def info(self, target: str) -> PaneInfo | None:
        return self.panes().get(target)

    def by_id(self, pane_id: str) -> PaneInfo | None:
        """A pane by its STABLE id (%276), whatever position it now holds."""
        if not pane_id:
            return None
        self.panes()
        return self._by_id.get(pane_id)

    def target_for_id(self, pane_id: str) -> str:
        """Where `%276` lives right now, or ''.

        `session:window.pane_index` is a POSITION, not an identity: close one
        pane and every pane after it in that window renumbers, so a target
        remembered five minutes ago can name its neighbour. That is the
        off-by-one behind the 08:57 bulk retitle, which moved "Storage box
        expansion to 5 TB" from %277 onto %276 — a whole window of sessions
        wearing each other's names. Anything that persists a pane must
        persist the id and re-resolve the target through this.
        """
        p = self.by_id(pane_id)
        return p.target if p else ""

    def set_pane_title(self, target: str, title: str) -> None:
        self._run("set-option", "-p", "-t", target, "@tally_title", title)
        self._run("set-option", "-p", "-t", target, "@tally_title_auto", title)

    def rename_window(self, session: str, index: str, name: str) -> None:
        target = f"{session}:{index}"
        self._run("rename-window", "-t", target, name)
        self._run("set-option", "-w", "-t", target, "@tally_window_auto", name)


_TMUX: dict[str, Tmux] = {}


def tmux_for(socket: str) -> Tmux:
    """One pane table per socket per process — both session sources want the
    same list-panes output, and asking tmux twice a second for it twice is
    two subprocesses per poll for nothing."""
    t = _TMUX.get(socket)
    if t is None:
        t = _TMUX[socket] = Tmux(socket)
    return t


def registry_target(sid8: str, tmux: Tmux, state: Path | None = None) -> str:
    """The pane a session recorded for itself (~/.tallydeck/panes/<sid8>.json),
    re-resolved through the pane id it recorded with it.

    The record is written from INSIDE the pane by the session's own hook, so
    it is the one durable session→pane identity we have. What it stores is a
    target, and a target is a position: a pane closed anywhere earlier in that
    window shifts it onto a neighbour, and then the deck labels, routes and
    answers into the wrong agent. The id pins it; the recorded target is only
    the fallback for records written before ids were kept.
    """
    root = Path(state) if state is not None else state_dir()
    try:
        rec = json.loads((root / "panes" / f"{sid8}.json").read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(rec, dict):
        return ""
    pane_id = str(rec.get("pane_id") or "")
    if pane_id:
        # The id is the answer, either way: if that pane is gone, whatever
        # holds its old target now is a DIFFERENT session, and routing a
        # press — or pasting an answer — into it is the failure this exists
        # to prevent. No pane is the honest result.
        return tmux.target_for_id(pane_id)
    target = str(rec.get("tmux") or "")
    return target if target and target in tmux.panes() else ""


# ── the resolver ────────────────────────────────────────────────────────────

def resolve_label(*, harness_title: str = "", harness_prose: str = "",
                  pane: PaneInfo | None = None, session_name: str = "",
                  cwd: str = "", limit: int = KEY_LIMIT) -> str:
    """The one precedence order, applied identically on both harnesses.

    `harness_title` is a title the harness generated (Claude Code's
    ai-title); `harness_prose` is a raw message the harness happens to file
    under "title" (Codex's threads.title is the first thing the operator
    typed). They are shortened differently and must not be confused: prose
    has to be reduced to the words that carry the topic, a title only ever
    needs trimming. Everything else here — a name G typed, a window, a spawn
    slug, a directory — is a title by definition.
    """
    if pane is not None:
        manual = pane.manual_title
        if manual:
            return trim(manual, limit)
        # A window name only names a session when there is one session in it.
        if pane.window_panes == 1 and pane.manual_window:
            return trim(pane.manual_window, limit)
        session_name = session_name or pane.session
    if session_name and not _AUTO_SESSION.match(session_name):
        return trim(session_name, limit)
    for text, shorten in ((harness_title, trim), (harness_prose, headline)):
        if text:
            out = shorten(text, limit)
            if out:
                return out
    if session_name:
        return trim(session_name, limit)
    return trim(os.path.basename(cwd.rstrip("/")) or cwd, limit)


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
                info.title = info.title_auto = title
                writes += 1
            # A single-pane window can carry the title in its name, where the
            # window list and the terminal window title can both see it. A
            # window a human has named is left alone.
            short = title[:self.WINDOW_LIMIT]
            if info.window_panes == 1 and not info.manual_window \
                    and info.window != short:
                self.tmux.rename_window(info.session, info.window_index, short)
                info.window = info.window_auto = short
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
