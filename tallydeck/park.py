"""Park a live agent session: save resume info, free RAM, drop the deck key.

Close is not delete. The transcript stays on disk; `tally park` records the
exact resume command, SIGTERMs the agent, kills the tmux session, and writes
a parked marker so session sources stop emitting a key. Hourly `tally reap`
does the same for spawned sessions idle ≥3h — this is the on-demand version
for a key the operator is looking at.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .paths import state_dir, write_json_atomic


SOCKET = os.environ.get("TALLY_TMUX_SOCKET", "/tmp/tmux-1000/cc")


def parked_dir() -> Path:
    return state_dir() / "parked"


def is_parked(uuid: str = "", slug: str = "") -> bool:
    d = parked_dir()
    if uuid and (d / f"{uuid}.json").is_file():
        return True
    if slug and (d / f"{slug}.json").is_file():
        return True
    return False


def resume_cmd(harness: str, account: str, cwd: str, uuid: str) -> str:
    cwd = cwd or str(Path.home())
    if harness == "grok":
        grok = os.environ.get("GROK_BIN") or str(Path.home() / ".local/bin/grok")
        return f"cd {cwd!r} && env HOME={str(Path.home())!r} {grok} --always-approve --resume {uuid}"
    if harness == "codex":
        bin_ = os.environ.get("CODEX_BIN") or "codex"
        return f"cd {cwd!r} && {bin_} resume --dangerously-bypass-approvals-and-sandbox {uuid}"
    bin_ = str(Path.home() / ".local/bin/claude-b") if account == "B" else "claude"
    return f"cd {cwd!r} && {bin_} --dangerously-skip-permissions --resume {uuid}"


def _tmux(*args: str) -> str:
    try:
        return subprocess.run(["tmux", "-S", SOCKET, *args], capture_output=True,
                              text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def park(*, slug: str = "", uuid: str = "", cwd: str = "", harness: str = "",
         account: str = "", target: str = "") -> dict:
    slug = slug or (target.split(":")[0] if target else "")
    sess = slug or (target.split(":")[0] if target else "")
    rec = {
        "at": time.time(),
        "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "slug": slug,
        "uuid": uuid,
        "cwd": cwd,
        "harness": harness or "claude",
        "account": account,
        "target": target,
        "resume": resume_cmd(harness or "claude", account, cwd, uuid) if uuid else "",
    }
    d = parked_dir()
    d.mkdir(parents=True, exist_ok=True)
    if uuid:
        write_json_atomic(d / f"{uuid}.json", rec)
    if slug:
        write_json_atomic(d / f"{slug}.json", rec)
    ledger = state_dir() / "reaped" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a") as fh:
        fh.write(json.dumps(rec) + "\n")

    if sess and sess not in {"main", "mainA", "mainB", "mainO", "main-O", "oneshot", "cc"}:
        # TERM the agent first so it can flush; then drop the tmux session.
        pids = _tmux("list-panes", "-t", sess, "-F", "#{pane_pid}").split()
        for p in pids:
            try:
                os.kill(int(p), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        time.sleep(0.4)
        _tmux("kill-session", "-t", sess)

    spawned = state_dir() / "spawned" / f"{slug}.json"
    if slug and spawned.is_file():
        dest = d / f"{slug}.spawned.json"
        try:
            spawned.replace(dest)
        except OSError:
            pass
    return rec


def unpark(uuid: str = "", slug: str = "") -> None:
    d = parked_dir()
    for name in (uuid, slug):
        if name:
            (d / f"{name}.json").unlink(missing_ok=True)
