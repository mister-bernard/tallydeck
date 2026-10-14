"""Claude Code sessions → signals.

Scan ~/.claude/projects/*/*.jsonl, read the tail, and infer state from the
last CONVERSATIONAL record — the log is full of bookkeeping records
(attachment, system, cost-state, last-prompt, ai-title, mode…) that say
nothing about whose move it is, and on current Claude Code most tails end
in one of those. Skipping them:

  user (prompt or tool result)   → Claude's move                 → WORKING
  assistant, tool_use pending    → a tool is running             → WORKING
  assistant, AskUserQuestion /
    ExitPlanMode pending         → it is asking you              → ATTENTION
  assistant, turn ended, asks    → it is asking you              → ATTENTION
  assistant, turn ended, no ask  → it finished; nothing to do    → SUCCESS
  nothing conversational         →                                  IDLE
  mtime older than `stale`       → dropped entirely, unless a live
                                   Claude process still owns the session

Live identity comes from Claude Code's own process registry,
`<HOME>/.claude/sessions/<pid>.json`: session id, exact tmux pane id and
its status (busy · shell · idle · waiting), rewritten as the process
changes state. It is the authority on three things the log tail can only
guess at: which pane a session is in, whether it is still running at all,
and whether it is mid-turn. The tail still decides the one thing the
registry does not say — whether a finished turn ends in a question.

A `system/turn_duration` record after the last assistant record is Claude
Code's own end-of-turn marker: when it is present the verdict is final and
the dwell window (below) is skipped.

The distinction that matters on the deck: a session that merely FINISHED
is green and quiet. Only one that is WAITING ON AN ANSWER is amber and
flashes. Red is reserved for hard stops raised by hooks (permission prompt)
or explicitly by scripts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from dataclasses import replace

from ..signal import Signal, WORKING, ATTENTION, SUCCESS, IDLE, BLOCKED, _STATE_WEIGHT
from ..paths import signals_dir, acked_dir, contrib_bin, claude_account
from ..activity import ClaudeTail, Tails, assess
from ..titles import (TitleSync, claude_ai_title, registry_target,
                      resolve_label, tmux_for)
from .base import Source

TAIL_BYTES = 65536


def _rollup_by_tmux(signals: list[Signal]) -> list[Signal]:
    """One tile per chosen tmux session.

    Teammates, watchers and extra panes of a slug like subnetbridge stay
    visible as 'N running' on the parent instead of each taking a key.
    Human `main*` sessions stay per-pane — those are separate conversations.
    """
    from ..titles import _AUTO_SESSION
    groups: dict[str, list[Signal]] = {}
    passthrough: list[Signal] = []
    for s in signals:
        pane = str((s.meta or {}).get("tmux") or "")
        sess = pane.split(":")[0] if pane else ""
        if (not sess or _AUTO_SESSION.match(sess)
                or (s.meta or {}).get("oneshot")):
            passthrough.append(s)
            continue
        groups.setdefault(sess, []).append(s)
    out = list(passthrough)
    for _sess, members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        hottest = max(members, key=lambda m: (
            _STATE_WEIGHT.get(m.state, 0), m.updated))
        # Keep the session's own name (window 1 / matching label), but the
        # hottest child's state — a paused coordinator with live watchers
        # must still read as running.
        named = next((m for m in members
                      if (m.label or "").lower() == _sess.lower()
                      or ((m.meta or {}).get("tmux") or "").startswith(_sess + ":1.")),
                     hottest)
        primary = named
        if _STATE_WEIGHT.get(hottest.state, 0) > _STATE_WEIGHT.get(primary.state, 0):
            primary = replace(primary, state=hottest.state)
        n_run = sum(1 for m in members if m.state == WORKING)
        if n_run and primary.state in (SUCCESS, IDLE):
            primary = replace(primary, state=WORKING)
        extra = f"{len(members)} panes"
        if n_run:
            extra += f" · {n_run} running"
        sub = extra if not primary.sublabel else f"{extra} · {primary.sublabel}"
        meta = dict(primary.meta or {})
        meta["rolled"] = len(members)
        # Burn belongs to the session, not to whichever pane names it: any
        # burning pane makes the tile burn, at the hottest pane's rate.
        burners = [m for m in members if (m.meta or {}).get("burning")]
        if burners:
            meta["burning"] = True
        out.append(replace(primary, sublabel=sub, meta=meta,
                           priority=max(m.priority for m in members)))
    return out
MAX_LINES = 200


def _tail_lines(path: Path) -> list[str]:
    from ..transcripts import records_backwards
    lines = []
    size = 0
    for rec in records_backwards(path):
        line = json.dumps(rec, ensure_ascii=False)
        lines.append(line)
        size += len(line)
        if size >= TAIL_BYTES or len(lines) >= MAX_LINES:
            break
    return list(reversed(lines))


def _load(ln: str) -> dict | None:
    ln = ln.strip()
    if not ln:
        return None
    try:
        rec = json.loads(ln)
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) else None


# Tools that ARE the question: they block on the human by design.
ASK_TOOLS = ("AskUserQuestion", "ExitPlanMode")

def asks_question(text: str) -> bool:
    from ..decisions import decision_text
    return bool(decision_text(text))


def classify(lines: list[str]) -> tuple[str, bool]:
    """(state, final) from the tail of a session log.

    Walks backwards to the last conversational record, skipping the
    bookkeeping types. `final` is True when Claude Code's own end-of-turn
    marker (system/turn_duration) sits after that record — then the state
    is not a guess and needs no dwell."""
    ended = False
    for ln in reversed(lines):
        rec = _load(ln)
        if rec is None:
            continue
        t = rec.get("type")
        if t == "system":
            if rec.get("subtype") == "turn_duration":
                ended = True
            continue
        if t == "user":
            # A human prompt or a tool result: either way it is Claude's
            # move now.
            return WORKING, False
        if t != "assistant":
            continue                       # attachment, cost-state, …
        msg = rec.get("message") or {}
        content = msg.get("content", [])
        if not isinstance(content, list):
            content = [content]
        last = content[-1] if content else None
        if isinstance(last, dict) and last.get("type") == "tool_use":
            if last.get("name") in ASK_TOOLS:
                return ATTENTION, True
            return WORKING, False          # a tool is running
        if msg.get("stop_reason") == "tool_use":
            return WORKING, False
        text = "\n\n".join(str(i.get("text", "")) for i in content
                        if isinstance(i, dict) and i.get("type") == "text")
        return (ATTENTION if asks_question(text) else SUCCESS), ended
    return IDLE, False


def _ask_tool_pending(lines: list[str]) -> bool:
    """Is the session parked on a question TOOL (AskUserQuestion,
    ExitPlanMode)? That is the harness asking — unlike prose ending in '?'."""
    for ln in reversed(lines):
        rec = _load(ln)
        if rec is None or rec.get("type") not in ("assistant", "user"):
            continue
        if rec.get("type") == "user":
            return False
        content = (rec.get("message") or {}).get("content", [])
        last = content[-1] if isinstance(content, list) and content else None
        return isinstance(last, dict) and last.get("type") == "tool_use" \
            and last.get("name") in ASK_TOOLS
    return False


def _assistant_wants_input(lines: list[str]) -> bool:
    """Kept for callers/tests: does the tail wait on the human?"""
    return classify(lines)[0] == ATTENTION


def _snippet(lines: list[str], limit: int = 300) -> str:
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
                    from ..decisions import decision_text
                    return " ".join((decision_text(text) or text).split())[:limit]
    return ""


def _last_cwd(lines: list[str]) -> str:
    """The session's own records carry its real cwd — authoritative, unlike
    the munged directory name, which is lossy: '-a-b-c' cannot distinguish
    'a/b/c' from 'a/b-c', and pressing a key for my-web-app once ssh'd into
    the nonexistent my/web/app."""
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("cwd"):
            return str(rec["cwd"])
    return ""


def _resolve_munged(dirname: str) -> str:
    """Fallback: existence-checked reconstruction of a munged dir name.
    Greedy: at each step, consume as many dash-joined segments as still name
    a real directory before descending."""
    segs = dirname.lstrip("-").split("-")
    path = "/"
    i = 0
    while i < len(segs):
        for j in range(len(segs), i, -1):        # longest candidate first
            cand = os.path.join(path, "-".join(segs[i:j]))
            if os.path.isdir(cand):
                path, i = cand, j
                break
        else:
            path = os.path.join(path, "/".join(segs[i:]))  # best effort
            break
    return path


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
        # Accounts appear over time (C arrived 2026-09-12 and was never added
        # here, so every C session was missing from the deck for a month).
        # `discover` globs account HOMEs; each one's `.claude-<x>` suffix is
        # its badge. Explicit roots win on overlap.
        known = {os.path.realpath(p) for _, p in self.roots}
        for pattern in opts.get("discover", []) or []:
            import glob
            for home in sorted(glob.glob(os.path.expanduser(str(pattern)))):
                if not re.search(r"/\.claude-[A-Za-z0-9]{1,8}/?$", home):
                    continue                   # .claude-c.bak is not an account
                proj = Path(home) / ".claude" / "projects"
                if proj.is_dir() and os.path.realpath(proj) not in known:
                    known.add(os.path.realpath(proj))
                    self.roots.append((claude_account(home), proj))
        self.root = self.roots[0][1]      # kept: existing callers/tests read it
        self.stale = float(opts.get("stale", 1800))
        # A session whose tail says "Claude's move" but whose log has not
        # moved for this long is not working, it is stalled (killed mid-turn,
        # a hung tool, an abandoned pane). Blue would be a lie; it goes gray
        # and keeps its age. Long tool runs are the ceiling here.
        self.stall = float(opts.get("stall", 900))
        # Dwell: tool results are logged as "user" records, so mid-turn the
        # tail flaps assistant/user/assistant… Only a log that has been
        # QUIET for `dwell` seconds with an assistant tail is truly waiting
        # on the human; a fresh assistant tail is just Claude still working.
        self.dwell = float(opts.get("dwell", 15))
        # A fresh "your move" flashes; one you've plainly seen and left goes
        # steady amber after flash_for seconds. Still ranked as attention —
        # it just stops shouting.
        self.flash_for = float(opts.get("flash_for", 300))
        # tmux socket. Sessions live on a named socket; a bare
        # `tmux list-panes` queries the DEFAULT one and finds nothing,
        # so every key fell back to "open a shell in the project dir".
        self.socket = str(opts.get("socket", "/tmp/tmux-1000/cc"))
        # Burn-rate window: bytes appended to a session log are a faithful,
        # already-on-disk proxy for tokens spent. Sampled per poll, rated
        # over this window, and fed into Signal.priority so the hottest
        # sessions rank first on the deck.
        self.burn_window = float(opts.get("burn_window", 600))
        self._samples: dict[str, list[tuple[float, int]]] = {}
        self.snooze_for = float(opts.get("snooze_for", 900))
        self._snooze: dict[str, float] = {}    # session → quiet-until epoch
        # One-shots (disposable runner windows) have no human loop: they must
        # never demand attention and always rank below persistent sessions.
        self.oneshot_sessions = set(opts.get("oneshot_sessions", ["oneshot"]))
        # Same default as grok headless / codex exec: nobody answers a
        # disposable runner, and a truncated `job-<epoch>-<pid>-n` key is
        # how two dark unlabeled tiles ate the pad (G, 2026-09-11). The
        # background-fleet source is the visibility path for those workers.
        self.include_oneshots = bool(opts.get("include_oneshots", False))
        # Titles: the pane table both session sources share, and the writer
        # that pushes the resolved title back into tmux for the manager.
        self.tmux = tmux_for(self.socket)
        # Incremental transcript readers (main + subagent + workflow agent
        # logs): real token usage and the shape of the turn — see activity.py.
        self._tails = Tails(ClaudeTail)
        self.sync = TitleSync(self.tmux, opts.get("sync_every")) \
            if opts.get("sync_titles", True) else None
        self._titled: list[tuple[str, str]] = []

    def _registry(self, root: Path) -> dict[str, dict]:
        """{session uuid: live record} from Claude Code's process registry.

        `root` is `<HOME>/.claude/projects`; the registry is its sibling
        `sessions/`. A record counts only while its process is alive AND is
        the same process (kernel start time = the recorded procStart), so a
        recycled pid can never resurrect a session. Cheap: one small JSON
        per running Claude, re-read only when the file changes."""
        reg_dir = root.parent / "sessions"
        cache = self.__dict__.setdefault("_reg_cache", {})
        out: dict[str, dict] = {}
        try:
            entries = list(reg_dir.glob("*.json"))
        except OSError:
            return out
        for fp in entries:
            try:
                mtime = fp.stat().st_mtime_ns
            except OSError:
                continue
            hit = cache.get(str(fp))
            if hit is None or hit[0] != mtime:   # rewritten on every state change
                try:
                    d = json.loads(fp.read_text())
                except (OSError, ValueError):
                    continue
                hit = cache[str(fp)] = (mtime, _registry_record(d))
            rec = hit[1]
            if rec and _alive(rec["pid"], rec["start"]):
                prev = out.get(rec["session"])
                if prev is None or rec["updated"] > prev["updated"]:
                    out[rec["session"]] = dict(rec)
        if len(cache) > len(entries) + 64:
            present = {str(fp) for fp in entries}
            for k in [k for k in cache if k not in present]:
                cache.pop(k, None)
        return out

    def _registry_target(self, rec: dict) -> str:
        """The registry's pane, if it is a pane of OUR tmux server.

        `%54` is only unique per server: a Claude on another socket would
        otherwise claim whatever cc pane has that id. The record also names
        the session ("mainC:@50.%54"); a pane id only counts when it sits in
        that session here."""
        if not rec.get("pane_id"):
            return ""
        tgt = self.tmux.target_for_id(rec["pane_id"])
        want = rec.get("tmux_session", "")
        if tgt and want and tgt.split(":", 1)[0] != want:
            return ""
        return tgt

    def poll(self) -> list[Signal]:
        signals: list[Signal] = []
        now = time.time()
        self._titled = []
        # A pane belongs to the session its live process says it is running —
        # across ALL accounts, since lanes move between them (B ↔ C). After
        # /clear or a handoff the old log still names the pane through the
        # sticky memo; it must not keep a key (or a stale question) there.
        # Keyed by (account root, uuid): a lane handed from B to C leaves the
        # SAME session id in both accounts' logs, and only the account whose
        # process is running owns the pane.
        self._live: dict[tuple, dict] = {}
        self._claimed: dict[str, tuple] = {}
        # Where the registry exists, a session missing from it has no process:
        # nothing of it can be in flight. Without one (older Claude Code, test
        # fixtures) liveness is unknown and the transcript stands alone.
        self._has_registry = {str(r): (r.parent / "sessions").is_dir()
                              for _, r in self.roots}
        for _, root in self.roots:
            if root.is_dir():
                for uuid, rec in self._registry(root).items():
                    rec["target"] = self._registry_target(rec)
                    self._live[(str(root), uuid)] = rec
                    if rec["target"]:
                        self._claimed[rec["target"]] = (str(root), uuid)
        for acct, root in self.roots:
            if not root.is_dir():
                continue
            signals.extend(self._scan(root, acct, now))
        self._tails.prune(now)
        if self.sync:
            self.sync.push(self._titled)
        return signals

    def _burn_rate(self, session: str, now: float, size: int) -> float:
        """Bytes/sec appended to this session's log over the burn window."""
        samples = self._samples.setdefault(session, [])
        samples.append((now, size))
        while samples and now - samples[0][0] > self.burn_window:
            samples.pop(0)
        if len(samples) < 2:
            return 0.0
        dt = samples[-1][0] - samples[0][0]
        return max(0.0, (samples[-1][1] - samples[0][1]) / max(dt, 1.0))

    def _scan(self, root, acct: str, now: float) -> list[Signal]:
        signals: list[Signal] = []
        registry = getattr(self, "_live", {})
        claimed = getattr(self, "_claimed", {})
        for proj_dir in root.iterdir():
            if not proj_dir.is_dir():
                continue
            for fp in proj_dir.glob("*.jsonl"):
                try:
                    st = fp.stat()
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    continue
                me = (str(root), fp.stem)
                live = registry.get(me)
                # mtime is a safe PRE-filter only: Claude Code re-stamps logs
                # without writing (utimes), which makes files look newer,
                # never older. Freshness itself comes from record times.
                if now - mtime > self.stale and not live:
                    continue
                from ..park import is_parked
                if is_parked(uuid=fp.stem):
                    continue
                tail = self._tails.get(fp)
                subs = self._tails.subagents(fp, now)
                status = (live or {}).get("status", "") if live else (
                    "dead" if self._has_registry.get(str(root)) else "")
                act = assess(tail, subs, status, now)
                if act.phase == "generating" and tail.last_role == "assistant" \
                        and tail.last_stop is None and tail.last_kind != "tool_use" \
                        and now - (tail.last_t or mtime) >= self.dwell:
                    # Old logs carry no stop_reason: a non-tool tail that has
                    # stayed quiet for the dwell is a closed turn.
                    act.phase = "background" if act.agents else "ended"
                    act.burning = act.burning and bool(act.agents_burning)
                last = act.last or mtime        # the last real exchange
                newest = max(last, act.newest)  # …anywhere, agents included
                if not live and now - newest > self.stale:
                    continue                    # touched, not written: history
                lines = _tail_lines(fp)
                exact = (live or {}).get("target") or self._exact_pane(fp.stem)
                if exact and claimed.get(exact, me) != me:
                    exact = ""              # that pane runs another session now
                oneshot = bool(exact) and \
                    exact.split(":", 1)[0] in self.oneshot_sessions
                # The closing message decides ask-vs-done for a closed turn;
                # the activity decides whether the turn is closed at all. The
                # reader holds the whole closing message (the 64 KB tail can be
                # all attachment records after a long turn); the tail is only
                # a fallback for readers that saw no assistant text.
                if tail.last_text:
                    said = ATTENTION if asks_question(tail.last_text) else SUCCESS
                else:
                    said, _final = classify(lines)
                # A question the harness itself is holding open (a dialog,
                # AskUserQuestion — even beside a parallel Bash call) — as
                # opposed to prose that ends in '?'.
                asked = (status == "waiting" or tail.asking_tool()
                         or _ask_tool_pending(lines))
                ph = act.phase
                if ph == "waiting" or asked:
                    state = ATTENTION
                elif ph in ("generating", "tool", "agents"):
                    state = WORKING
                elif ph == "background":
                    state = ATTENTION if said == ATTENTION else WORKING
                elif ph == "ended":
                    state = ATTENTION if said == ATTENTION else SUCCESS
                    if status == "busy" and now - last < self.dwell:
                        state = WORKING     # a Stop hook may continue the turn
                else:
                    state = IDLE
                if state == WORKING and not act.burning and \
                        status not in ("busy", "shell") and now - newest > self.stall:
                    state = IDLE      # Claude's move, nothing for 15 min: stalled
                ended = state in (ATTENTION, SUCCESS)
                snoozed = ended and self._snooze.get(fp.stem, 0) > now
                if snoozed:
                    state = IDLE               # snoozed: quiet, still listed
                askf = signals_dir() / f"ask-{fp.stem[:8]}.json"
                if askf.is_file():
                    try:
                        if last > askf.stat().st_mtime + 5:
                            askf.unlink()   # session moved on: stale ask dies
                        elif ended:
                            state = WORKING  # the hook's key owns this alarm
                    except OSError:
                        pass
                if ended:
                    # Inbox-done (space in the popup): quiet until the session
                    # SPEAKS again — a fresh ask revives the alert on its own.
                    ackf = acked_dir() / fp.stem[:8]
                    try:
                        if ackf.stat().st_mtime >= last:
                            state = IDLE
                        else:
                            ackf.unlink()      # session spoke again: rearm
                    except OSError:
                        pass
                if oneshot and not self.include_oneshots:
                    continue
                if oneshot and state == ATTENTION:
                    state = WORKING            # nobody answers a one-shot
                # Real cwd from the records; munged-name reconstruction only
                # as a fallback for logs that never carried one.
                full = _last_cwd(lines) or _resolve_munged(proj_dir.name)
                burning = state == WORKING and act.burning
                sub = _activity_label(state, act, now, last)
                snip = _snippet(lines)
                if not snip and tail.last_text:
                    from ..decisions import decision_text
                    text = tail.last_text.split("<thinking>")[0].strip()
                    snip = " ".join((decision_text(text) or text).split())[:300]
                if state == ATTENTION and snip:
                    # 96px answers "what do I do?" — the ask beats a rate.
                    # Markdown chrome (**bold** etc.) is noise at this size;
                    # the renderer wraps and pages the text itself.
                    import re as _re
                    sub = _re.sub(r"[*_`#]+", "", snip)[:280]
                flash = None
                if state == ATTENTION and (now - last) > self.flash_for:
                    flash = False
                # Exact pane via the session's own pid — unique even when many
                # sessions share a cwd. The directory-match fallback is only a
                # ROUTING hint: it may be shared by several sessions, so it
                # must never name the key or serve as a dedup identity
                # (it briefly relabeled half the fleet 'tmp').
                # A GUESS MUST NEVER ROUTE. Directory matching sends every
                # session whose cwd is the home directory to whichever pane
                # happens to sit there — that is how every key ended up
                # opening the same one. No exact identity → no pane → the router
                # offers to resume instead of teleporting you somewhere
                # wrong.
                pane = exact or ""
                # One precedence order for both harnesses (titles.py): a name
                # a human chose, else Claude Code's own ai-title for the
                # conversation, else the directory. Five panes of `mainA` all
                # said "mainA" before this.
                info = self.tmux.info(pane) if pane else None
                harness_title = "" if oneshot else claude_ai_title(lines)
                sess = pane.split(":", 1)[0] if pane else ""
                label = resolve_label(harness_title=harness_title, pane=info,
                                      session_name=sess, cwd=full)
                if pane and not oneshot:
                    # Same topic on every surface: key, pane border, window.
                    self._titled.append((pane, label))
                # The press is HUB-OWNED, like a raised question: the hub
                # runs tmux, so it puts the router popup up on the operator's
                # attached terminal(s) itself. Nothing on the deck machine has
                # to be configured or in sync for a press to land (G,
                # 2026-09-08: "standardize that across everything").
                action = None
                if not oneshot:
                    route = contrib_bin("tally-popup-route")
                    if route:
                        action = {"type": "cmd", "argv": [
                            route, pane, fp.stem, full, label[:24], state,
                            acct, f"{self.group}/{acct or 'x'}-{fp.stem[:8]}"]}
                signals.append(Signal(
                    id=f"{self.group}/{acct or 'x'}-{fp.stem[:8]}",
                    action=action,
                    label=label[:24],
                    sublabel=sub,
                    flash=flash,
                    detail=snip,
                    state=state,
                    # Recency is the last real exchange — or, while working,
                    # the newest record anywhere (an agent counts).
                    updated=newest if state == WORKING else last,
                    # Real burn ranks and heats keys: effective tokens/min, and
                    # only while tokens are flowing. A session parked on a long
                    # tool run is working, not burning — no heat, no rate.
                    priority=-10 if oneshot else (
                        int(min(act.rate, 100_000_000)) if burning else 0),
                    group=self.group,
                    meta={"project": full, "session": fp.stem,
                          "account": acct, "exact_pane": bool(exact),
                          "oneshot": oneshot, "live": bool(live),
                          "asked": asked, "phase": ph,
                          "burning": burning, "rate": int(act.rate),
                          "agents": act.agents, "snoozed": snoozed,
                          # Resolved hub-side because only the hub can see
                          # tmux. Without it the deck machine would have to
                          # re-derive the pane over ssh on every press.
                          "tmux": pane},
                ))
        # Dedup: distinct panes are distinct keys; paneless sessions collapse
        # per project to the most recent. Sessions that NEED THE HUMAN are
        # never deduped away — a session's question was shadowed for exactly
        # that reason (half the fleet shares the home directory as cwd, and
        # the noisiest session was the only one shown).
        keep: list[Signal] = []
        best: dict[tuple, Signal] = {}
        for s in signals:
            if s.state in (ATTENTION,):
                keep.append(s)
                continue
            # Exact panes are identities; fallback panes are not — two
            # sessions sharing a guessed pane are still two sessions.
            k = ("pane", s.meta["tmux"]) if s.meta.get("exact_pane") \
                else ("proj", s.meta["project"])
            if k not in best or s.updated > best[k].updated:
                best[k] = s
        return _rollup_by_tmux(keep + list(best.values()))


    # ── tmux resolution ──────────────────────────────────────────────────────

    _PANE_TTL = 15.0   # panes move rarely; a press must not wait on a scan

    def _tmux(self, *args) -> list[str]:
        return ["tmux", "-S", self.socket, *args]

    def _exact_pane(self, session: str) -> str:
        """Sticky exact identity. The env scan can only see a session while
        one of its tool subprocesses is alive, so a session that resolved a
        minute ago would otherwise 'lose' its pane between tool calls.
        Remember what we learn; forget it when the pane is gone.

        Remembered as a pane ID, never as a target: `mainA:1.3` is where a
        pane sits, and closing any pane before it in that window hands the
        name to its neighbour. Every recall re-resolves the id to wherever
        that pane is now.
        """
        memo = getattr(self, "_exact_memo", None)
        if memo is None:
            memo = self._exact_memo = {}
        found = self._session_panes().get(session)
        if found:
            info = self.tmux.info(found)
            memo[session] = info.pane_id if info and info.pane_id else found
            return found
        remembered = memo.get(session)
        if remembered:
            live = self.tmux.target_for_id(remembered) if \
                remembered.startswith("%") else \
                (remembered if remembered in self._all_pane_targets() else "")
            if live:
                return live
        memo.pop(session, None)
        # Durable identity: the session's own hook records session → pane
        # (id included) from inside the pane. Trusted while that pane lives.
        tgt = registry_target(session[:8], self.tmux)
        if tgt:
            info = self.tmux.info(tgt)
            memo[session] = info.pane_id if info and info.pane_id else tgt
            return tgt
        return ""

    def _all_pane_targets(self) -> set:
        """Every live pane target. The cwd-keyed map collapses panes sharing
        a directory (46 panes → 10 entries live), so validating the sticky
        memo against its values randomly demoted live sessions to paneless."""
        now = time.time()
        if now - getattr(self, "_apt_ts", 0.0) < self._PANE_TTL:
            return getattr(self, "_apt_cache", set())
        out: set = set()
        try:
            r = subprocess.run(
                self._tmux("list-panes", "-a", "-F",
                           "#{session_name}:#{window_index}.#{pane_index}"),
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                out = set(r.stdout.split())
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._apt_cache, self._apt_ts = out, now
        return out

    def _session_panes(self) -> dict[str, str]:
        """{session_uuid: pane target} — EXACT identity, via CLAUDE_SESSION_ID
        in each pane's process tree. Directory matching alone collapses every
        home-cwd session onto one pane; this is how each session gets its own
        key with its own name."""
        now = time.time()
        if now - getattr(self, "_sp_ts", 0.0) < self._PANE_TTL:
            return getattr(self, "_sp_cache", {})
        out: dict[str, str] = {}
        try:
            r = subprocess.run(
                self._tmux("list-panes", "-a", "-F",
                           "#{session_name}:#{window_index}.#{pane_index} "
                           "#{pane_pid} #{window_name} #{session_windows}"),
                capture_output=True, text=True, timeout=3)
            ps = subprocess.run(["ps", "-eo", "pid=,ppid="],
                                capture_output=True, text=True, timeout=3)
            kids: dict[int, list[int]] = {}
            for ln in ps.stdout.splitlines():
                parts = ln.split()
                if len(parts) == 2:
                    kids.setdefault(int(parts[1]), []).append(int(parts[0]))
            self._sp_names = getattr(self, "_sp_names", {})
            for ln in (r.stdout or "").strip().splitlines():
                parts = ln.split(" ")
                if len(parts) < 4:
                    continue
                target, pid_s = parts[0], parts[1]
                winname, nwin = " ".join(parts[2:-1]), parts[-1]
                stack = [int(pid_s)] if pid_s.isdigit() else []
                seen = 0
                while stack and seen < 64:
                    pid = stack.pop()
                    seen += 1
                    try:
                        env = Path(f"/proc/{pid}/environ").read_bytes()
                    except OSError:
                        env = b""
                    i = env.find(b"CLAUDE_SESSION_ID=")
                    if i >= 0:
                        sid = env[i + 18:env.find(b"\0", i)].decode(
                            "ascii", "ignore")
                        if sid:
                            out.setdefault(sid, target)
                            self._sp_names[sid] = (winname, nwin)
                    stack.extend(kids.get(pid, []))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        self._sp_cache, self._sp_ts = out, now
        return out

    def _panes(self) -> dict[str, str]:
        """{realpath(cwd): target} for every pane, cached briefly."""
        now = time.time()
        if now - getattr(self, "_pane_ts", 0.0) < self._PANE_TTL:
            return getattr(self, "_pane_cache", {})
        out_map: dict[str, str] = {}
        try:
            r = subprocess.run(
                self._tmux("list-panes", "-a", "-F",
                           "#{session_name}:#{window_index}.#{pane_index} "
                           "#{pane_current_path}"),
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
        panes = self._panes()
        hit = panes.get(key)
        if hit:
            return hit
        # A pane sitting in a parent (or child) of the session's cwd is still
        # that session's window — exact-match alone left most keys paneless.
        best = ""
        best_len = -1
        for cwd, target in panes.items():
            if key.startswith(cwd + "/") or cwd.startswith(key + "/"):
                if len(cwd) > best_len:      # deepest match wins
                    best, best_len = target, len(cwd)
        return best

    def on_press(self, sig: Signal, long: bool = False) -> bool:
        """Long press: snooze. Short press: NOTHING hub-side — the Mac owns
        routing. The old handler here switch-cliented a cwd-GUESSED pane with
        no -c, which tmux resolves to the most-recently-used client: it
        yanked whatever window the operator was in onto an arbitrary session,
        racing the popup on every press (the audit's second view-jump)."""
        if long:
            sid = str(sig.meta.get("session", ""))
            if sid:
                if self._snooze.get(sid, 0) > time.time():
                    self._snooze.pop(sid, None)      # press again to unsnooze
                else:
                    self._snooze[sid] = time.time() + self.snooze_for
                return True
        return False

def _rate(rate: float) -> str:
    """Effective tokens/min, deck-sized: 850/m · 12k/m · 1.4M/m."""
    if rate >= 1e6:
        return f"{rate / 1e6:.1f}M/m"
    if rate >= 1e3:
        return f"{rate / 1e3:.0f}k/m"
    return f"{rate:.0f}/m"


def _activity_label(state: str, act, now: float, last: float = 0.0) -> str:
    """What a non-asking key says under its title. `last` is the last real
    exchange (record time, else the file time for a reader that saw none)."""
    last = act.last or last or now
    if state == SUCCESS:
        return f"done · {_age_str(now - last)}"
    if state != WORKING:
        return _age_str(now - last)
    n = act.agents_burning or act.agents
    crew = f"{n} agent{'s' if n != 1 else ''}" if n else ""
    if act.burning:
        if act.phase == "background":
            return f"{crew} · {_rate(act.rate)}"
        return f"{_rate(act.rate)} · {crew}" if crew else _rate(act.rate)
    # working, not burning: say what it is waiting on, and for how long
    waited = _age_str(now - (act.since or last))
    if act.phase == "tool":
        return f"{act.tool} · {waited}"
    if act.phase in ("agents", "background") and crew:
        return f"{crew} · {waited}"
    return f"stalled · {waited}"


def _registry_record(d: dict) -> dict | None:
    """One registry file → the fields we use, or None if it is not one."""
    try:
        pid = int(d["pid"])
        session = str(d["sessionId"])
    except (KeyError, TypeError, ValueError):
        return None
    if not session or d.get("kind", "interactive") not in ("interactive", "bg"):
        return None
    tm = str(d.get("tmux") or "")          # "mainC:@50.%54"
    pane_id = tm[tm.rfind("%"):] if "%" in tm else ""
    return {"pid": pid, "session": session, "pane_id": pane_id,
            "tmux_session": tm.split(":", 1)[0] if ":" in tm else "",
            "start": str(d.get("procStart") or ""),
            "status": str(d.get("status") or ""),
            "updated": float(d.get("updatedAt") or 0) / 1000.0}


def _alive(pid: int, start: str) -> bool:
    """Is `pid` running, and is it the process that wrote the record?"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    if not start:
        return True
    try:
        return stat.rsplit(")", 1)[1].split()[19] == start
    except IndexError:
        return False


def _age_str(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"
