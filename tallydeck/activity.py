"""Activity: is a session burning tokens right now, and since when?

The deck used to call the bytes appended to a session log its "burn". That
is three kinds of wrong at once:

  * bytes are not tokens — tool output, hook summaries and bookkeeping
    records grow the log; a 360k-token cache read grows it by 4 KB;
  * subagents burn in their OWN transcripts (<session>/subagents/
    agent-*.jsonl), so a session whose background agents were working
    flat out read "done";
  * Claude Code re-stamps log mtimes without writing anything (utimes,
    millisecond precision), so a conversation that ended an hour ago read
    "done · 4m" and passed every freshness test.

This module reads what the transcripts actually say:

  usage    tokens per API message, deduplicated by message id (a message is
           logged once per content block and every record repeats the full
           usage), across the main transcript AND every subagent's;
  shape    whose move it is, which tool calls are still open, when the last
           real exchange happened (record timestamps — never mtime).

and turns it into one `Activity` per session:

  phase    generating · tool · agents · background · waiting · ended · idle
  burning  tokens are flowing NOW: a model call is in flight, or usage
           landed in the last ACTIVE seconds (main thread or any subagent)
  rate     effective tokens/min over RATE_WINDOW. "Effective" = input-token
           equivalents at API price ratios (cache read 0.1, cache write
           1.25, output 5), so cached context does not dwarf real work and
           the numbers compare across sessions and harnesses
  last     time of the last conversational record (the age the key shows)

Readers are incremental: the first read backfills the tail of a file, after
that each poll reads only what was appended.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Tunables (seconds), measured on this box 2026-10-14 over 4,917 model calls
# (main p99 66 s / p99.9 253 s; agents p99 114 s / p99.9 368 s):
# ACTIVE      usage this recent means tokens are flowing — one tool step of
#             a tight loop, not a long tool run;
# GEN_CAP     a call "in flight" longer than this is a stall (p99.9), and an
#             agent whose last record handed it the turn this long ago was
#             aborted, not thinking;
# TOOL_STALL  an agent inside an open tool call this long has stopped;
# RATE_WINDOW the burn rate is averaged over this; KEEP: usage history kept.
ACTIVE = 60.0
GEN_CAP = 300.0
TOOL_STALL = 900.0
RATE_WINDOW = 300.0
KEEP = 900.0
BACKFILL = 2_000_000
AGENT_TOOLS = ("Agent", "Task")
# stop_reasons that close a turn (surveyed over 13k live records: every
# assistant record carries one; stop_sequence is always followed by a prompt).
TURN_ENDS = ("end_turn", "stop_sequence", "refusal")
WEIGHTS = {"input": 1.0, "cache_write": 1.25, "cache_read": 0.1, "output": 5.0}


def effective(input_=0, cache_write=0, cache_read=0, output=0) -> float:
    w = WEIGHTS
    return (input_ * w["input"] + cache_write * w["cache_write"]
            + cache_read * w["cache_read"] + output * w["output"])


def _epoch(ts) -> float | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class _Tail:
    """Incremental JSONL reader. Subclasses implement `_record`."""

    def __init__(self, path: Path, backfill: int = BACKFILL):
        self.path = Path(path)
        self.off = -1
        self.partial = b""
        self.backfill = backfill
        self.usage: dict[str, tuple[float, float]] = {}   # key → (t, effective)
        self.pending: dict[str, tuple[str, float]] = {}   # call id → (tool, t)
        self.last_t = 0.0          # last conversational record
        self.last_role = ""        # "user" | "assistant"
        self.last_kind = ""        # prompt · tool_result · text · thinking · tool_use
        self.ended = False         # the last turn closed (end_turn / task_complete)
        self.last_stop = ""        # stop_reason of the last assistant record (None: old log)
        self.touched = 0.0         # when this reader was last refreshed
        self._mtime = 0.0          # time for records that carry none (old logs)

    def refresh(self) -> None:
        self.touched = time.time()
        try:
            st = self.path.stat()
        except OSError:
            return
        size, self._mtime = st.st_size, st.st_mtime
        first = self.off < 0
        if first or size < self.off:            # new reader, or truncated
            self.off = max(0, size - self.backfill)
            self.partial = b""
            skip_first = self.off > 0
        else:
            skip_first = False
        if size == self.off:
            return
        try:
            with self.path.open("rb") as f:
                f.seek(self.off)
                data = self.partial + f.read(size - self.off)
        except OSError:
            return
        self.off = size
        lines = data.split(b"\n")
        self.partial = lines.pop()               # an unterminated line waits
        if skip_first and lines:
            lines = lines[1:]                    # backfill began mid-line
        for ln in lines:
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except (ValueError, UnicodeError):
                continue
            if isinstance(rec, dict):
                self._record(rec)
        cut = time.time() - KEEP
        if len(self.usage) > 64:
            self.usage = {k: v for k, v in self.usage.items() if v[0] >= cut}

    def _record(self, rec: dict) -> None:
        raise NotImplementedError

    def tokens(self, window: float, now: float) -> float:
        return sum(e for t, e in self.usage.values() if now - t <= window)

    def used_within(self, seconds: float, now: float) -> bool:
        return any(now - t <= seconds for t, _ in self.usage.values())

    def in_flight(self, now: float) -> bool:
        """The model owes this thread an answer: the last record handed it
        the turn (a prompt or a tool result) and nothing came back yet."""
        return (self.last_role == "user" and not self.pending
                and 0 <= now - self.last_t < GEN_CAP)


class ClaudeTail(_Tail):
    """A Claude Code transcript (main session or subagent)."""

    def _record(self, rec: dict) -> None:
        typ = rec.get("type")
        if typ not in ("user", "assistant"):
            return
        t = _epoch(rec.get("timestamp")) or self._mtime
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text"}] if isinstance(content, str) else [])
        if typ == "assistant":
            u = msg.get("usage")
            mid = msg.get("id") or rec.get("requestId")
            if isinstance(u, dict) and mid:
                self.usage[str(mid)] = (t, effective(
                    int(u.get("input_tokens") or 0),
                    int(u.get("cache_creation_input_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0),
                    int(u.get("output_tokens") or 0)))
            kind = ""
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                kind = b.get("type") or kind
                if b.get("type") == "tool_use" and b.get("id"):
                    self.pending[str(b["id"])] = (str(b.get("name") or "tool"), t)
            self.last_role, self.last_kind, self.last_t = "assistant", kind, t
            stop = msg.get("stop_reason")
            self.last_stop = stop
            # No stop_reason (older logs): undecided here — the source applies
            # its dwell (a quiet non-tool tail is a closed turn).
            self.ended = kind != "tool_use" and stop in TURN_ENDS
            if self.ended:
                self.pending.clear()
            return
        # user: tool results close calls; anything else is a prompt
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        for b in results:
            self.pending.pop(str(b.get("tool_use_id") or ""), None)
        if results:
            self.last_kind = "tool_result"
        elif rec.get("isMeta"):
            return              # caveats, local-command echoes: not a turn
        else:
            self.pending.clear()          # a new prompt supersedes open calls
            self.last_kind = "prompt"
        self.last_role, self.last_t, self.ended = "user", t, False


class CodexTail(_Tail):
    """A Codex rollout. Usage comes from token_count events: each one that
    advances the cumulative total is one model call."""

    def __init__(self, path: Path, backfill: int = BACKFILL):
        super().__init__(path, backfill)
        self._total = -1

    def _record(self, rec: dict) -> None:
        t = _epoch(rec.get("timestamp")) or self._mtime
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        kind = p.get("type")
        typ = rec.get("type")
        if typ == "event_msg" and kind == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            tot = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
            last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
            total = int(tot.get("total_tokens") or 0)
            if total and total != self._total and last:
                self._total = total
                inp = int(last.get("input_tokens") or 0)
                cached = int(last.get("cached_input_tokens") or 0)
                cw = int(last.get("cache_write_input_tokens") or 0)
                self.usage[f"{t}:{total}"] = (t, effective(
                    max(0, inp - cached - cw), cw, cached,
                    int(last.get("output_tokens") or 0)))
            return
        if typ == "event_msg" and kind in ("task_complete", "turn_aborted"):
            self.ended, self.pending = True, {}
            self.last_t = max(self.last_t, t)
            return
        if typ == "event_msg" and kind in ("task_started", "user_message"):
            self.ended = False
            if kind == "user_message":
                self.pending.clear()
                self.last_role, self.last_kind, self.last_t = "user", "prompt", t
            return
        if typ != "response_item":
            return
        if kind in ("function_call", "custom_tool_call", "local_shell_call"):
            cid = p.get("call_id") or p.get("id")
            if cid:
                self.pending[str(cid)] = (str(p.get("name") or "shell"), t)
            self.last_role, self.last_kind, self.last_t = "assistant", "tool_use", t
            self.ended = False
        elif kind in ("function_call_output", "custom_tool_call_output",
                      "local_shell_call_output"):
            self.pending.pop(str(p.get("call_id") or ""), None)
            self.last_role, self.last_kind, self.last_t = "user", "tool_result", t
        elif kind in ("message", "reasoning"):
            role = "assistant" if p.get("role", "assistant") == "assistant" else "user"
            if role == "user":
                return          # injected context (AGENTS.md, env): not a turn
            self.last_role, self.last_kind, self.last_t = "assistant", (
                "text" if kind == "message" else "thinking"), t


@dataclass
class Activity:
    phase: str = "idle"      # generating · tool · agents · background · waiting · ended · idle
    burning: bool = False
    rate: float = 0.0        # effective tokens/min over RATE_WINDOW
    last: float = 0.0        # last conversational record, main thread
    newest: float = 0.0      # newest record anywhere (main or a subagent)
    tool: str = ""           # the open call, for phase "tool"
    since: float = 0.0       # when the current phase began
    agents: int = 0          # subagents still running
    agents_burning: int = 0  # …of which tokens are flowing now


def assess(main: _Tail, subs: list[_Tail], status: str = "",
           now: float | None = None) -> Activity:
    """One session's activity from its transcripts.

    The TRANSCRIPT decides whether a turn is open: a prompt or tool result
    with no answer yet (generating), an open tool call (tool / agents), or a
    closed turn (ended). `status` is what the live process published, and it
    may only add what the transcript cannot know:

      waiting   a dialog is holding for the human
      idle      the process is between turns — an "open" transcript turn
                was interrupted or killed, so nothing is in flight
      dead      no live process: nothing can be in flight either
      busy/shell/''  no override. Claude Code keeps reporting `busy` while
                background agents or shells run after the turn ended — the
                stale-busy that kept a finished GX10 session blue — so busy
                is never allowed to reopen a closed turn.
    """
    now = time.time() if now is None else now
    dead = status == "dead"
    a = Activity(last=main.last_t)
    a.newest = max([main.last_t] + [s.last_t for s in subs])
    a.rate = (main.tokens(RATE_WINDOW, now)
              + sum(s.tokens(RATE_WINDOW, now) for s in subs)) * 60.0 / RATE_WINDOW
    for s in subs:
        hot = s.used_within(ACTIVE, now) or (not dead and s.in_flight(now))
        quiet = now - s.last_t
        running = hot or (not dead and not s.ended and 0 <= quiet < (
            TOOL_STALL if s.pending else GEN_CAP))
        a.agents += int(running)
        a.agents_burning += int(hot)
    if main.pending:
        names = [n for n, _ in main.pending.values()]
        newest = max(main.pending.values(), key=lambda v: v[1])
        if any(n in AGENT_TOOLS for n in names):
            a.phase, a.since = "agents", newest[1]
        else:
            a.phase, a.tool, a.since = "tool", newest[0], newest[1]
    elif main.last_role == "user":
        a.phase, a.since = "generating", main.last_t
    elif main.last_role == "assistant" and not main.ended:
        a.phase, a.since = "generating", main.last_t   # more blocks on the way
    elif main.ended:
        a.phase, a.since = "ended", main.last_t
    if status == "waiting":
        a.phase = "waiting"
    elif status in ("idle", "dead") and a.phase in ("generating", "tool", "agents"):
        a.phase = "ended" if main.last_role == "assistant" else "idle"
    elif status == "shell" and a.phase == "generating":
        a.phase, a.tool = "tool", "shell"
    if a.phase in ("ended", "idle") and a.agents:
        # The conversation turn is over but agents it launched are still
        # going (background Agent calls, a Workflow): working, not done.
        a.phase, a.since = "background", main.last_t
    in_flight = a.phase == "generating" and 0 <= now - (a.since or now) < GEN_CAP
    a.burning = bool(in_flight or main.used_within(ACTIVE, now) or a.agents_burning)
    return a


def _agent_files(d: str) -> list[str]:
    try:
        return [e.path for e in os.scandir(d)
                if e.name.startswith("agent-") and e.name.endswith(".jsonl")]
    except OSError:
        return []


class Tails:
    """Readers keyed by path, shared across polls; idle ones are dropped."""

    def __init__(self, factory=ClaudeTail, ttl: float = 1800.0):
        self.factory = factory
        self.ttl = ttl
        self._readers: dict[str, _Tail] = {}
        self._sub_ls: dict[str, tuple[float, list[str]]] = {}

    def get(self, path) -> _Tail:
        key = str(path)
        r = self._readers.get(key)
        if r is None:
            r = self._readers[key] = self.factory(Path(key))
        r.refresh()
        return r

    def subagents(self, main_path, now: float, listing_ttl: float = 10.0) -> list[_Tail]:
        """Readers for a Claude session's agent transcripts that could be
        live: written to in the last KEEP seconds, or already tracked.

        Two places: Agent/Task subagents in <session>/subagents/, and
        Workflow agents one level down in <session>/subagents/workflows/
        <run>/ — a session driving a workflow burns there and nowhere else."""
        base = str(main_path)[:-6] if str(main_path).endswith(".jsonl") else str(main_path)
        d = os.path.join(base, "subagents")
        cached = self._sub_ls.get(d)
        if cached is None or now - cached[0] > listing_ttl:
            names = _agent_files(d)
            try:
                for run in os.scandir(os.path.join(d, "workflows")):
                    if run.is_dir():
                        names += _agent_files(run.path)
            except OSError:
                pass
            cached = self._sub_ls[d] = (now, names)
        out = []
        for p in cached[1]:
            if p not in self._readers:
                try:
                    if now - os.path.getmtime(p) > KEEP:
                        continue                 # long finished: not worth a read
                except OSError:
                    continue
            out.append(self.get(p))
        return out

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for k in [k for k, r in self._readers.items() if now - r.touched > self.ttl]:
            self._readers.pop(k, None)
        if len(self._sub_ls) > 512:
            self._sub_ls.clear()


def _agent_label(agent_path: str) -> str:
    """A subagent's own task label, from its meta.json (description, type)."""
    try:
        d = json.loads(Path(agent_path[:-6] + ".meta.json").read_text())
    except (OSError, ValueError):
        return ""
    desc = str(d.get("description") or "").strip()
    kind = str(d.get("agentType") or "").strip()
    return desc + (f" ({kind})" if kind and kind != "general-purpose" else "")


