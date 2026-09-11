"""Park a verified, inactive agent while preserving its transcript and shell.

Save resume information before requesting termination. Recheck ownership and
completion immediately before signalling; keep the key visible until the
process exits. Unknown identities, running tools and shared sessions fail closed.
"""
from __future__ import annotations

import json
import os
import signal
import shlex
import re
import fcntl
import select
import subprocess
import time
from pathlib import Path

from .paths import state_dir, write_json_atomic


SOCKET = os.environ.get("TALLY_TMUX_SOCKET", "/tmp/tmux-1000/cc")


def parked_dir() -> Path:
    return state_dir() / "parked"


def is_parked(uuid: str = "", slug: str = "") -> bool:
    from .paths import read_json, safe_id
    d = parked_dir()
    for name in (uuid, slug):
        if not safe_id(name) or not (d / f"{name}.json").is_file():
            continue
        rec = read_json(d / f"{name}.json") or {}
        if rec.get("checkpoint") and rec.get("transcript"):
            try:
                st = Path(rec["transcript"]).stat()
                if [st.st_mtime_ns, st.st_size] != rec["checkpoint"]:
                    unpark(rec.get("uuid", ""), rec.get("slug", ""))
                    return False  # resumed work returns to the deck on activity
            except OSError:
                pass
        return True
    return False


def resume_cmd(harness: str, account: str, cwd: str, uuid: str) -> str:
    cwd = cwd or str(Path.home())
    if harness == "grok":
        binary = os.environ.get("GROK_BIN") or str(Path.home() / ".local/bin/grok")
        argv = ["env", "HOME=" + str(Path.home()), binary, "--always-approve", "--resume", uuid]
    elif harness == "codex":
        from .launch import resolve_launch
        route = resolve_launch(account or "O", "codex")
        argv = ["env", "CODEX_HOME=" + route["codex_home"], route["binary"],
                "resume", "--dangerously-bypass-approvals-and-sandbox", uuid]
    else:
        binary = str(Path.home() / ".local/bin/claude-b") if account == "B" else "claude"
        argv = [binary, "--dangerously-skip-permissions", "--resume", uuid]
    return "cd " + shlex.quote(cwd) + " && " + shlex.join(argv)


