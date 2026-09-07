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
    assert lay.keys[4].id == "s7"          # rank #2 sits BELOW #1 (column fill)
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
    assert len(sigs) == 8
    assert sigs[0].state == BLOCKED        # ranked output


def test_demo_press_acks_rearms_and_dismisses():
    hub = Hub([DemoSource()], log=lambda m: None)
    hub.poll()
    hub.press("demo/arb")                      # short press: ack
    arb = [s for s in hub.poll() if s.id == "demo/arb"][0]
    assert arb.state == SUCCESS and "acked" in arb.sublabel
    hub.press("demo/arb")                      # press again: re-arm
    assert [s for s in hub.poll()
            if s.id == "demo/arb"][0].state == ATTENTION
    hub.press("demo/relay", long=True)         # long press: dismiss
    assert not [s for s in hub.poll() if s.id == "demo/relay"]


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


# ── tokenburn source ─────────────────────────────────────────────────────────

def _burn_fixture():
    payload = {"accounts": [
        {"id": "A", "provider": "anthropic", "enabled": True,
         "session_pct": 15, "session_reset": "2026-09-07T08:00:00+00:00"},
        {"id": "B", "provider": "anthropic", "enabled": True,
         "session_pct": 10, "session_reset": "2026-09-07T08:00:00+00:00"},
        {"id": "X", "provider": "grok", "enabled": True, "session_pct": 50},
    ]}
    targets = {
        "A": {"window_5h_limit": 9_000_000, "target_pct_5h": 40},
        "B": {"window_5h_limit": 9_000_000, "target_pct_5h": 70},
    }
    return payload, targets


def test_burn_aggregates_anthropic_accounts_only():
    from tallydeck.sources.burn import TokenBurnSource
    payload, targets = _burn_fixture()
    # Pin "now" so the countdowns are deterministic: 2h before both resets.
    import datetime
    now = datetime.datetime.fromisoformat("2026-09-07T06:00:00+00:00").timestamp()
    sigs = TokenBurnSource(tz="UTC").signals_from(payload, targets, now=now)
    assert len(sigs) == 1
    m = sigs[0].meta
    # (15% + 10%) of 9M each = 2.25M burned; (40% + 70%) of 9M = 9.9M target
    assert abs(m["frac"] - 2.25 / 9.9) < 1e-9
    assert m["meter"] is True
    assert m["left"] == "23%"
    assert m["mid"] == "A 15 · B 10"          # grok account excluded
    # `right` is a COUNTDOWN now, not the reset wall-clock. The old "→ 08:00"
    # was read as a duration and appeared frozen for hours; that was the bug.
    assert m["right"] == "A 2:00  B 2:00"
    # One lane per anthropic account, each with its own fill — grok excluded.
    assert [l["id"] for l in m["lanes"]] == ["A", "B"]
    # Fill = window pct, matching the number engraved on the lane (a 26% label
    # over a 65%-full bar read as nonsense); the target rides the bar as a notch.
    assert abs(m["lanes"][0]["frac"] - 0.15) < 1e-9
    assert abs(m["lanes"][0]["target"] - 0.40) < 1e-9
    assert abs(m["lanes"][1]["frac"] - 0.10) < 1e-9
    assert abs(m["lanes"][1]["target"] - 0.70) < 1e-9
    assert all(l["remaining_s"] == 7200 for l in m["lanes"])
    assert m["hot"] == ""            # one sample — nothing provably burning yet
    assert not sigs[0].wants_flash


def test_burn_countdown_never_goes_negative():
    """A window that has already rolled must read 0:00, not a negative time.

    The API can lag the reset by a few seconds; showing "-0:03" would be both
    alarming and meaningless.
    """
    from tallydeck.sources.burn import TokenBurnSource
    payload, targets = _burn_fixture()
    import datetime
    past = datetime.datetime.fromisoformat("2026-09-07T09:00:00+00:00").timestamp()
    m = TokenBurnSource(tz="UTC").signals_from(payload, targets, now=past)[0].meta
    assert m["right"] == "A 0:00  B 0:00"
    assert all(l["remaining_s"] == 0 for l in m["lanes"])


def test_burn_hot_is_the_account_burning_now():
    """`hot` (the underlined lane) names the account whose window-% is
    actually growing — the one currently burning — not the soonest reset."""
    from tallydeck.sources.burn import TokenBurnSource
    src = TokenBurnSource(tz="UTC")
    payload, targets = _burn_fixture()
    src.signals_from(payload, targets, now=1000.0)
    payload["accounts"][1]["session_pct"] = 14        # B grew 10 → 14
    m = src.signals_from(payload, targets, now=1060.0)[0].meta
    assert m["hot"] == "B"
    payload["accounts"][0]["session_pct"] = 40        # now A grows faster
    m = src.signals_from(payload, targets, now=1120.0)[0].meta
    assert m["hot"] == "A"


def test_burn_over_target_and_empty():
    from tallydeck.sources.burn import TokenBurnSource
    payload, targets = _burn_fixture()
    payload["accounts"][0]["session_pct"] = 80
    payload["accounts"][1]["session_pct"] = 60
    sigs = TokenBurnSource(tz="UTC").signals_from(payload, targets)
    assert sigs[0].meta["frac"] > 1.0          # renderer goes molten
    assert sigs[0].progress == 1.0             # wire progress stays clamped
    assert TokenBurnSource().signals_from({"accounts": []}, {}) == []


