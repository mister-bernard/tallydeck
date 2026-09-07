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


def _assistant_wants_input(lines: list[str]) -> bool:
    """An assistant tail only means "your move" if the turn actually ENDED.

    A tail whose last assistant record carries tool_use (or stop_reason
    "tool_use") is a session waiting on a TOOL — e.g. a long test run — and
    flagging it flashed autonomous workers as needing the human (the operator hit this:
    'running the fork suite' shown as attention)."""
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        content = msg.get("content", [])
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict) and last.get("type") == "tool_use":
                # Most tool_use tails mean "a tool is running" — but some
                # tools ARE the question: they block on the human by design.
                return last.get("name") in ("AskUserQuestion", "ExitPlanMode")
        if msg.get("stop_reason") == "tool_use":
            return False
        return True
    return True


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
                    return " ".join(text.split())[:limit]
    return ""


def _last_cwd(lines: list[str]) -> str:
    """The session's own records carry its real cwd — authoritative, unlike
    the munged directory name, which is lossy: '-a-b-c' cannot distinguish
    'a/b/c' from 'a/b-c', and pressing a key for my-web-app once
    ssh'd into the nonexistent my/web/app."""
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
        self.root = self.roots[0][1]      # kept: existing callers/tests read it
        self.stale = float(opts.get("stale", 1800))
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

    def poll(self) -> list[Signal]:
        signals: list[Signal] = []
        now = time.time()
        for acct, root in self.roots:
            if not root.is_dir():
                continue
            signals.extend(self._scan(root, acct, now))
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
        for proj_dir in root.iterdir():
            if not proj_dir.is_dir():
                continue
            for fp in proj_dir.glob("*.jsonl"):
                try:
                    st = fp.stat()
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    continue
                if now - mtime > self.stale:
                    continue
                lines = _tail_lines(fp)
                last = _last_record_type(lines)
                exact = self._exact_pane(fp.stem)
                oneshot = bool(exact) and \
                    exact.split(":", 1)[0] in self.oneshot_sessions
                state = {"user": WORKING, "assistant": ATTENTION}.get(last, IDLE)
                if state == ATTENTION and ((now - mtime) < self.dwell
                                           or not _assistant_wants_input(lines)):
                    state = WORKING
                if state == ATTENTION and self._snooze.get(fp.stem, 0) > now:
                    state = IDLE               # snoozed: quiet, still listed
                askf = (Path.home() / ".tallydeck" / "signals" /
                        f"ask-{fp.stem[:8]}.json")
                if askf.is_file():
                    try:
                        if mtime > askf.stat().st_mtime + 5:
                            askf.unlink()   # session moved on: stale ask dies
                        elif state == ATTENTION:
                            state = WORKING  # the hook's key owns this alarm
                    except OSError:
                        pass
                if state == ATTENTION:
                    # Inbox-done (space in the popup): quiet until the log
                    # MOVES again — a fresh ask revives the alert on its own.
                    ackf = Path.home() / ".tallydeck" / "acked" / fp.stem[:8]
                    try:
                        if ackf.stat().st_mtime >= mtime:
                            state = IDLE
                        else:
                            ackf.unlink()      # session spoke again: rearm
                    except OSError:
                        pass
                if oneshot and state == ATTENTION:
                    state = WORKING            # nobody answers a one-shot
                # Real cwd from the records; munged-name reconstruction only
                # as a fallback for logs that never carried one.
                full = _last_cwd(lines) or _resolve_munged(proj_dir.name)
                rate = self._burn_rate(fp.stem, now, size)   # bytes/sec
                sub = _age_str(now - mtime)
                if rate >= 20:
                    sub += f" · {rate * 60 / 1024:.0f}k/m"
                snip = _snippet(lines)
                if state == ATTENTION and snip:
                    # 96px answers "what do I do?" — the ask beats a rate.
                    # Markdown chrome (**bold** etc.) is noise at this size;
                    # the renderer wraps and pages the text itself.
                    import re as _re
                    sub = _re.sub(r"[*_`#]+", "", snip)[:280]
                flash = None
                if state == ATTENTION and (now - mtime) > self.flash_for:
                    flash = False
                # Exact pane via the session's own pid — unique even when many
                # sessions share a cwd. The directory-match fallback is only a
                # ROUTING hint: it may be shared by several sessions, so it
                # must never name the key or serve as a dedup identity
                # (it briefly relabeled half the fleet 'tmp').
                # A GUESS MUST NEVER ROUTE. Directory matching sends every
                # session whose cwd is /home/me to whichever pane
                # happens to sit there — that is how every key ended up
                # opening `zephyr`. No exact identity → no pane → the router
                # offers to resume instead of teleporting you somewhere
                # wrong.
                pane = exact or ""
                win_label = ""
                if exact:
                    wn, nw = getattr(self, "_sp_names", {}).get(
                        fp.stem, ("", "1"))
                    generic = ("bash", "zsh", "sh", "fish", "claude",
                               "claude-b", "node", "python3", "ssh",
                               "mosh", "")
                    if str(nw) not in ("", "1") and wn not in generic:
                        win_label = wn
                label = win_label or (exact.split(":", 1)[0] if exact
                                      else (os.path.basename(full) or full))
                signals.append(Signal(
                    id=f"{self.group}/{acct or 'x'}-{fp.stem[:8]}",
                    label=label[:24],
                    sublabel=sub,
                    flash=flash,
                    detail=snip,
                    state=state,
                    updated=mtime,
                    # Hotter sessions outrank within the same state, so the
                    # busiest work funnels toward the top of the deck.
                    priority=-10 if oneshot else int(min(rate, 1_000_000)),
                    group=self.group,
                    meta={"project": full, "session": fp.stem,
                          "account": acct, "exact_pane": bool(exact),
                          "oneshot": oneshot,
                          # Resolved hub-side because only the hub can see
                          # tmux. Without it the deck machine would have to
                          # re-derive the pane over ssh on every press.
                          "tmux": pane},
                ))
        # Dedup: distinct panes are distinct keys; paneless sessions collapse
        # per project to the most recent. Sessions that NEED THE HUMAN are
        # never deduped away — a batch job's question was shadowed for exactly
        # that reason (half the fleet shares cwd /home/me, and the
        # noisiest session was the only one shown).
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
        return keep + list(best.values())


    # ── tmux resolution ──────────────────────────────────────────────────────

    _PANE_TTL = 15.0   # panes move rarely; a press must not wait on a scan

    def _tmux(self, *args) -> list[str]:
        return ["tmux", "-S", self.socket, *args]

    def _exact_pane(self, session: str) -> str:
        """Sticky exact identity. The env scan can only see a session while
        one of its tool subprocesses is alive, so a session that resolved a
        minute ago would otherwise 'lose' its pane between tool calls.
        Remember what we learn; forget it when the pane is gone."""
        memo = getattr(self, "_exact_memo", None)
        if memo is None:
            memo = self._exact_memo = {}
        found = self._session_panes().get(session)
        if found:
            memo[session] = found
            return found
        remembered = memo.get(session)
        if remembered and remembered in self._all_pane_targets():
            return remembered
        memo.pop(session, None)
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
        /home cwd session onto one pane; this is how 'batch job' gets its own
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

def _age_str(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"
