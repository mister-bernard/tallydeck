"""Burning is token flow, not log growth; ages are record times, not mtimes."""
import json
import os
import time
from pathlib import Path

import pytest

from tallydeck.activity import (ClaudeTail, CodexTail, Tails, assess, effective,
                                ACTIVE, GEN_CAP)
from tallydeck.signal import Signal, rank, WORKING, SUCCESS, IDLE, ATTENTION


def stamp(age):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - age))


def assistant(age, blocks, stop, mid="msg_1", out=100, cr=0):
    return {"type": "assistant", "timestamp": stamp(age), "message": {
        "id": mid, "stop_reason": stop, "content": blocks,
        "usage": {"input_tokens": 2, "output_tokens": out, "cache_read_input_tokens": cr,
                  "cache_creation_input_tokens": 0}}}


def tool_use(name="Bash", tid="toolu_1"):
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def tool_result(age, tid="toolu_1"):
    return {"type": "user", "timestamp": stamp(age), "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def prompt(age):
    return {"type": "user", "timestamp": stamp(age), "message": {"content": "go on"}}


def write(path, records, mtime_age=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    if mtime_age is not None:
        t = time.time() - mtime_age
        os.utime(path, (t, t))
    return path


def tail(tmp_path, records, name="s.jsonl"):
    t = ClaudeTail(write(tmp_path / name, records))
    t.refresh()
    return t


# ── the reader ──────────────────────────────────────────────────────────────

def test_a_message_logged_once_per_block_counts_once(tmp_path):
    t = tail(tmp_path, [assistant(30, [{"type": "thinking"}], "tool_use", out=1000),
                        assistant(29, [tool_use()], "tool_use", out=1000)])
    assert len(t.usage) == 1
    assert t.tokens(60, time.time()) == pytest.approx(effective(2, 0, 0, 1000))


def test_open_calls_close_on_their_result_and_a_prompt_supersedes_them(tmp_path):
    t = tail(tmp_path, [assistant(30, [tool_use("Bash", "a"), tool_use("Read", "b")], "tool_use"),
                        tool_result(20, "a")])
    assert set(t.pending) == {"b"} and t.last_kind == "tool_result"
    t2 = tail(tmp_path, [assistant(30, [tool_use("Bash", "a")], "tool_use"), prompt(5)], "p.jsonl")
    assert t2.pending == {} and t2.last_kind == "prompt"


def test_appends_are_read_incrementally(tmp_path):
    fp = write(tmp_path / "s.jsonl", [prompt(20)])
    t = ClaudeTail(fp); t.refresh()
    assert t.last_role == "user"
    with fp.open("a") as f:
        f.write(json.dumps(assistant(1, [{"type": "text", "text": "done"}], "end_turn")) + "\n")
    t.refresh()
    assert t.ended and t.last_role == "assistant"


def test_effective_tokens_weigh_cache_reads_lightly_and_output_heavily():
    assert effective(cache_read=1000) == pytest.approx(100)
    assert effective(output=1000) == pytest.approx(5000)


# ── what a session is doing ─────────────────────────────────────────────────

def test_a_model_call_in_flight_is_burning(tmp_path):
    t = tail(tmp_path, [assistant(400, [tool_use()], "tool_use"), tool_result(5)])
    a = assess(t, [], "busy")
    assert a.phase == "generating" and a.burning


def test_a_long_tool_run_is_working_but_not_burning(tmp_path):
    t = tail(tmp_path, [assistant(ACTIVE + 200, [tool_use("Bash")], "tool_use")])
    a = assess(t, [], "busy")
    assert a.phase == "tool" and a.tool == "Bash" and not a.burning


def test_a_tight_tool_loop_is_burning(tmp_path):
    t = tail(tmp_path, [assistant(8, [tool_use("Bash")], "tool_use")])
    a = assess(t, [], "busy")
    assert a.phase == "tool" and a.burning          # a call landed seconds ago


def test_stale_busy_never_reopens_a_closed_turn(tmp_path):
    """Claude Code keeps `busy` while background work runs after the turn
    ended — the GX10 key that stayed blue over a finished conversation."""
    t = tail(tmp_path, [assistant(450, [{"type": "text"}], "end_turn")])
    a = assess(t, [], "busy")
    assert a.phase == "ended" and not a.burning


def test_background_agents_keep_a_finished_turn_working(tmp_path):
    main = tail(tmp_path, [assistant(450, [{"type": "text"}], "end_turn")])
    agent = tail(tmp_path, [prompt(300), assistant(10, [tool_use()], "tool_use", mid="m2", out=900)],
                 "agent-x.jsonl")
    a = assess(main, [agent], "busy")
    assert a.phase == "background" and a.burning and a.agents == 1 and a.rate > 0


def test_interrupted_and_dead_turns_are_not_in_flight(tmp_path):
    t = tail(tmp_path, [assistant(400, [tool_use()], "tool_use", out=1), tool_result(100)])
    assert assess(t, [], "idle").phase == "idle"
    assert not assess(t, [], "dead").burning
    assert assess(t, [], "waiting").phase == "waiting"


def test_an_answer_that_never_comes_is_a_stall_not_a_burn(tmp_path):
    t = tail(tmp_path, [tool_result(GEN_CAP + 60)])
    a = assess(t, [], "busy")
    assert a.phase == "generating" and not a.burning


def test_workflow_agents_are_found_under_the_subagent_tree(tmp_path):
    main = write(tmp_path / "proj" / "sess.jsonl", [assistant(450, [{"type": "text"}], "end_turn")])
    write(tmp_path / "proj" / "sess" / "subagents" / "agent-a.jsonl", [prompt(60)])
    write(tmp_path / "proj" / "sess" / "subagents" / "workflows" / "wf_1" / "agent-b.jsonl",
          [prompt(30), assistant(5, [tool_use()], "tool_use", mid="m9")])
    T = Tails(ClaudeTail)
    subs = T.subagents(main, time.time())
    assert sorted(Path(s.path).name for s in subs) == ["agent-a.jsonl", "agent-b.jsonl"]


def test_codex_counts_each_model_call_once(tmp_path):
    def tc(age, total, inp, cached, out):
        return {"timestamp": stamp(age), "type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"total_tokens": total},
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                     "output_tokens": out, "total_tokens": inp + out}}}}
    recs = [tc(40, 1000, 900, 800, 100), tc(39, 1000, 900, 800, 100),   # repeat: same call
            {"timestamp": stamp(30), "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command", "call_id": "c1"}}]
    t = CodexTail(write(tmp_path / "rollout-x.jsonl", recs)); t.refresh()
    assert len(t.usage) == 1
    assert t.tokens(120, time.time()) == pytest.approx(effective(100, 0, 800, 100))
    assert assess(t, [], "").phase == "tool"


