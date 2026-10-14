"""Presence ranking, live identity from Claude's registry, and the one exit key."""
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tallydeck.signal import Signal, rank, ATTENTION, BLOCKED, WORKING, SUCCESS, IDLE
from tallydeck.presence import Presence, VIEW, ATTACHED, LIVE, NONE
from tallydeck.paths import claude_account, claude_bin
from tallydeck.titles import PaneInfo, resolve_label

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


# ── presence tiers and filters ──────────────────────────────────────────────

def fake_tmux(clients, panes):
    def run(argv, **kw):
        out = clients if "list-clients" in argv else panes
        return subprocess.CompletedProcess(argv, 0, "\n".join(out) + "\n", "")
    return run


PANES = [  # target|pane_id|window_id|session|pid
    "mainC:1.1|%1|@1|mainC|111", "mainC:1.2|%2|@1|mainC|112",
    "mainB:2.1|%3|@2|mainB|113", "mainB:1.1|%4|@3|mainB|114",
    "groups:1.1|%5|@4|groups|115",
]
CLIENTS = [  # session|current window|activity
    f"mainC|@1|{NOW - 60:.0f}", f"mainB|@3|{NOW - 60:.0f}",
    f"_fl-groups|@4|{NOW - 5:.0f}",          # the router's own view: not a terminal
    f"old|@9|{NOW - 13 * 3600:.0f}",         # a ghost client: not a terminal
]


def sess(sid, state, tmux="", updated=NOW - 30, group="cc", **meta):
    return Signal(id=sid, label=sid, state=state, updated=updated, group=group,
                  meta=dict({"session": sid, "tmux": tmux}, **meta))


def presence(lanes=()):
    p = Presence(runner=fake_tmux(CLIENTS, PANES), background_quiet=3600)
    p.is_lane = lambda s, snap: (s.meta.get("tmux") in lanes)
    return p


def test_tiers_follow_what_the_terminals_show():
    sigs = [sess("view", IDLE, "mainC:1.2"), sess("attached", IDLE, "mainB:2.1"),
            sess("bg", WORKING, "groups:1.1"), sess("ghost", WORKING, ""),
            sess("outside", WORKING, "", live=True)]
    out = {s.id: s.meta["presence"] for s in presence().apply(sigs, NOW)}
    assert out == {"view": VIEW, "attached": ATTACHED, "bg": LIVE, "outside": LIVE}
    # a log with no pane and no process is history, not a session
    assert "ghost" not in out


def test_quiet_background_sessions_leave_but_never_the_ones_in_view():
    sigs = [sess("lane-old", SUCCESS, "groups:1.1", updated=NOW - 7200),
            sess("view-old", IDLE, "mainC:1.1", updated=NOW - 86400),
            sess("lane-busy", WORKING, "groups:1.1", updated=NOW - 7200)]
    out = {s.id for s in presence().apply(sigs, NOW)}
    assert out == {"view-old", "lane-busy"}


def test_urgent_survives_every_filter():
    sigs = [sess("dead-ask", BLOCKED, ""), sess("bg-ask", ATTENTION, "groups:1.1",
                                               updated=NOW - 86400, asked=True)]
    out = {s.id: s.state for s in presence().apply(sigs, NOW)}
    assert out == {"dead-ask": BLOCKED, "bg-ask": ATTENTION}


def test_a_lane_question_is_for_its_chat_not_the_operator():
    prose = sess("lane", ATTENTION, "groups:1.1")
    dialog = sess("lane-dialog", ATTENTION, "groups:1.1", asked=True)
    out = {s.id: s for s in presence(lanes={"groups:1.1"}).apply([prose, dialog], NOW)}
    assert out["lane"].state == SUCCESS and out["lane"].meta["lane"]
    assert out["lane-dialog"].state == ATTENTION          # the harness is holding a dialog


def test_non_session_keys_and_meters_pass_untouched():
    flag = Signal(id="sig/x", label="x", state=ATTENTION, group="sig")
    meter = Signal(id="burn/session", label="burn", group="burn", meta={"meter": True})
    board = Signal(id="hunt/cars", label="cars", state=SUCCESS, group="exec")
    out = presence().apply([flag, meter, board], NOW)
    assert [s.id for s in out] == ["sig/x", "burn/session", "hunt/cars"]
    assert board.meta["presence"] == NONE


def test_unreadable_tmux_fails_open():
    def broken(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "no server")
    sigs = [sess("ghost", WORKING, "")]
    assert Presence(runner=broken).apply(sigs, NOW) == sigs


# ── ranking ─────────────────────────────────────────────────────────────────

def ranked(*sigs):
    return [s.id for s in rank(list(sigs))]


def test_asking_beats_everything_then_presence_beats_state():
    def mk(sid, state, tier, prio=0):
        return Signal(id=sid, label=sid, state=state, priority=prio,
                      meta={"presence": tier})
    order = ranked(mk("bg-working", WORKING, LIVE, prio=900),
                   mk("view-idle", IDLE, VIEW),
                   mk("view-working", WORKING, VIEW),
                   mk("bg-attention", ATTENTION, LIVE),
                   mk("view-attention", ATTENTION, VIEW),
                   mk("bg-blocked", BLOCKED, NONE),
                   mk("attached-done", SUCCESS, ATTACHED))
    assert order == ["bg-blocked", "view-attention", "bg-attention",
                     "view-working", "view-idle", "attached-done", "bg-working"]


