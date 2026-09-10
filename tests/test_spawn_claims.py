#!/usr/bin/env python3
"""tally-spawn ownership claims: exactly one writer per declared tree.

The failure: on 2026-09-09 /home/openclaw/projects/taobridge-contracts was handed to
two writers two minutes apart — a codex-oneshot batch job and an interactive pane, on
the same account. The second launcher looked for a running worker, correctly found
none (the first was a one-shot and left no record) and started a duplicate.

So these tests care about the CROSS-LAUNCHER property above all: a claim taken by
codex-oneshot must block tally-spawn and vice versa. A registry only one launcher
consults would have let that exact incident happen again.

Runs standalone (no pytest — it is not installed on this box).
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPAWN = ROOT / "contrib" / "tally-spawn"
CLAIM = Path("/home/openclaw/scripts/work-claim.py")

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        fails.append(name)
        print(f"  FAIL {name} {detail}")


def holder(resource, owner="other-worker", launcher="codex-oneshot"):
    """A real live process to own the claim. Ownership is proved by a live process,
    never by a timestamp, so the test must hold one rather than fake a record."""
    proc = subprocess.Popen(["sleep", "120"])
    subprocess.run([str(CLAIM), "acquire", resource, "--owner", owner,
                    "--pid", str(proc.pid), "--launcher", launcher],
                   capture_output=True)
    return proc


def release(resource, owner="other-worker"):
    subprocess.run([str(CLAIM), "release", resource, "--owner", owner, "--force"],
                   capture_output=True)


def spawn(slug, *args):
    return subprocess.run([str(SPAWN), slug, *args, "noop"],
                          capture_output=True, text=True, timeout=90)


def preempted(r):
    """True when the MEMORY gate refused before the claim gate was ever reached.

    Both gates exit 3, so `rc == 3` alone cannot tell them apart — asserting on it
    reports a pass while proving nothing, and the message assertions then 'fail'
    for a reason that has nothing to do with claims. Skip honestly instead: a test
    that fails for an unrelated environmental reason is one people learn to ignore.
    """
    return "not enough memory" in (r.stderr or "")


def main():
    if not CLAIM.is_file():
        print("work-claim.py absent — claims not installed; skipping")
        return 0

    with tempfile.TemporaryDirectory(prefix="spawnclaim-") as d:
        # CROSS-LAUNCHER: a codex-oneshot claim must stop a tally-spawn. This is the
        # whole point; a same-launcher-only guard leaves the original incident intact.
        h = holder(d)
        try:
            r = spawn("claimtest-x", "-c", d)
            if preempted(r):
                print("  SKIP  memory gate refused before the claim gate — "
                      "claim behaviour not exercised (free some swap and re-run)")
                return 0
            check("a codex-oneshot claim refuses tally-spawn", r.returncode == 3,
                  f"(rc={r.returncode})")
            check("the refusal names the live holder",
                  "codex-oneshot" in r.stderr and "live=True" in r.stderr)
            # A gate that has already built something is not free to fire: no session,
            # and no trust entry written for a directory we never opened.
            sess = subprocess.run(["tmux", "has-session", "-t", "=claimtest-x"],
                                  capture_output=True)
            check("refusal creates no tmux session", sess.returncode != 0)
            cj = Path("/home/openclaw/.claude.json")
            check("refusal writes no folder-trust entry",
                  not cj.is_file() or d not in cj.read_text())
            # --share is the documented way to run read-only alongside a holder, so it
            # must get PAST the claim gate (it will stop later for its own reasons).
            r2 = spawn("claimtest-share", "-c", d, "--share")
            check("--share is not blocked by the claim gate",
                  "already owns" not in r2.stderr, f"(stderr={r2.stderr[:80]})")
            if r2.returncode == 0:
                subprocess.run(["tmux", "kill-session", "-t", "=claimtest-share"],
                               capture_output=True)
        finally:
            h.send_signal(signal.SIGTERM)
            h.wait(timeout=5)
            release(d)

        # A dead owner does NOT silently admit a new writer. "The owner stopped
        # checking in" and "the owner is gone" are different facts, and the tree may
        # be half written — a human looks, then forces.
        dead = subprocess.Popen(["sleep", "0.1"])
        dead.wait()
        subprocess.run([str(CLAIM), "acquire", d, "--owner", "dead-worker",
                        "--pid", str(dead.pid), "--launcher", "codex-oneshot"],
                       capture_output=True)
        try:
            r3 = spawn("claimtest-stale", "-c", d)
            check("a stale claim still refuses (no silent takeover)", r3.returncode == 3,
                  f"(rc={r3.returncode})")
            check("the refusal explains how to take it over", "--force" in r3.stderr)
        finally:
            release(d, "dead-worker")

        # An unclaimed tree is claimable — the gate must not block everything.
        r4 = subprocess.run([str(CLAIM), "check", d], capture_output=True, text=True)
        check("tree is FREE once every owner is gone", r4.stdout.startswith("FREE"))

    print("OK" if not fails else f"{len(fails)} FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