# ── the Claude source ───────────────────────────────────────────────────────

UID = "7a1f0c2a-0000-4000-8000-000000000002"


def own_start():
    return Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]


def session(tmp_path, monkeypatch, records, status="idle", live=True, agents=(),
            mtime_age=None):
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path / "state"))
    home = tmp_path / ".claude-c"
    main = write(home / ".claude" / "projects" / "-home-u" / f"{UID}.jsonl", records, mtime_age)
    for i, recs in enumerate(agents):
        write(home / ".claude" / "projects" / "-home-u" / UID / "subagents" / f"agent-{i}.jsonl", recs)
    reg = home / ".claude" / "sessions"; reg.mkdir(parents=True)
    if live:
        (reg / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": UID, "kind": "interactive", "procStart": own_start(),
            "status": status, "updatedAt": int(time.time() * 1000), "tmux": ""}))
    from tallydeck.sources.claude_sessions import ClaudeSessionsSource
    src = ClaudeSessionsSource(root=str(tmp_path / "none"), discover=[str(tmp_path / ".claude-*")],
                               socket=str(tmp_path / "no.sock"), sync_titles=False)
    return src.poll()


def test_a_touched_log_does_not_look_fresh(tmp_path, monkeypatch):
    """Claude Code re-stamps logs (utimes) without writing: the conversation
    ended two hours ago, the file says now."""
    recs = [assistant(7200, [{"type": "text", "text": "All set."}], "end_turn")]
    s, = session(tmp_path, monkeypatch, recs, status="idle", mtime_age=0)
    assert s.state == SUCCESS and s.sublabel == "done · 2h00"
    assert s.updated < time.time() - 7000
    assert session(tmp_path / "dead", monkeypatch, recs, live=False, mtime_age=0) == []


def test_background_agents_show_as_working_with_their_burn(tmp_path, monkeypatch):
    recs = [assistant(500, [{"type": "text", "text": "Started the collector."}], "end_turn")]
    agent = [prompt(200), assistant(5, [tool_use()], "tool_use", mid="m2", out=2000)]
    s, = session(tmp_path, monkeypatch, recs, status="busy", agents=[agent])
    assert s.state == WORKING and s.meta["burning"] and s.priority > 0
    assert s.sublabel.startswith("1 agent · ") and s.sublabel.endswith("k/m")


def test_a_long_tool_run_says_so_and_takes_no_heat(tmp_path, monkeypatch):
    recs = [prompt(700), assistant(600, [tool_use("Bash")], "tool_use")]
    s, = session(tmp_path, monkeypatch, recs, status="busy")
    assert s.state == WORKING and s.meta["burning"] is False and s.priority == 0
    assert s.sublabel == "Bash · 10m"


def test_a_question_at_the_end_of_a_turn_survives_background_busy(tmp_path, monkeypatch):
    recs = [assistant(60, [{"type": "text", "text": "Launched it. Should I also redeploy?"}],
                      "end_turn")]
    s, = session(tmp_path, monkeypatch, recs, status="busy")
    assert s.state == ATTENTION


# ── ranking ─────────────────────────────────────────────────────────────────

def test_burning_then_just_done_then_waiting_on_a_tool_then_idle():
    def mk(sid, state, burning=None):
        meta = {"presence": 3}
        if burning is not None:
            meta["burning"] = burning
        return Signal(id=sid, label=sid, state=state, meta=meta)
    order = [s.id for s in rank([mk("tool", WORKING, False), mk("idle", IDLE),
                                 mk("done", SUCCESS), mk("burn", WORKING, True)])]
    assert order == ["burn", "done", "tool", "idle"]


def test_rate_labels_fit_a_key():
    from tallydeck.sources.claude_sessions import _rate
    assert [_rate(850), _rate(12_400), _rate(1_400_000)] == ["850/m", "12k/m", "1.4M/m"]
