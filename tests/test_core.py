import json
import time

import pytest

from tallydeck.signal import (Signal, rank, summarize,
                              ATTENTION, BLOCKED, WORKING, IDLE, SUCCESS)
from tallydeck.devices import NEO
from tallydeck.view import View
from tallydeck.hub import Hub
from tallydeck.sources.demo import DemoSource
from tallydeck.sources.watchdir import WatchDirSource
from tallydeck.sources.claude_sessions import ClaudeSessionsSource


# ── signal model ─────────────────────────────────────────────────────────────

def test_rank_orders_by_urgency_then_priority_then_recency():
    now = time.time()
    sigs = [
        Signal(id="a", label="a", state=WORKING, updated=now),
        Signal(id="b", label="b", state=ATTENTION, updated=now - 100),
        Signal(id="c", label="c", state=BLOCKED, updated=now - 500),
        Signal(id="d", label="d", state=ATTENTION, priority=9, updated=now - 900),
    ]
    assert [s.id for s in rank(sigs)] == ["c", "d", "b", "a"]


def test_wire_roundtrip_and_validation():
    s = Signal(id="x/y", label="y", state=BLOCKED, progress=0.5, ttl=60)
    s2 = Signal.from_dict(json.loads(s.to_json()))
    assert (s2.id, s2.state, s2.progress, s2.ttl) == ("x/y", BLOCKED, 0.5, 60)
    with pytest.raises(ValueError):
        Signal.from_dict({"id": "a", "label": "a", "state": "excited"})
    with pytest.raises(ValueError):
        Signal.from_dict({"id": "a"})
    assert Signal.from_dict(
        {"id": "a", "label": "a", "progress": 7}).progress == 1.0


def test_ttl_expiry():
    s = Signal(id="a", label="a", ttl=10, updated=time.time() - 11)
    assert s.expired()
    assert not Signal(id="b", label="b", updated=0).expired()  # no ttl → never


def test_flash_defaults_and_override():
    assert Signal(id="a", label="a", state=ATTENTION).wants_flash
    assert not Signal(id="a", label="a", state=WORKING).wants_flash
    assert Signal(id="a", label="a", state=WORKING, flash=True).wants_flash
    assert not Signal(id="a", label="a", state=BLOCKED, flash=False).wants_flash


def test_summarize():
    sigs = [Signal(id="a", label="a", state=BLOCKED),
            Signal(id="b", label="b", state=ATTENTION),
            Signal(id="c", label="c", state=WORKING)]
    assert summarize(sigs) == "1 blocked · 1 need you · 1 working"
    assert summarize([]) == "all quiet"


# ── view ─────────────────────────────────────────────────────────────────────

def test_view_pins_then_ranks_and_pages():
    sigs = [Signal(id=f"s{i}", label=f"s{i}", state=IDLE) for i in range(10)]
    sigs[7].state = ATTENTION
    v = View(profile=NEO, pinned=["s3"])
    lay = v.layout(sigs)
    assert lay.pages == 2
    assert lay.keys[0].id == "s3"          # pinned wins over rank
    assert lay.keys[1].id == "s7"          # then the attention signal
    v.page_next()
    lay2 = v.layout(sigs)
    assert lay2.page == 1
    assert sum(1 for k in lay2.keys if k) == 2
    assert len(lay2.keys) == NEO.keys      # always padded to full grid
    v.page_next()                          # clamps at last page
    assert v.layout(sigs).page == 1


def test_view_hide_idle():
    sigs = [Signal(id="a", label="a", state=IDLE),
            Signal(id="b", label="b", state=WORKING)]
    lay = View(profile=NEO, hide_idle=True).layout(sigs)
    assert [k.id for k in lay.keys if k] == ["b"]


# ── hub ──────────────────────────────────────────────────────────────────────

def test_hub_merges_and_survives_broken_source():
    class Broken:
        group = "boom"
        def poll(self):
            raise RuntimeError("nope")
        def on_press(self, sig, long=False):
            return False

    hub = Hub([DemoSource(), Broken()], log=lambda m: None)
    sigs = hub.poll()
    assert len(sigs) == 7
    assert sigs[0].state == BLOCKED        # ranked output