def test_view_routes_meter_to_info_bar_not_keys():
    sigs = [Signal(id="a", label="a", state=WORKING),
            Signal(id="burn/session", label="burn", state=WORKING,
                   meta={"meter": True, "frac": 0.5})]
    lay = View(profile=NEO).layout(sigs)
    assert lay.meter is not None and lay.meter.id == "burn/session"
    assert all(k is None or k.id != "burn/session" for k in lay.keys)
    assert "working" in lay.summary            # summary counts real keys only


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


def test_config_layers_repo_then_user(tmp_path, monkeypatch):
    """The tracked repo config loads underneath the user's, and the user wins.

    Two layers exist because the hub and the deck machine need different
    settings while sharing one repo — and because config that must be
    hand-edited per machine drifts away from the code that reads it.
    """
    from tallydeck import config
    user = tmp_path / "user.toml"
    user.write_text('[view]\ndevice = "xl"\n')
    cfg = config.load(user)
    assert cfg["view"]["device"] == "xl"          # user overrides repo
    assert cfg["client"]["on_press"]              # repo layer still applied
    assert cfg["client"]["connect"][0] == "ssh"


def test_repo_config_carries_no_secrets():
    """The tracked layer must never grow a credential.

    It is in git. Private today is not private forever, and the habit is what
    protects you, not the visibility flag.

    Deliberately NOT a substring grep: the first version flagged
    `kind = "tokenburn"` because the word "token" appears in a source name.
    A check that fires on legitimate values is a check that gets deleted. This
    one looks at KEY NAMES that promise a credential, and at VALUES that look
    like one.
    """
    import re
    import tomllib
    from tallydeck import config
    if not config.REPO_PATH.is_file():
        return
    with open(config.REPO_PATH, "rb") as fh:
        data = tomllib.load(fh)

    key_pat = re.compile(
        r"(?:^|_)(token|secret|password|passwd|api_?key|private_?key|"
        r"credential|auth)s?(?:$|_)", re.I)
    # sk-…, ghp_…, PEM blocks, or a long unbroken high-entropy blob.
    val_pat = re.compile(
        r"(^(sk|ghp|gho|xox[bp]|AKIA)[-_])|(-----BEGIN)|([A-Za-z0-9+/=]{40,})")

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                assert not key_pat.search(k), f"credential-shaped key {path}{k!r}"
                walk(v, f"{path}{k}.")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}].")
        elif isinstance(node, str):
            assert not val_pat.search(node), f"credential-shaped value at {path}"

    walk(data)


# ── real cwd, burn-rate ranking, column fill ─────────────────────────────────

def test_claude_sessions_uses_real_cwd_from_records(tmp_path):
    proj = tmp_path / "-home-me-projects-my-web-app"
    proj.mkdir()
    _write_jsonl(proj, "abc12345", [
        {"type": "user", "cwd": "/home/me/projects/my-web-app",
         "message": {"content": []}}])
    s = ClaudeSessionsSource(root=str(tmp_path)).poll()[0]
    assert s.meta["project"] == "/home/me/projects/my-web-app"
    assert s.label == "my-web-app"    # full basename; renderer wraps


def test_resolve_munged_checks_existence(tmp_path, monkeypatch):
    from tallydeck.sources.claude_sessions import _resolve_munged
    real = tmp_path / "projects" / "my-web-app"
    real.mkdir(parents=True)
    munged = "-" + str(tmp_path).strip("/").replace("/", "-") \
             + "-projects-my-web-app"
    assert _resolve_munged(munged) == str(real)


def test_burn_rate_feeds_priority(tmp_path):
    src = ClaudeSessionsSource(root=str(tmp_path))
    assert src._burn_rate("s1", 100.0, 1000) == 0.0      # first sample
    assert src._burn_rate("s1", 110.0, 51000) == 5000.0  # 50k over 10s
    assert src._burn_rate("cold", 110.0, 999) == 0.0


def test_view_column_first_fill():
    sigs = [Signal(id=f"s{i}", label=f"s{i}", state=WORKING, priority=10 - i)
            for i in range(5)]
    lay = View(profile=NEO).layout(sigs)            # fill="columns" default
    # NEO is 2 rows x 4 cols; rank order walks columns: (0,0),(1,0),(0,1)...
    assert lay.keys[0].id == "s0"                   # row 0, col 0
    assert lay.keys[4].id == "s1"                   # row 1, col 0
    assert lay.keys[1].id == "s2"                   # row 0, col 1
    assert lay.keys[5].id == "s3"                   # row 1, col 1
    assert lay.keys[2].id == "s4"
    rows_lay = View(profile=NEO, fill="rows").layout(sigs)
    assert [k.id for k in rows_lay.keys[:5] if k] == \
        ["s0", "s1", "s2", "s3", "s4"]


def test_burn_lane_fill_matches_window_pct():
    from tallydeck.sources.burn import TokenBurnSource
    payload, targets = _burn_fixture()
    lanes = TokenBurnSource(tz="UTC").signals_from(
        payload, targets)[0].meta["lanes"]
    a = [l for l in lanes if l["id"] == "A"][0]
    assert a["frac"] == 0.15         # 15% of the WINDOW — matches the label
    assert a["target"] == 0.40       # target rides the bar as a notch
