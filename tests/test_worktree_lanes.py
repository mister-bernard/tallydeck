#!/usr/bin/env python3
"""Worktree lanes: `tally spawn --worktree` opens one, `tally close` closes it.

The gap these fill: our only isolation was the claim registry, which SERIALISES —
one writer per tree, the second refused. Correct, and also why the repo count
exploded: a lane needing to work alongside another had nowhere to go but a new
repo, so SubnetBridge ossified into 13 of them. Worktrees add the third option,
and `tally close` is the step that stops a lane from becoming a repo.

What these tests actually care about is the REFUSALS. A close that merges is easy;
a close that declines to merge a conflict, or money-touching code, or a tree whose
worker is still writing, is the part that protects work. A gate that proceeds is
not a gate.

Runs standalone (no pytest on this box).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLOSE = ROOT / "contrib" / "tally-close"

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        fails.append(name)
        print(f"  FAIL {name} {detail}")


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, **kw)


def commit(repo, msg):
    git(repo, "add", "-A")
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@t",
         "commit", "-qm", msg], capture_output=True, text=True)


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "f.txt").write_text("base\n")
    commit(path, "base")
    return git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def open_lane(repo: Path, trees: Path, state: Path, slug: str, base: str) -> Path:
    """Open a lane the way tally-spawn --worktree does, and record it."""
    wt = trees / repo.name / slug
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", f"lane/{slug}", str(wt), base)
    (state / "spawned").mkdir(parents=True, exist_ok=True)
    (state / "spawned" / f"{slug}.json").write_text(json.dumps({
        "slug": slug, "cwd": str(wt), "harness": "claude",
        "worktree": {"repo_root": str(repo), "branch": f"lane/{slug}",
                     "base_ref": base}}))
    return wt


def close(state: Path, trees: Path, slug: str, *args):
    env = {**os.environ, "TALLYDECK_STATE": str(state),
           "TALLY_WORKTREE_ROOT": str(trees),
           # Point the liveness check at a socket with no sessions, so the test
           # never depends on what the real fleet happens to be running.
           "TALLY_TMUX_SOCKET": str(state / "no-such-socket")}
    return subprocess.run([str(CLOSE), slug, *args], capture_output=True,
                          text=True, env=env, timeout=120)


def main():
    if not CLOSE.is_file():
        print("tally-close absent; skipping")
        return 0

    with tempfile.TemporaryDirectory(prefix="wtlane-") as d:
        d = Path(d)
        trees, state = d / "trees", d / "state"

        # ── happy path: lane commits + leaves a dirty file; both must land ──
        repo = d / "proj"
        base = make_repo(repo)
        wt = open_lane(repo, trees, state, "l1", base)
        (wt / "new.txt").write_text("lane work\n")
        commit(wt, "lane commit")
        (wt / "dirty.txt").write_text("uncommitted\n")

        r = close(state, trees, "l1")
        check("close merges the lane", r.returncode == 0, f"(rc={r.returncode} {r.stderr[-160:]})")
        check("the lane's committed work landed", (repo / "new.txt").is_file())
        check("the lane's UNCOMMITTED work landed too", (repo / "dirty.txt").is_file())
        check("worktree removed", not wt.exists())
        check("branch deleted",
              git(repo, "show-ref", "--verify", "--quiet", "refs/heads/lane/l1").returncode != 0)
        check("merge is recorded as a merge commit",
              "Merge lane l1" in git(repo, "log", "--oneline", "-3").stdout)
        check("nothing was pushed", "NOT pushed" in r.stderr)

        # ── conflict: abort and keep EVERYTHING, so a human can resolve it ──
        wt2 = open_lane(repo, trees, state, "l2", base)
        (wt2 / "f.txt").write_text("LANE VERSION\n"); commit(wt2, "lane edits f")
        (repo / "f.txt").write_text("BASE VERSION\n"); commit(repo, "base edits f")
        r2 = close(state, trees, "l2")
        check("a conflicting merge fails loudly", r2.returncode != 0, f"(rc={r2.returncode})")
        check("conflict leaves the worktree in place", wt2.exists())
        check("conflict leaves the branch in place",
              git(repo, "show-ref", "--verify", "--quiet", "refs/heads/lane/l2").returncode == 0)
        check("conflict leaves the main repo clean (merge aborted)",
              git(repo, "status", "--porcelain").stdout.strip() == "")
        close(state, trees, "l2", "--abandon")

        # ── money-touching code never gets an automated merge ───────────────
        pearl = d / "pearl-treasury"
        pbase = make_repo(pearl)
        wt3 = open_lane(pearl, trees, state, "l3", pbase)
        (wt3 / "x.txt").write_text("y\n"); commit(wt3, "work")
        r3 = close(state, trees, "l3")
        check("a money-touching repo is refused", r3.returncode == 3, f"(rc={r3.returncode})")
        check("the refusal cites the build lifecycle", "build lifecycle" in r3.stderr)
        check("refusal leaves the lane untouched", wt3.exists())
        # ...but it can still be abandoned deliberately.
        r3b = close(state, trees, "l3", "--abandon")
        check("--abandon still works on a money-touching lane", r3b.returncode == 0)
        check("abandon removes the tree", not wt3.exists())
        check("abandon does NOT merge", not (pearl / "x.txt").exists())

        # ── --no-commit must refuse rather than discard dirty work ──────────
        wt4 = open_lane(repo, trees, state, "l4", base)
        (wt4 / "only-dirty.txt").write_text("unsaved\n")
        r4 = close(state, trees, "l4", "--no-commit")
        check("--no-commit refuses a dirty tree", r4.returncode == 3, f"(rc={r4.returncode})")
        check("the dirty file was not silently dropped", (wt4 / "only-dirty.txt").is_file())

        # ── a non-worktree lane cannot be closed this way ───────────────────
        (state / "spawned" / "plain.json").write_text(json.dumps(
            {"slug": "plain", "cwd": str(repo), "harness": "claude"}))
        r5 = close(state, trees, "plain")
        check("a non-worktree lane is refused, not guessed at", r5.returncode == 2,
              f"(rc={r5.returncode})")

    print("OK" if not fails else f"{len(fails)} FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