def _fmt_age(s: float) -> str:
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def summary(path, kind: str = "claude", now: float | None = None) -> str:
    """Plain-language 'what is it doing right now' for the brief popup.

    The popup used to show only the last message — a session whose turn was
    over while its background agent kept burning read "nothing needed" on a
    blue key, with no hint why it was blue."""
    now = time.time() if now is None else now
    pool = Tails(CodexTail if kind == "codex" else ClaudeTail)
    try:
        main = pool.get(path)
    except OSError:
        return ""
    subs = pool.subagents(path, now) if kind == "claude" else []
    a = assess(main, subs, "", now)
    lines = []
    if a.phase == "generating":
        lines.append(f"Conversation: the model is answering ({_fmt_age(now - a.since)}).")
    elif a.phase == "tool":
        lines.append(f"Conversation: waiting on {a.tool} for {_fmt_age(now - a.since)}.")
    elif a.phase == "agents":
        lines.append(f"Conversation: waiting on its agents ({_fmt_age(now - a.since)}).")
    elif a.phase in ("ended", "background"):
        lines.append(f"Conversation: turn ended {_fmt_age(now - a.last)} ago.")
    running = []
    for s in subs:
        hot = s.used_within(ACTIVE, now) or s.in_flight(now)
        alive = hot or (not s.ended and 0 <= now - s.last_t < (
            TOOL_STALL if s.pending else GEN_CAP))
        if alive:
            label = _agent_label(str(s.path)) or Path(s.path).stem
            running.append(f"- {label} · {'burning' if hot else 'running a tool'}")
    if running:
        lines.append(f"Agents: {len(running)} still running")
        lines.extend(running[:8])
    if a.burning:
        lines.append(f"Burn: {a.rate / 1000:.0f}k effective tokens/min over the last "
                     f"{int(RATE_WINDOW // 60)} min, agents included.")
    elif a.phase in ("tool", "generating", "agents"):
        lines.append("Burn: no tokens flowing right now.")
    return "\n".join(lines)
