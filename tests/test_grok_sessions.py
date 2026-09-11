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


def test_term_surface_marks_grok_keys():
    sigs = [Signal(id="gk/x", label="grok", state=WORKING,
                   meta={"harness": "grok"})]
    assert "▁" in render_term(NEO, View(profile=NEO).layout(sigs))
    plain = [Signal(id="cc/x", label="claude", state=WORKING)]
    assert "▁" not in render_term(NEO, View(profile=NEO).layout(plain))