def _candidate(slug: str, uuid: str, harness: str, target: str) -> dict:
    """Fresh process/transcript proof; never authorize a stop from a tile label."""
    import importlib.machinery
    import importlib.util
    from .paths import contrib_bin
    loader = importlib.machinery.SourceFileLoader("tally_park_reaper", contrib_bin("tally-reap"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    reaper = importlib.util.module_from_spec(spec)
    loader.exec_module(reaper)
    if slug in reaper.PROTECTED | {"cc"} or slug.startswith("_fl-") or slug in reaper.telegraph_sessions():
        raise ValueError("This is a shared or routed session. Open it to manage it.")
    # Never kill several projects or a pane selected by tmux's fuzzy matching.
    panes = _tmux("list-panes", "-s", "-t", "=" + slug,
                  "-F", "#{session_name}:#{window_index}.#{pane_index}").splitlines()
    if len(panes) != 1 or (target and panes[0] != target):
        raise ValueError("The session is shared, missing, or its pane has changed.")
    rec = next((r for r in reaper.collect() if r["slug"] == slug), None)
    if not rec or not rec.get("pids") or rec.get("telegraph"):
        raise ValueError("No independently verified agent belongs to this key.")
    if harness == "grok":
        from .sources.grok_sessions import _uuid_from_fds, _tail_records, _update
        from .brief import _grok_session_file
        ids = {_uuid_from_fds(pid) for pid in rec["pids"]} - {""}
        fp = _grok_session_file(uuid, [Path.home() / ".grok/sessions"])
        if ids != {uuid} or not fp:
            raise ValueError("Grok's live process does not prove this session identity.")
        updates = fp.with_name("updates.jsonl")
        # Quiet thinking is still work. Require the real completion event,
        # followed only by bookkeeping, not merely an old assistant chunk.
        lifecycle = {"turn_completed", "user_message_chunk", "agent_message_chunk",
                     "agent_thought_chunk", "tool_call", "tool_call_update",
                     "auto_compact_started"}
        last = next((_update(r).get("sessionUpdate") for r in reversed(_tail_records(updates))
                     if _update(r).get("sessionUpdate") in lifecycle), "")
        rec.update(uuid=uuid, transcript=str(fp), state="idle" if last == "turn_completed" else "working",
                   idle_h=(time.time() - max(fp.stat().st_mtime, updates.stat().st_mtime)) / 3600)
        rec["pids"] = [p for p in rec["pids"] if _uuid_from_fds(p) == uuid]
    elif rec.get("harness") == "codex":
        rec["pids"] = [p for p in rec["pids"] if rec.get("transcript") in reaper.rollout_fds([p])]
    else:
        ids = {reaper.fd_uuid(p) for p in rec["pids"]} - {""}
        if ids != {uuid}:
            raise ValueError("Several or unknown agent identities share this pane.")
        rec["pids"] = [p for p in rec["pids"] if reaper.fd_uuid(p) == uuid]
        if not _claude_complete(Path(rec.get("transcript") or "")):
            raise ValueError("The session has not recorded a completed turn. Nothing was stopped.")
    if rec.get("uuid") != uuid or not rec.get("transcript") or not rec["pids"]:
        raise ValueError("The live transcript does not match this key. Nothing was stopped.")
    if rec.get("state") not in {"idle", "success", "attention"} or (rec.get("idle_h") or 0) < 15 / 3600:
        raise ValueError("The session is still active. Wait for its turn to finish before parking.")
    if any(pid not in rec["pids"] for root in rec["pids"] for pid in reaper.descendants(root)):
        raise ValueError("A tool or child process is still running. Nothing was stopped.")
    return rec


def _claude_complete(path: Path) -> bool:
    from .transcripts import records_backwards
    for rec in records_backwards(path):
        if rec.get("type") == "system" and rec.get("subtype") == "turn_duration":
            return True
        if rec.get("type") == "user":
            return False
        if rec.get("type") == "assistant":
            return (rec.get("message") or {}).get("stop_reason") == "end_turn"
    return False


def _tmux(*args: str) -> str:
    try:
        return subprocess.run(["tmux", "-S", SOCKET, *args], capture_output=True,
                              text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def park(*, slug: str = "", uuid: str = "", cwd: str = "", harness: str = "",
         account: str = "", target: str = "") -> dict:
    from .paths import safe_id
    slug = slug or (target.split(":")[0] if target else "")
    if not safe_id(slug) or not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return {"status": "refused", "message": "A live session and full resume ID are required to park."}
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "park.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rec = _candidate(slug, uuid, harness, target)
            # The process owns cwd/account; stale caller metadata cannot change
            # where the saved history will be resumed.
            cwd = rec.get("cwd") or cwd
            account = rec.get("account") or account
            resume = resume_cmd(harness or rec.get("harness", "claude"), account, cwd, uuid)
            entry = dict(at=time.time(), slug=slug, uuid=uuid, cwd=cwd,
                         harness=harness, account=account, target=target,
                         transcript=rec["transcript"], resume=resume)
            # Save BEFORE signalling. A pending record is not a hidden tile.
            history = root / "reaped"
            history.mkdir(parents=True, exist_ok=True)
            write_json_atomic(history / (uuid + ".resume.json"), entry)
            handles = []
            try:
                for pid in rec["pids"]:
                    handles.append(os.pidfd_open(pid))
                current = _candidate(slug, uuid, harness, target)
                if set(current["pids"]) != set(rec["pids"]):
                    raise ValueError("The process changed during parking. Nothing was stopped.")
                for fd in handles:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if all(select.select([fd], [], [], 0)[0] for fd in handles):
                        break
                    time.sleep(.1)
                else:
                    return {**entry, "status": "pending", "message":
                            "Stop requested; the agent is still exiting. The key remains visible."}
            finally:
                for fd in handles:
                    os.close(fd)
            # Never kill-session: the shell and any other linked view survive.
            st = Path(entry["transcript"]).stat()
            entry["checkpoint"] = [st.st_mtime_ns, st.st_size]
            entry.update(status="parked", message="Parked. History saved; the terminal shell stays available.")
            d = parked_dir()
            d.mkdir(parents=True, exist_ok=True)
            write_json_atomic(d / (uuid + ".json"), entry)
            write_json_atomic(d / (slug + ".json"), entry)
            with (history / "ledger.jsonl").open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
            spawned = root / "spawned" / (slug + ".json")
            if spawned.is_file():
                spawned.replace(d / (slug + ".spawned.json"))
            return entry
        except (OSError, ValueError, KeyError, AttributeError, ImportError) as exc:
            return {"status": "refused", "message": str(exc)}


def unpark(uuid: str = "", slug: str = "") -> None:
    from .paths import safe_id
    d = parked_dir()
    for name in (uuid, slug):
        if safe_id(name):
            (d / f"{name}.json").unlink(missing_ok=True)