def test_without_presence_the_old_state_order_holds():
    a = Signal(id="a", label="a", state=SUCCESS, priority=5)
    b = Signal(id="b", label="b", state=WORKING)
    c = Signal(id="c", label="c", state=SUCCESS, priority=9)
    assert ranked(a, b, c) == ["b", "c", "a"]


# ── accounts ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("home,acct", [
    ("/home/u", "A"), ("/home/u/.openclaw/.claude-b", "B"),
    ("/home/u/.openclaw/.claude-c/.claude", "C"), ("/x/.claude-d/", "D"),
    ("/home/u/.claude", "A"), ("", "A")])
def test_account_comes_from_the_home_suffix(home, acct):
    assert claude_account(home) == acct


def test_account_binary_is_the_accounts_launcher(tmp_path):
    bin_dir = tmp_path / ".local" / "bin"; bin_dir.mkdir(parents=True)
    c = bin_dir / "claude-c"; c.write_text("#!/bin/sh\n"); c.chmod(0o755)
    assert claude_bin("C", tmp_path) == str(c)
    assert claude_bin("A", tmp_path) == "claude"
    assert claude_bin("D", tmp_path) == "claude"           # no launcher: never guess


# ── Claude's process registry ───────────────────────────────────────────────

def own_start():
    return Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]


def claude_home(tmp_path, name, uid, text, age, status, start=None):
    home = tmp_path / name
    proj = home / ".claude" / "projects" / "-home-u"
    proj.mkdir(parents=True)
    fp = proj / f"{uid}.jsonl"
    fp.write_text(json.dumps({"type": "assistant", "cwd": "/home/u", "message": {
        "content": [{"type": "text", "text": text}]}}) + "\n")
    os.utime(fp, (time.time() - age, time.time() - age))
    reg = home / ".claude" / "sessions"; reg.mkdir()
    (reg / f"{os.getpid()}.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": uid, "cwd": "/home/u", "kind": "interactive",
        "procStart": own_start() if start is None else start, "status": status,
        "updatedAt": int(time.time() * 1000), "tmux": ""}))
    return home


def source(tmp_path, monkeypatch, **opts):
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path / "state"))
    from tallydeck.sources.claude_sessions import ClaudeSessionsSource
    opts.setdefault("root", str(tmp_path / "no-default-root"))   # never the real ~/.claude
    return ClaudeSessionsSource(socket=str(tmp_path / "no.sock"), sync_titles=False, **opts)


UID = "5e1f0c2a-0000-4000-8000-000000000001"


@pytest.mark.parametrize("status,text,state", [
    ("busy", "Do you want X?", WORKING),       # mid-turn: a '?' is not a question yet
    ("waiting", "Working on it.", ATTENTION),  # a dialog is holding for the human
    ("idle", "All done.", SUCCESS),
    ("idle", "Should I ship it?", ATTENTION),
])
def test_live_session_survives_quiet_and_uses_claudes_status(tmp_path, monkeypatch,
                                                            status, text, state):
    home = claude_home(tmp_path, ".claude-c", UID, text, age=7200, status=status)
    src = source(tmp_path, monkeypatch, discover=[str(tmp_path / ".claude-*")])
    sigs = src.poll()
    assert [s.id for s in sigs] == [f"cc/C-{UID[:8]}"]
    assert sigs[0].state == state and sigs[0].meta["live"]
    assert sigs[0].meta["asked"] == (status == "waiting")


def test_a_recycled_pid_is_not_a_live_session(tmp_path, monkeypatch):
    home = claude_home(tmp_path, ".claude-c", UID, "All done.", age=7200,
                       status="idle", start="1")
    src = source(tmp_path, monkeypatch, discover=[str(tmp_path / ".claude-*")])
    assert src.poll() == []                     # stale log, no process: dropped


def test_discovery_adds_accounts_and_explicit_roots_keep_their_label(tmp_path, monkeypatch):
    b = claude_home(tmp_path, ".claude-b", UID, "ok", age=10, status="idle")
    src = source(tmp_path, monkeypatch, discover=[str(tmp_path / ".claude-*")],
                 roots=[{"label": "B2", "path": str(b / ".claude" / "projects")}])
    assert [label for label, _ in src.roots] == ["B2"]


# ── labels ──────────────────────────────────────────────────────────────────

def pane(window_panes, title="", auto=""):
    return PaneInfo("c2:1.1", "%1", "c2", "node", 1, window_panes, title, auto)


def test_a_session_name_does_not_label_five_panes_alike():
    assert resolve_label(harness_prose="Fix the wallet send flow please",
                         pane=pane(5), cwd="/home/u") != "c2"
    assert resolve_label(harness_prose="Fix the wallet send flow please",
                         pane=pane(1), cwd="/home/u") == "c2"


