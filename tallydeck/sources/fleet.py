"""Background fleets → ONE key each, instead of N invisible ones.

The blind spot this fills. Disposable one-shot runners are correctly ranked
last by the session sources — nobody answers a one-shot, so it must never
outrank a session that wants you (claude_sessions pins them to priority -10;
`codex exec` runs are excluded outright). But last on a deck with a hundred
sessions means off the bottom of the physical device: on the night four c64
autopilot workers were burning tokens in the background, the deck showed
nothing at all about them. Ranking them higher is the wrong fix — that is
what the floor is for. Collapsing them is the right one.

So this source ignores transcripts entirely and reads the RUNNERS: every
window of the tmux session cc-oneshot spawns into, whose name it fixes as

    job-<epoch>-<pid>-<label>          e.g. job-1788902390-811372-c64-B-fx-reel

The label a job was launched with (`cc-oneshot.sh -l`) is by convention
`<project>-<slot>-<task>`, so the first dash-separated token names the FLEET
(c64, dc, email…), the next names the worker's slot when it looks like one
(A, B, A2), and the rest is the task. Workers sharing a fleet collapse into a
single key carrying the live count, how long the fleet has been running, and
the task names that fit.

What the key is, and is not. It is VISIBILITY, never an ask: state is working
or idle, it never flashes, and its priority sits above the one-shot floor but
below a session that is genuinely burning (and, by state weight, below
everything that wants the operator). A key that flashes for something nobody
can answer teaches you to ignore the deck, which costs more than the key is
worth.

Pressing it opens the fleet's tmux window — the most recently active worker,
routed through the same hub-side popup every session key uses
(contrib/tally-popup-route → tally-route), with the full roster and the
account burn as the popup's brief so the other workers are one keystroke
away rather than invisible.

Off unless configured. See config/tallydeck.toml.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..signal import Signal, WORKING, IDLE
from ..paths import contrib_bin
from .base import Source
# One age vocabulary across the deck: "14m" here must mean what it means on a
# session key, so borrow the formatter rather than growing a second dialect.
from .claude_sessions import _age_str

# The window name cc-oneshot gives every job. Anything else in the session is
# named by a human and is taken at face value.
JOB = re.compile(r"^job-(\d+)-(\d+)-(.+)$")
# A worker slot inside a fleet: A, B, A2, B2 — a letter and maybe one digit.
SLOT = re.compile(r"^[A-Za-z]\d?$")
# tmux -F field separator: printable (tmux renders control characters as the
# literal four characters "\037"), and not something anyone types.
_SEP = "␟"

DEFAULT_TOKENBURN = Path.home() / ".tokenburn.json"


class Worker:
    """One running background job."""

    __slots__ = ("fleet", "slot", "task", "target", "window", "started",
                 "activity", "pid", "cwd", "account")

    def __init__(self, fleet, slot, task, target, window, started, activity,
                 pid, cwd, account=""):
        self.fleet = fleet
        self.slot = slot
        self.task = task
        self.target = target            # pane target, e.g. oneshot:2.1
        self.window = window            # window name as tmux has it
        self.started = started          # epoch from the window name, or 0
        self.activity = activity        # tmux window_activity
        self.pid = pid
        self.cwd = cwd
        self.account = account          # "A" / "B" / "" — which quota it drains

    @property
    def name(self) -> str:
        return f"{self.slot}·{self.task}" if self.slot else self.task


class BackgroundFleetSource(Source):
    """opts: sessions, socket, min_workers, idle_after, priority, ignore,
    burn, enabled, group (default 'fleet')."""

    group = "fleet"

    def __init__(self, **opts):
        super().__init__(**opts)
        # Present-but-off is the shipped state: the stanza documents itself in
        # the repo config and a single `enabled = true` turns it on.
        self.enabled = bool(opts.get("enabled", True))
        # The tmux sessions that hold disposable runners. Same value
        # claude-sessions calls `oneshot_sessions` — these are the windows it
        # deliberately ranks last.
        self.sessions = set(opts.get("sessions", ["oneshot"]))
        self.socket = str(opts.get("socket", "/tmp/tmux-1000/cc"))
        # One background job is not a fleet; it is a job, and it already has a
        # (low) key of its own. Two is a group worth a slot on the deck.
        self.min_workers = int(opts.get("min_workers", 2))
        # Nothing on any worker's window for this long: the fleet is alive but
        # quiet. Blue would overstate it.
        self.idle_after = float(opts.get("idle_after", 300))
        # Above the one-shot floor (-10), below a session that is actually
        # burning (those carry bytes/sec). State weight already keeps it below
        # anything asking a question.
        self.priority = int(opts.get("priority", 5))
        # Fleet names never worth a key of their own — set per machine.
        self.ignore = {str(x).lower() for x in opts.get("ignore", [])}
        # The word after the fleet name. "background" is what this is, and it
        # is also eleven characters that a 96px key breaks mid-word into
        # "c64 backg / round" — the label that says what the key is must first
        # be readable from a metre away. "fleet" fits on one line at full size.
        self.label_word = str(opts.get("label_word", "fleet"))
        self.burn = bool(opts.get("burn", True))
        self.burn_config = Path(opts.get("burn_config", DEFAULT_TOKENBURN)).expanduser()
        self._burn_src = None
        self._pane_ts = 0.0
        self._panes: list[dict] = []
        self._acct_ts = 0.0
        self._acct_homes: dict[str, str] = {}

    # ── polling ──────────────────────────────────────────────────────────────

    def poll(self) -> list[Signal]:
        if not self.enabled:
            return []
        now = time.time()
        fleets: dict[str, list[Worker]] = {}
        for w in self._workers(now):
            if w.fleet in self.ignore:
                continue
            fleets.setdefault(w.fleet, []).append(w)
        out: list[Signal] = []
        for fleet, workers in sorted(fleets.items()):
            if len(workers) < self.min_workers:
                continue
            out.append(self._signal(fleet, workers, now))
        return out

    def _signal(self, fleet: str, workers: list[Worker], now: float) -> Signal:
        # Most recently active worker first: that is the one a press opens,
        # and the one whose age the key reports.
        workers.sort(key=lambda w: (-w.activity, w.started))
        newest = workers[0]
        oldest = min((w.started for w in workers if w.started), default=0.0)
        state = WORKING if (now - newest.activity) <= self.idle_after else IDLE
        n = len(workers)
        sub = f"{n} jobs"
        if oldest:
            sub += f" · {_age_str(now - oldest)}"
        tasks = self._pack([w.task for w in workers], 46 - len(sub))
        if tasks:
            sub += " · " + tasks
        accounts = [a for a in dict.fromkeys(w.account for w in workers) if a]
        detail = self._detail(fleet, workers, accounts, now)
        sid = f"{self.group}/{re.sub(r'[^a-z0-9-]+', '-', fleet)}"
        label = f"{fleet} {self.label_word}".strip()[:24]
        action = None
        route = contrib_bin("tally-popup-route")
        if route:
            # The same hub-side routing every session key uses: a popup on the
            # attached terminal(s) whose Enter goes to that pane. The trailing
            # argument is the brief the popup shows — here, the roster and the
            # burn, so the workers this key stands for are readable without
            # opening any of them.
            action = {"type": "cmd", "argv": [
                route, newest.target, "", newest.cwd, label, state,
                accounts[0] if accounts else "", sid, detail]}
        return Signal(
            id=sid,
            action=action,
            label=label,
            sublabel=sub,
            # Never: this is a status light, not a request. A key that flashes
            # for something nobody can answer trains you to ignore the deck.
            flash=False,
            detail=detail,
            state=state,
            updated=newest.activity or now,
            priority=self.priority,
            group=self.group,
            # NOT meta.oneshot: that flag renders a card muted, which is right
            # for one disposable worker among a hundred keys and wrong for the
            # one key standing in for the whole fleet. `background` says the
            # same thing without dimming the only thing there is to see.
            meta={"fleet": fleet, "workers": n, "background": True,
                  "accounts": accounts, "project": newest.cwd,
                  # Resolved hub-side, like every other key: only the hub can
                  # see tmux.
                  "tmux": newest.target,
                  "targets": [w.target for w in workers]},
        )

    @staticmethod
    def _pack(tasks: list[str], budget: int) -> str:
        """As many task names as fit, shortened, in the order given.

        Deduped: two c64 workers on different `fx-*` tasks would otherwise
        spend the key's second line saying "fx, fx". The count is stated
        separately, so this line is about WHAT, not how many.
        """
        shorts = list(dict.fromkeys(t.split("-")[0][:10] or t[:10]
                                    for t in tasks))
        out: list[str] = []
        for short in shorts:
            if len(", ".join(out + [short])) > max(0, budget):
                break
            out.append(short)
        if out and len(out) < len(shorts):
            out.append("…")
        return ", ".join(out)

    def _detail(self, fleet: str, workers: list[Worker], accounts: list[str],
                now: float) -> str:
        lines = [f"{len(workers)} background workers in tmux session "
                 f"`{workers[0].target.split(':', 1)[0]}` on {self.socket}", ""]
        for i, w in enumerate(workers):
            age = _age_str(now - w.started) if w.started else "?"
            quiet = _age_str(now - w.activity) if w.activity else "?"
            mark = "→" if i == 0 else " "
            acct = f" [{w.account}]" if w.account else ""
            lines.append(f"{mark} {w.target:<12} {w.name[:44]:<46} "
                         f"{age} old · {quiet} quiet{acct}")
        lines += ["", f"Enter opens {workers[0].target} — the most recently "
                      f"active of the {len(workers)}."]
        if len(workers) > 1:
            others = ", ".join(w.target for w in workers[1:])
            lines.append(f"The others are {others}; "
                         f"`tmux -S {self.socket} attach -t "
                         f"{workers[0].target.split(':', 1)[0]}` shows them all.")
        lines.append("These are disposable one-shot runners: the window dies "
                     "when the job does, and nothing here is waiting on you.")
        burn = self._burn_lines(accounts)
        if burn:
            lines += ["", "TOKEN BURN · 5h window, from the same meter as the "
                          "deck's bar"] + burn
        return "\n".join(lines)

    # ── token burn (reuses the existing meter, never a second one) ───────────

    def _burn_lines(self, accounts: list[str]) -> list[str]:
        if not self.burn:
            return []
        if self._burn_src is None:
            from .burn import TokenBurnSource
            self._burn_src = TokenBurnSource(config=self.burn_config)
        try:
            sigs = self._burn_src.poll()
        except Exception:
            return []                      # the meter is optional, always
        if not sigs:
            return []
        lanes = (sigs[0].meta or {}).get("lanes") or []
        # The fleet's own accounts if we could resolve them, else every lane —
        # "which quota is this draining" is the question either way.
        mine = [l for l in lanes if l.get("id") in accounts] or lanes
        out = []
        for l in mine:
            clock = f" · resets in {l['clock']}" if l.get("clock") else ""
            out.append(f"  {l.get('id')}  {round(l.get('pct', 0))}% of window "
                       f"· target {round(l.get('target', 0) * 100)}%{clock}")
        return out

    # ── tmux ─────────────────────────────────────────────────────────────────

    _TTL = 5.0

    def _tmux(self, *args, timeout: float = 3) -> str:
        try:
            r = subprocess.run(["tmux", "-S", self.socket, *args],
                               capture_output=True, text=True, timeout=timeout)
            return r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""                      # fail open: no tmux, no fleet key

    def _runner_panes(self) -> list[dict]:
        """The active pane of every window in the configured sessions."""
        now = time.time()
        if now - self._pane_ts < self._TTL:
            return self._panes
        fmt = _SEP.join((
            "#{session_name}:#{window_index}.#{pane_index}", "#{session_name}",
            "#{window_name}", "#{window_activity}", "#{pane_pid}",
            "#{pane_current_path}", "#{pane_active}"))
        panes = []
        for ln in self._tmux("list-panes", "-a", "-F", fmt).splitlines():
            f = ln.split(_SEP)
            if len(f) < 7 or f[1] not in self.sessions or f[6] != "1":
                continue
            panes.append({"target": f[0], "window": f[2],
                          "activity": float(f[3] or 0), "pid": f[4],
                          "cwd": f[5]})
        self._panes, self._pane_ts = panes, now
        return panes

    def _workers(self, now: float) -> list[Worker]:
        workers = []
        for p in self._runner_panes():
            m = JOB.match(p["window"])
            started, label = (float(m.group(1)), m.group(3)) if m \
                else (0.0, p["window"])
            parts = label.split("-")
            fleet = parts[0].lower()
            rest = parts[1:]
            slot = ""
            if rest and SLOT.match(rest[0]) and len(rest) > 1:
                slot, rest = rest[0], rest[1:]
            task = "-".join(rest) or label
            if not fleet:
                continue
            workers.append(Worker(
                fleet=fleet, slot=slot, task=task, target=p["target"],
                window=p["window"], started=started,
                activity=p["activity"] or now, pid=p["pid"], cwd=p["cwd"],
                account=self._account(p["pid"])))
        return workers

    # ── which quota a worker is draining ─────────────────────────────────────

    def _accounts(self) -> dict[str, str]:
        """{realpath(claude HOME): account id}, from tokenburn's own config.

        The account a job runs under is its HOME (account B lives in its own),
        and the mapping from HOME to account id already exists in
        ~/.tokenburn.json — the file the burn meter reads. Re-deriving it from
        the job's label would be a guess about a naming convention; this is the
        machine's own answer.
        """
        now = time.time()
        if now - self._acct_ts < 60 and self._acct_homes:
            return self._acct_homes
        self._acct_ts = now
        try:
            cfg = json.loads(self.burn_config.read_text())
        except (OSError, ValueError):
            return self._acct_homes
        homes = {}
        for a in cfg.get("accounts", []):
            home = a.get("claude_home")
            if home and a.get("id"):
                homes[os.path.realpath(str(home))] = str(a["id"])
        if homes:
            self._acct_homes = homes
        return self._acct_homes

    def _account(self, pane_pid: str) -> str:
        """Walk the pane's process tree to the claude process and read its HOME.

        The pane's own shell inherits the deck user's HOME even when the job
        does not: cc-oneshot puts the account HOME on the `claude` process, a
        couple of levels down (sh → timeout → claude).
        """
        homes = self._accounts()
        if not homes or not pane_pid.isdigit():
            return ""
        stack, seen = [int(pane_pid)], 0
        kids = self._children()
        while stack and seen < 32:
            pid = stack.pop()
            seen += 1
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                continue
            if comm.startswith("claude") or comm.startswith("codex"):
                try:
                    env = Path(f"/proc/{pid}/environ").read_bytes()
                except OSError:
                    env = b""
                i = env.find(b"HOME=")
                if i >= 0:
                    home = env[i + 5:env.find(b"\0", i)].decode("utf-8", "ignore")
                    hit = homes.get(os.path.realpath(home))
                    if hit:
                        return hit
            stack.extend(kids.get(pid, []))
        return ""

    def _children(self) -> dict[int, list[int]]:
        now = time.time()
        if now - getattr(self, "_kids_ts", 0.0) < self._TTL:
            return getattr(self, "_kids", {})
        kids: dict[int, list[int]] = {}
        try:
            ps = subprocess.run(["ps", "-eo", "pid=,ppid="],
                                capture_output=True, text=True, timeout=3)
            for ln in ps.stdout.splitlines():
                parts = ln.split()
                if len(parts) == 2:
                    kids.setdefault(int(parts[1]), []).append(int(parts[0]))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        self._kids, self._kids_ts = kids, now
        return kids
