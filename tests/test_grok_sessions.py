"""Grok Build as a first-class deck harness."""
import json
from pathlib import Path

from tallydeck.signal import Signal, WORKING, SUCCESS, IDLE, is_grok, is_codex
from tallydeck.sources.grok_sessions import classify, GrokSessionsSource
from tallydeck.render import theme
from tallydeck.render.keycard import draw_key
from tallydeck.render.term import render_term
from tallydeck.view import View
from tallydeck.devices import NEO


def _rec(kind, **u):
    return {"params": {"update": {"sessionUpdate": kind, **u}}}


def test_classify_pending_tool_is_working():
    recs = [
        _rec("tool_call", toolCallId="t1"),
        _rec("tool_call_update", toolCallId="t1", status="pending"),
    ]
    assert classify(recs) == WORKING


def test_classify_completed_tools_then_agent_is_success():
    recs = [
        _rec("tool_call", toolCallId="t1"),
        _rec("tool_call_update", toolCallId="t1", status="completed"),
        _rec("agent_message_chunk", content={"type": "text", "text": "done"}),
    ]
    assert classify(recs) == SUCCESS


def test_classify_empty_is_idle():
    assert classify([]) == IDLE


def test_headless_sessions_are_hidden_by_default(tmp_path):
    root = tmp_path / "sessions" / "%2Ftmp%2Fhunt"
    sess = root / "01aaaaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
    sess.mkdir(parents=True)
    (sess / "summary.json").write_text(json.dumps({
        "info": {"id": sess.name, "cwd": "/tmp/hunt"},
        "session_kind": "headless",
        "generated_title": "Fetch CarandClassic listing raw HTML",
        "last_active_at": "2099-01-01T00:00:00Z",
    }))
    (sess / "updates.jsonl").write_text(
        json.dumps(_rec("tool_call", toolCallId="t1")) + "\n")
    src = GrokSessionsSource(root=str(tmp_path / "sessions"),
                             include_headless=False, sync_titles=False)
    assert src.poll() == []
    src.include_headless = True
    got = src.poll()
    assert len(got) == 1
    assert got[0].meta["oneshot"] is True
    assert got[0].meta["harness"] == "grok"
    assert is_grok(got[0]) and not is_codex(got[0])


def test_live_grok_pane_keeps_a_quiet_session_on_the_deck(tmp_path):
    """An idle Grok TUI still occupies a pane. Dropping it after `stale`
    is how live X sessions vanished while only Cruiser remained."""
    import os, time
    root = tmp_path / "sessions" / "%2Ftmp%2Flive"
    uid = "01cccccccc-dddd-7eee-8fff-000000000000"
    sess = root / uid
    sess.mkdir(parents=True)
    (sess / "summary.json").write_text(json.dumps({
        "info": {"id": uid, "cwd": "/tmp/live"},
        "session_kind": "interactive",
        "generated_title": "Move localnet onto 8555",
        "last_active_at": "2020-01-01T00:00:00Z",
    }))
    (sess / "updates.jsonl").write_text("{}\n")
    old = time.time() - 10_000
    os.utime(sess / "updates.jsonl", (old, old))
    src = GrokSessionsSource(root=str(tmp_path / "sessions"),
                             stale=1800, dwell=0, sync_titles=False)
    assert src.poll() == []
    src._live_grok_panes = lambda: {uid: "sb-localnet-move:1.1"}
    got = src.poll()
    assert len(got) == 1
    assert got[0].state == IDLE
    assert got[0].meta["tmux"] == "sb-localnet-move:1.1"
    assert got[0].meta["exact_pane"] is True
    assert "localnet" in got[0].label.lower() or "8555" in got[0].label