def test_shared_window_without_a_topic_keeps_the_panes_known_title():
    assert resolve_label(pane=pane(3, "PearlWallet v0.7", "PearlWallet v0.7"),
                         cwd="/home/u/projects/wallet") == "PearlWallet v0.7"
    assert resolve_label(pane=pane(3), cwd="/home/u/projects/wallet") == "wallet"


# ── popups: one exit key, key-cap bar ───────────────────────────────────────

def test_keybar_puts_the_exit_key_last_even_when_unnamed():
    pytest.importorskip("rich")
    from tallydeck.popup import keybar
    bar = keybar("Enter open · x park", 80).plain
    assert bar.index("Enter") < bar.index("x") < bar.index("Ctrl-Q")
    assert bar.rstrip().endswith("Ctrl-Q  close") and len(bar) <= 80
    from tallydeck.popup import Console
    out = io.StringIO()
    Console(file=out, width=32, color_system=None).print(
        keybar("Enter open · i reply · t tab · x park · Space done · Ctrl-Q close", 32))
    lines = out.getvalue().splitlines()
    assert all(len(line) <= 32 for line in lines) and "Ctrl-Q" in lines[-1]


def test_ctrl_q_closes_a_popup_in_a_real_terminal(tmp_path):
    pytest.importorskip("rich")
    import pty, select
    master, slave = pty.openpty()
    runner = tmp_path / "view.py"
    runner.write_text('''from tallydeck.popup import choose, body
from pathlib import Path
import sys
key = choose(lambda w: body("Ship it?", "THE ASK", w), "Enter open · Ctrl-Q close",
             timeout=10, valid_keys={"\\n"})
Path(sys.argv[1]).write_text(repr(key))
''')
    result = tmp_path / "key"
    env = dict(os.environ, PYTHONPATH=str(ROOT), TERM="xterm-256color")
    proc = subprocess.Popen([sys.executable, str(runner), str(result)], stdin=slave,
                            stdout=slave, stderr=slave, env=env)
    try:
        data, deadline = b"", time.monotonic() + 5
        while b"Ship it?" not in data and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                data += os.read(master, 65536)
        os.write(master, b"\x11")            # XON on a default tty: must still arrive
        proc.wait(timeout=3)
        assert result.read_text() == "'q'"
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        os.close(master); os.close(slave)


def fake_router_env(tmp_path, key):
    fake = tmp_path / "bin"; fake.mkdir()
    package = tmp_path / "pkg" / "tallydeck"; package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import os,sys\nfrom pathlib import Path\na=sys.argv\n"
        "if '--actions' in a: Path(os.environ['TEST_ACTIONS']).write_text(a[a.index('--actions')+1])\n"
        "if '--action-file' in a: Path(a[a.index('--action-file')+1]).write_text(os.environ['TEST_KEY'])\n")
    tmux = fake / "tmux"
    tmux.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOG"
case "$*" in
  *list-sessions*|*list-windows*) exit 0;;
  *has-session*) exit 1;;
  *client_session*) echo here;;
  *window_id*) echo '@1';;
  *pane_id*) echo '%1';;
esac
exit 0
'''); tmux.chmod(0o755)
    return dict(os.environ, PATH=str(fake) + ":" + os.environ["PATH"], HOME=str(tmp_path),
                TALLYDECK_HOME=str(package.parent), TALLYDECK_STATE=str(tmp_path / "state"),
                TEST_KEY=key, TEST_LOG=str(tmp_path / "calls"),
                TEST_ACTIONS=str(tmp_path / "actions"))


def test_router_opens_the_floating_view_with_the_exit_key_bound(tmp_path):
    env = fake_router_env(tmp_path, "\n")
    r = subprocess.run([str(ROOT / "contrib/tally-route"), "tty", "here", "work:1.1",
                        UID, str(tmp_path), "Brief", "attention", "C", "cc/C-x", ""],
                       env=env, cwd=tmp_path, capture_output=True, text=True, timeout=8)
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls").read_text()
    assert "bind-key -n C-q" in calls and "detach-client" in calls
    assert "status-left" in calls and "Ctrl-Q" in calls
    bar = (tmp_path / "actions").read_text()
    assert bar.endswith("Ctrl-Q close") and "Ctrl-b" not in bar


def test_router_resumes_each_account_with_its_own_launcher(tmp_path):
    env = fake_router_env(tmp_path, "\n")     # Enter on a paneless key resumes it
    launcher = tmp_path / ".local" / "bin" / "claude-c"
    launcher.parent.mkdir(parents=True); launcher.write_text("#!/bin/sh\n"); launcher.chmod(0o755)
    r = subprocess.run([str(ROOT / "contrib/tally-route"), "tty", "here", "", UID,
                        str(tmp_path), "Brief", "attention", "C", "cc/C-x", ""],
                       env=env, cwd=tmp_path, capture_output=True, text=True, timeout=8)
    assert r.returncode == 0, r.stderr
    assert f"{launcher} --dangerously-skip-permissions --resume {UID}" in \
        (tmp_path / "calls").read_text()