def test_hub_press_unknown_id_is_safe():
    hub = Hub([DemoSource()], log=lambda m: None)
    hub.poll()
    hub.press("nonexistent")               # must not raise


# ── watchdir source ──────────────────────────────────────────────────────────

def test_watchdir_lifecycle(tmp_path):
    src = WatchDirSource(path=str(tmp_path))
    assert src.poll() == []
    (tmp_path / "deploy.json").write_text(json.dumps(
        {"label": "prod deploy", "state": "blocked", "progress": 0.4}))
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / "gone.json").write_text(json.dumps(
        {"label": "old", "ttl": 1, "updated": time.time() - 999}))
    sigs = {s.id: s for s in src.poll()}
    assert sigs["sig/deploy"].state == BLOCKED
    assert sigs["sig/bad"].state == BLOCKED            # malformed = visible
    assert "sig/gone" not in sigs                      # expired = reaped
    assert not (tmp_path / "gone.json").exists()

    # short press acks; long press deletes
    src.on_press(sigs["sig/deploy"])
    assert json.loads((tmp_path / "deploy.json").read_text())["state"] == "idle"
    src.on_press(sigs["sig/deploy"], long=True)
    assert not (tmp_path / "deploy.json").exists()


# ── claude sessions source ───────────────────────────────────────────────────

def _write_jsonl(dirp, name, records):
    p = dirp / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def test_claude_sessions_states(tmp_path):
    import os
    proj = tmp_path / "-home-me-projects-widget"
    proj.mkdir()
    p = _write_jsonl(proj, "abc12345", [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "run the tests"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Tests pass. What next?"}]}},
    ])
    t = time.time() - 60                   # quiet past the dwell window
    os.utime(p, (t, t))
    src = ClaudeSessionsSource(root=str(tmp_path))
    sigs = src.poll()
    assert len(sigs) == 1
    s = sigs[0]
    assert s.state == ATTENTION            # assistant spoke last → your move
    assert s.label == "widget"
    assert "Tests pass" in s.detail
    assert s.meta["project"] == "/home/me/projects/widget"


def test_claude_sessions_dwell_masks_midturn_flap(tmp_path):
    """Tool results log as 'user' records, so mid-turn the tail flaps.
    A FRESH assistant tail is Claude still working; only a quiet one is
    truly waiting on the human."""
    proj = tmp_path / "-home-me-projects-widget"
    proj.mkdir()
    _write_jsonl(proj, "abc12345", [{"type": "assistant",
                                     "message": {"content": []}}])
    assert ClaudeSessionsSource(
        root=str(tmp_path)).poll()[0].state == WORKING
    assert ClaudeSessionsSource(
        root=str(tmp_path), dwell=0).poll()[0].state == ATTENTION


def test_claude_sessions_one_key_per_project(tmp_path):
    proj = tmp_path / "-home-me-projects-widget"
    proj.mkdir()
    old = _write_jsonl(proj, "old00000", [{"type": "user", "message":
                                          {"content": []}}])
    time.sleep(0.02)
    _write_jsonl(proj, "new00000", [{"type": "assistant", "message":
                                     {"content": []}}])
    sigs = ClaudeSessionsSource(root=str(tmp_path)).poll()
    assert len(sigs) == 1
    assert sigs[0].meta["session"] == "new00000"
    assert old.exists()                    # scanner never mutates sessions


# ── renderers stay import-light and draw without hardware ────────────────────

def test_png_render_smoke(tmp_path):
    from tallydeck.render.png import render_png
    lay = View(profile=NEO).layout(DemoSource().poll())
    img = render_png(NEO, lay, {"demo/arb": True}, scale=1)
    assert img.size[0] > 400 and img.size[1] > 250
    img.save(tmp_path / "deck.png")


def test_term_render_smoke():
    from tallydeck.render.term import render_term
    lay = View(profile=NEO).layout(DemoSource().poll())
    out = render_term(NEO, lay, {})
    assert "arb bot" in out and "page" not in out