def test_interactive_session_becomes_a_key(tmp_path):
    root = tmp_path / "sessions" / "%2Fhome%2Fopenclaw%2Fprojects%2Fcruiser-finder"
    sess = root / "01bbbbbbbb-cccc-7ddd-8eee-ffffffffffff"
    sess.mkdir(parents=True)
    (sess / "summary.json").write_text(json.dumps({
        "info": {"id": sess.name,
                 "cwd": "/home/openclaw/projects/cruiser-finder"},
        "session_kind": "interactive",
        "generated_title": "Cruiser finder hunt",
        "last_active_at": "2099-01-01T00:00:00Z",
    }))
    (sess / "updates.jsonl").write_text("\n".join([
        json.dumps(_rec("tool_call", toolCallId="t1")),
        json.dumps(_rec("tool_call_update", toolCallId="t1",
                        status="completed")),
        json.dumps(_rec("agent_message_chunk",
                        content={"type": "text", "text": "ok"})),
    ]))
    src = GrokSessionsSource(root=str(tmp_path / "sessions"),
                             stale=10**12, dwell=0, sync_titles=False)
    got = src.poll()
    assert len(got) == 1
    assert got[0].meta["harness"] == "grok"
    assert got[0].meta["account"] == "X"
    assert got[0].label
    assert "cruiser" in got[0].label.lower() or got[0].label == "Cruiser finder hunt"[:24]


def test_sessions_on_the_same_pane_collapse_to_one_key():
    """Hunt board + Grok TUI + leftover clones on cruiser-finder:1.1
    are one Cruiser Finder, not a pad of duplicates."""
    from tallydeck.signal import rollup_same_pane, ATTENTION, SUCCESS
    pane = "cruiser-finder:1.1"
    hunt = Signal(id="hunt/cruiser", label="cruiser", state=SUCCESS,
                  group="hunt", sublabel="98 cars · 30 hot",
                  meta={"tmux": pane, "harness": "grok"})
    live = Signal(id="gk/live", label="cruiser-finder", state=WORKING,
                  group="gk", meta={"tmux": pane, "harness": "grok",
                                    "exact_pane": True})
    clone = Signal(id="gk/old", label="cruiser-finder", state=ATTENTION,
                   group="gk", meta={"tmux": pane, "harness": "grok"})
    other = Signal(id="gk/nnum", label="n-number.org pages", state=WORKING,
                   group="gk", meta={"tmux": "", "harness": "grok"})
    got = rollup_same_pane([hunt, live, clone, other])
    by_id = {s.id: s for s in got}
    assert set(by_id) == {"hunt/cruiser", "gk/nnum"}
    merged = by_id["hunt/cruiser"]
    assert merged.state == ATTENTION
    assert merged.meta["rolled"] == 3
    assert "98 cars" in merged.sublabel
    assert "3 sessions" in merged.sublabel


def test_grok_key_wears_its_tally_bar_on_the_bottom():
    """Claude top, Codex left, Grok bottom — harness from shape, not the badge."""
    px = 96
    cc = draw_key(Signal(id="cc/x", label="pv", state=WORKING), px)
    gk = draw_key(Signal(id="gk/x", label="pv", state=WORKING,
                         meta={"harness": "grok", "account": "X"}), px)
    blue = theme.hex_rgb(theme.STATE_COLOR[WORKING])

    def near(px_rgb, ref, tol=26):
        return all(abs(a - b) <= tol for a, b in zip(px_rgb[:3], ref))

    # Claude: top-right is bar, bottom-middle is not.
    assert near(cc.getpixel((px - 3, 2)), blue)
    assert not near(cc.getpixel((px // 2, px - 2)), blue)
    # Grok: bottom-middle is bar, top-right and mid-left are not.
    assert near(gk.getpixel((px // 2, px - 2)), blue)
    assert not near(gk.getpixel((px - 3, 2)), blue)
    assert not near(gk.getpixel((1, px // 2)), blue)


def test_waiting_for_next_prompt_is_not_a_decision():
    from tallydeck.decisions import decision_text
    assert not decision_text("waiting for your next prompt")
    assert not decision_text("I'll wait here.\n\nwaiting for your next prompt")
    assert decision_text("Awaiting your approval to continue.")


def test_subagent_sessions_are_not_their_own_keys(tmp_path):
    root = tmp_path / "sessions" / "%2Ftmp%2Fhunt"
    parent = root / "01parent00-0000-7000-8000-000000000001"
    child = root / "01child000-0000-7000-8000-000000000002"
    parent.mkdir(parents=True)
    child.mkdir()
    (parent / "summary.json").write_text(json.dumps({
        "info": {"id": parent.name, "cwd": "/tmp/hunt"},
        "session_kind": "interactive",
        "generated_title": "Hunt",
        "last_active_at": "2099-01-01T00:00:00Z",
    }))
    (parent / "updates.jsonl").write_text(
        json.dumps({"params": {"update": {"sessionUpdate": "agent_message_chunk"}}}) + "\n")
    (parent / "subagents" / child.name).mkdir(parents=True)
    (parent / "subagents" / child.name / "meta.json").write_text(json.dumps({
        "subagent_id": child.name,
        "parent_session_id": parent.name,
        "status": "running",
        "description": "watcher",
    }))
    (child / "summary.json").write_text(json.dumps({
        "info": {"id": child.name, "cwd": "/tmp/hunt"},
        "session_kind": "subagent",
        "generated_title": "watcher",
        "last_active_at": "2099-01-01T00:00:00Z",
    }))
    (child / "updates.jsonl").write_text("{}\n")
    src = GrokSessionsSource(root=str(tmp_path / "sessions"),
                             stale=10**12, dwell=0, sync_titles=False)
    got = src.poll()
    assert [s.meta["session"] for s in got] == [parent.name]
    assert got[0].state == "working"
    assert "running" in got[0].sublabel


def test_brief_reads_a_grok_transcript(tmp_path):
    from tallydeck.brief import document
    root = tmp_path / "sessions" / "%2Ftmp%2Fhunt"
    uid = "01cccccccc-dddd-7eee-8fff-000000000000"
    sess = root / uid
    sess.mkdir(parents=True)
    (sess / "chat_history.jsonl").write_text(
        json.dumps({"type": "assistant",
                    "content": "Should I ping you about the Caspian Blue County in NM?"})
        + "\n")
    doc = document(uid, "/tmp/hunt", label="cruiser-finder",
                   grok_roots=[tmp_path / "sessions"], state="attention")
    blob = "\n".join(t for _, t in doc["sections"])
    assert "Caspian Blue County" in blob
    assert any(h.startswith("THE ASK") or h.startswith("WHERE IT LEFT OFF")
               for h, _ in doc["sections"])


def test_term_surface_marks_grok_keys():
    sigs = [Signal(id="gk/x", label="grok", state=WORKING,
                   meta={"harness": "grok"})]
    assert "▁" in render_term(NEO, View(profile=NEO).layout(sigs))
    plain = [Signal(id="cc/x", label="claude", state=WORKING)]
    assert "▁" not in render_term(NEO, View(profile=NEO).layout(plain))


def test_real_lifecycle_closes_tools_and_rearms_for_the_next_prompt():
    recs = [_rec('tool_call', toolCallId='x'), _rec('turn_completed'), _rec('session_recap')]
    assert classify(recs) == SUCCESS
    assert classify(recs + [_rec('user_message_chunk')]) == WORKING
    assert classify([_rec('agent_thought_chunk')]) == WORKING
    assert classify([_rec('tool_call', toolCallId='x'),
                     _rec('tool_call_update', toolCallId='x', status='failed'),
                     _rec('agent_message_chunk')]) == SUCCESS


def test_grok_mute_and_flash_expiry_use_full_session_identity(tmp_path, monkeypatch):
    import os, time
    from tallydeck.signal import ATTENTION
    monkeypatch.setenv('TALLYDECK_STATE', str(tmp_path/'state'))
    uid = '01234567-89ab-4cde-8fab-0123456789ab'
    folder = tmp_path/'sessions'/'%2Ftmp'/uid; folder.mkdir(parents=True)
    (folder/'summary.json').write_text(json.dumps({'info':{'id':uid,'cwd':'/tmp'},'generated_title':'Hunt'}))
    (folder/'chat_history.jsonl').write_text(json.dumps({'type':'assistant','content':'Which region should I search?'})+'\n')
    log = folder/'updates.jsonl'; log.write_text(json.dumps(_rec('turn_completed'))+'\n')
    now = time.time(); os.utime(log,(now-400,now-400))
    src = GrokSessionsSource(root=str(tmp_path/'sessions'), dwell=0, sync_titles=False)
    src._live_grok_panes = lambda: {}
    sig = src.poll()[0]
    assert sig.state == ATTENTION and sig.wants_flash is False
    ack = tmp_path/'state/acked'/uid; ack.parent.mkdir(parents=True); ack.touch()
    assert src.poll()[0].state == IDLE
    os.utime(ack,(now-1,now-1))
    os.utime(log,(now,now))
    assert src.poll()[0].state == ATTENTION
