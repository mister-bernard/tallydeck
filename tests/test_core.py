import json
import os
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
from tallydeck.render import theme


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

    # short press is hands-off (the client shows it); long press deletes
    assert src.on_press(sigs["sig/deploy"]) is False
    assert json.loads((tmp_path / "deploy.json").read_text())["state"] == "blocked"
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
    # quiet, ended, nothing asked → finished, not "your move"
    assert ClaudeSessionsSource(
        root=str(tmp_path), dwell=0).poll()[0].state == SUCCESS


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


def test_pane_matching_uses_socket_and_prefixes(monkeypatch):
    """Sessions live on a NAMED socket; a bare `tmux list-panes` queries the
    default one and finds nothing — every key then fell back to a bare shell.
    And a pane in a parent dir is still that session's window."""
    src = ClaudeSessionsSource(socket="/tmp/tmux-1000/cc")
    assert src._tmux("list-panes")[:3] == ["tmux", "-S", "/tmp/tmux-1000/cc"]
    src._pane_cache = {"/home/me/projects": "main:1.1",
                       "/home/me/projects/widget": "work:2.0"}
    src._pane_ts = time.time()
    assert src._pane_for("/home/me/projects/widget") == "work:2.0"   # exact
    assert src._pane_for("/home/me/projects/widget/sub") == "work:2.0"  # deepest
    assert src._pane_for("/home/me/other") == ""


def test_brief_reports_session_and_repo(tmp_path):
    from tallydeck.brief import build
    proj = tmp_path / "-home-me-projects-widget"
    proj.mkdir()
    _write_jsonl(proj, "sess1234", [
        {"type": "assistant", "cwd": str(tmp_path), "message": {"content": [
            {"type": "text", "text": "Ran the migration; next step is to "
             "verify the row counts against staging before deploying."}]}}])
    out = build("sess1234", str(tmp_path), "widget", roots=[tmp_path],
                state="attention")
    assert "widget" in out and "ATTENTION" in out   # styled header
    assert "verify the row counts" in out
    assert "WHERE IT LEFT OFF" in out


# ── marbled backgrounds ──────────────────────────────────────────────────────

def test_marble_deterministic_and_distinct():
    from tallydeck.render.marble import swirl
    a1 = swirl((48, 48), "cc/abc", "working", (62, 155, 255))
    a2 = swirl((48, 48), "cc/abc", "working", (62, 155, 255))
    b = swirl((48, 48), "cc/other", "working", (62, 155, 255))
    assert a1.tobytes() == a2.tobytes()      # same card → same swirl, always
    assert a1.tobytes() != b.tobytes()       # different card → different swirl


def test_marble_luminance_ceiling_holds_across_heat():
    from tallydeck.render.marble import card_bg
    def worst_contrast(img):
        def lin(c):
            c /= 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        w = 1e9
        for r, g, b in img.getdata():
            L = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
            w = min(w, 1.05 / (L + 0.05))
        return w
    cold = card_bg((64, 64), "cc/x", "working", (229, 72, 77), 0.0)
    hot = card_bg((64, 64), "cc/x", "working", (229, 72, 77), 1.0)
    assert worst_contrast(cold) > 7.0        # cold: white text comfortably AAA
    assert worst_contrast(hot) > 4.5         # hot: brighter, still AA for white
    # and hot really is brighter — the whole point (G: "background images
    # should be more or less bright according to how hot the sessions are")
    mean = lambda im: sum(sum(p) for p in im.getdata()) / (3 * 64 * 64)
    assert mean(hot) > mean(cold) * 1.6


def test_marble_cache_reuses_by_heat_bucket():
    from tallydeck.render import marble
    a = marble.card_bg((32, 32), "cc/c", "idle", (86, 90, 100), 0.50)
    b = marble.card_bg((32, 32), "cc/c", "idle", (86, 90, 100), 0.52)
    assert a is b                            # same bucket → same cached image


def test_tool_use_tail_is_working_not_attention(tmp_path):
    """A quiet assistant tail that ends in tool_use = a tool still running
    (long test suite, bake) — NOT 'your move'. Seen in practice: an autonomous
    worker flashing attention while running its fork suite."""
    import os
    proj = tmp_path / "-home-me-projects-widget"
    proj.mkdir()
    p = _write_jsonl(proj, "abc12345", [
        {"type": "assistant", "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "Running the fork suite."},
                        {"type": "tool_use", "name": "Bash", "input": {}}]}}])
    t = time.time() - 120                     # well past dwell
    os.utime(p, (t, t))
    assert ClaudeSessionsSource(root=str(tmp_path)).poll()[0].state == WORKING
    # but a genuinely ended turn stays attention
    p2 = _write_jsonl(proj, "def67890", [
        {"type": "assistant", "message": {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Done. What next?"}]}}])
    os.utime(p2, (t, t))
    states = {s.meta["session"]: s.state
              for s in ClaudeSessionsSource(root=str(tmp_path), dwell=0).poll()}
    # one key per project keeps the newest; check directly instead
    from tallydeck.sources.claude_sessions import _assistant_wants_input, _tail_lines
    assert _assistant_wants_input(_tail_lines(p)) is False
    assert _assistant_wants_input(_tail_lines(p2)) is True


def test_guessed_pane_never_routes(tmp_path, monkeypatch):
    """Directory matching sent every cwd=/home session to whichever pane sat
    there — that is how every key opened the same pane. Only exact identity
    routes."""
    proj = tmp_path / "-home-me"
    proj.mkdir()
    _write_jsonl(proj, "sess0001", [
        {"type": "user", "cwd": "/home/me", "message": {"content": []}}])
    src = ClaudeSessionsSource(root=str(tmp_path))
    monkeypatch.setattr(src, "_panes",
                        lambda: {"/home/me": "other:1.1"})   # the trap
    monkeypatch.setattr(src, "_session_panes", lambda: {})        # no identity
    assert src.poll()[0].meta["tmux"] == ""


def test_exact_pane_is_sticky_until_the_pane_dies(monkeypatch):
    """The env scan only sees a session while a tool subprocess lives; the
    mapping must survive the gaps between tool calls."""
    src = ClaudeSessionsSource()
    monkeypatch.setattr(src, "_session_panes", lambda: {"s1": "work:2.0"})
    # validation is against ALL live pane targets — the cwd-keyed map
    # collapses shared-directory panes and demoted live sessions (audit P0)
    monkeypatch.setattr(src, "_all_pane_targets",
                        lambda: {"work:2.0", "other:1.1"})
    assert src._exact_pane("s1") == "work:2.0"
    monkeypatch.setattr(src, "_session_panes", lambda: {})   # tool ended
    assert src._exact_pane("s1") == "work:2.0"               # still known
    monkeypatch.setattr(src, "_all_pane_targets", lambda: set())  # pane gone
    assert src._exact_pane("s1") == ""


def test_session_backed_signal_does_not_ack_on_short_press(tmp_path):
    """Pressing a hook-raised ask must ROUTE, not silently clear the flash —
    the operator pressed two flashing keys, saw nothing, and the alarms
    vanished."""
    src = WatchDirSource(path=str(tmp_path))
    (tmp_path / "ask-abc.json").write_text(json.dumps(
        {"label": "batch job", "state": "blocked",
         "meta": {"session": "abc-uuid", "project": "/x"}}))
    sig = src.poll()[0]
    assert src.on_press(sig) is False                  # short: untouched
    assert json.loads((tmp_path / "ask-abc.json").read_text())["state"] == "blocked"
    assert src.on_press(sig, long=True) is True        # long: explicit dismiss
    assert not (tmp_path / "ask-abc.json").exists()


def test_oneshots_never_shout_and_sink(tmp_path, monkeypatch):
    proj = tmp_path / "-home-me-projects-x"
    proj.mkdir()
    _write_jsonl(proj, "aaaa1111", [{"type": "assistant", "message": {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "One-shot answer done."}]}}])
    import os as _os
    t = time.time() - 60
    _os.utime(proj / "aaaa1111.jsonl", (t, t))
    src = ClaudeSessionsSource(root=str(tmp_path))
    monkeypatch.setattr(src, "_session_panes", lambda: {"aaaa1111": "oneshot:1.3"})
    s = src.poll()[0]
    assert s.state == SUCCESS          # an ended one-shot never flashes
    assert s.wants_flash is False
    assert s.priority < 0              # ranks below every persistent session
    assert s.meta["oneshot"] is True


def test_beacon_jump_to_finds_the_alert_page():
    sigs = [Signal(id=f"s{i}", label=f"s{i}", state=WORKING, priority=100 - i)
            for i in range(12)]
    sigs[10].state = IDLE                       # sinks to the tail
    v = View(profile=NEO)
    v.layout(sigs)
    v.jump_to(sigs[10].id)
    assert v.page == 1                          # the alert's page, not page 0
    v.jump_to("s0")
    assert v.page == 0


def test_space_done_mutes_until_the_session_asks_again(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path / "state"))
    proj = tmp_path / "-home-me-projects-w"
    proj.mkdir()
    p = _write_jsonl(proj, "feedbeef", [{"type": "assistant", "message": {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Decision needed on the rollout."}]}}])
    t = time.time() - 120
    os.utime(p, (t, t))
    ack = tmp_path / "state" / "acked" / "feedbeef"
    ack.parent.mkdir(parents=True, exist_ok=True)
    ack.touch()                                     # space pressed
    src = ClaudeSessionsSource(root=str(tmp_path))
    assert src.poll()[0].state == IDLE              # muted
    os.utime(p, None)                               # session asks anew (log moves)
    assert ClaudeSessionsSource(
        root=str(tmp_path), dwell=0).poll()[0].state == ATTENTION
    assert not ack.exists()                         # ack retired itself


def test_multiwindow_sessions_label_by_window_topic(tmp_path, monkeypatch):
    """main/mainB hold many windows on DIFFERENT topics; the window name is
    the topic, so it must name the tile — not the shared session name.

    The window has to hold ONE pane for its name to be about one session:
    main-O:1 is five Codex sessions sharing a window, and naming all five
    after the window is the bug this rule guards (titles.resolve_label)."""
    from tallydeck.titles import PaneInfo
    proj = tmp_path / "-home-me"
    proj.mkdir()
    _write_jsonl(proj, "cafe0001", [
        {"type": "user", "cwd": "/home/me", "message": {"content": []}}])
    src = ClaudeSessionsSource(root=str(tmp_path), sync_titles=False)
    monkeypatch.setattr(src, "_session_panes", lambda: {"cafe0001": "mainB:3.1"})
    monkeypatch.setattr(src, "_all_pane_targets", lambda: {"mainB:3.1"})

    def window(name, panes=1):
        info = PaneInfo("mainB:3.1", "%1", "mainB", name, "3", panes, "", "")
        monkeypatch.setattr(src.tmux, "info", lambda t, i=info: i)

    window("stonks-research")
    assert src.poll()[0].label == "stonks-research"
    window("claude")                                  # auto-name → session
    assert src.poll()[0].label == "mainB"
    window("stonks-research", panes=5)                # shared → session
    assert src.poll()[0].label == "mainB"


# ── state categorization: finished is green, asking is amber ─────────────────

def _quiet(p, age=120):
    import os
    t = time.time() - age
    os.utime(p, (t, t))


def test_bookkeeping_tail_does_not_hide_the_conversation(tmp_path):
    """Current Claude Code ends most logs with attachment / system /
    last-prompt / cost-state records. 15 of 18 live sessions read IDLE
    because the classifier only knew 'user' and 'assistant' tails."""
    proj = tmp_path / "-home-me-projects-w"
    proj.mkdir()
    p = _write_jsonl(proj, "aaaa0001", [
        {"type": "assistant", "message": {"stop_reason": "end_turn",
         "content": [{"type": "text", "text": "Shipped the fix and pushed."}]}},
        {"type": "attachment", "attachment": {"type": "x"}},
        {"type": "system", "subtype": "stop_hook_summary"},
        {"type": "system", "subtype": "turn_duration"},
        {"type": "last-prompt", "lastPrompt": "fix it"},
        {"type": "cost-state", "totalCostUSD": 1.0},
    ])
    # turn_duration is Claude Code's own end-of-turn marker: final even
    # while the log is fresh — no dwell needed.
    s = ClaudeSessionsSource(root=str(tmp_path)).poll()[0]
    assert s.state == SUCCESS
    assert s.sublabel.startswith("done")
    assert s.wants_flash is False

    # a tool still running, buried under bookkeeping, is WORKING
    p2 = _write_jsonl(proj, "aaaa0002", [
        {"type": "assistant", "message": {"stop_reason": "tool_use",
         "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "attachment"}, {"type": "last-prompt"}, {"type": "cost-state"},
    ])
    _quiet(p2)
    # and a human prompt under bookkeeping is WORKING too
    p3 = _write_jsonl(proj, "aaaa0003", [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "mode", "mode": "normal"}, {"type": "atis-latch"},
    ])
    _quiet(p3)
    from tallydeck.sources.claude_sessions import classify, _tail_lines
    assert classify(_tail_lines(p2)) == (WORKING, False)
    assert classify(_tail_lines(p3)) == (WORKING, False)


def test_finished_is_success_asking_is_attention(tmp_path):
    proj = tmp_path / "-home-me-projects-w"
    proj.mkdir()
    done = _write_jsonl(proj, "bbbb0001", [
        {"type": "assistant", "message": {"stop_reason": "end_turn",
         "content": [{"type": "text", "text":
                      "All 42 tests pass. Committed as 1a2b3c and pushed."}]}}])
    _quiet(done)
    from tallydeck.sources.claude_sessions import classify, _tail_lines
    assert classify(_tail_lines(done)) == (SUCCESS, False)
    ask = _write_jsonl(proj, "bbbb0002", [
        {"type": "assistant", "message": {"stop_reason": "end_turn",
         "content": [{"type": "text", "text":
                      "Two options: keep A or switch to B.\n\n"
                      "Which do you want?"}]}}])
    _quiet(ask)
    assert classify(_tail_lines(ask)) == (ATTENTION, False)
    src = ClaudeSessionsSource(root=str(tmp_path))
    by = {s.meta["session"]: s for s in src.poll()}
    assert by["bbbb0002"].state == ATTENTION
    assert "Which do you want" in by["bbbb0002"].sublabel   # the ask on the key
    assert by["bbbb0002"].wants_flash is True


def test_asks_question_heuristics():
    from tallydeck.sources.claude_sessions import asks_question as q
    assert q("Done. Which branch should this land on?")
    assert q("I need your sign-off before pushing to main.")
    assert q("Your call: keep the old key or rotate it.")
    assert q("Ready. Should I push?")
    # reports are reports — even when they mention questions or hedge
    assert not q("Fixed the bug (was the `?` in the regex). Tests green.")
    assert not q("Open question: is the cache safe?\n\nEither way, shipped "
                 "the fix and it is live now.")
    assert not q("Let me know if you want the same treatment elsewhere.")
    assert not q("```\nwhat?\n```\nAll done.")
    assert not q("")


def test_summarize_counts_finished():
    assert summarize([Signal(id="a", label="a", state=SUCCESS),
                      Signal(id="b", label="b", state=WORKING)]) \
        == "1 working · 1 done"


def test_raised_flag_short_press_never_acks_silently(tmp_path):
    """The Aurora key: flashing, pressed, 'acked' — and the operator never
    saw what it asked. Reading is the client's job; only an explicit
    done/long-press/clear may retire it."""
    src = WatchDirSource(path=str(tmp_path))
    (tmp_path / "aurora.json").write_text(json.dumps(
        {"label": "Aurora", "state": "attention",
         "sublabel": "cathode vs anode sphere?"}))
    sig = src.poll()[0]
    assert src.on_press(sig) is False
    d = json.loads((tmp_path / "aurora.json").read_text())
    assert d["state"] == "attention" and d["sublabel"] != "acked"
    assert src.on_press(sig, long=True) is True
    assert not (tmp_path / "aurora.json").exists()


def test_raise_stamps_its_own_pane_not_the_one_the_operator_is_watching(tmp_path, monkeypatch):
    """A raise from a background window must address ITSELF. `tmux display -p`
    without `-t` answers for the current pane of the attached client, so a
    one-shot worker's question stamped the operator's foreground pane and his
    answer was pasted into an unrelated agent's transcript (c64-dxm-source →
    mainA:1.4, 2026-09-08). No pane of our own → stamp nothing at all."""
    import subprocess
    from tallydeck import cli
    sock = str(tmp_path / "panes.sock")
    T = lambda *a: subprocess.run(["tmux", "-S", sock, *a], capture_output=True, text=True, timeout=10)
    T("-f", "/dev/null", "new-session", "-d", "-s", "watched", "-n", "front", "sleep 30")
    T("new-window", "-d", "-t", "watched", "-n", "worker", "sleep 30")
    T("select-window", "-t", "watched:0")                    # what the operator is on
    mine = [ln.split()[0] for ln in T("list-panes", "-a", "-F", "#{pane_id} #W").stdout.splitlines()
            if ln.endswith(" worker")][0]
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    monkeypatch.setenv("TMUX", f"{sock},0,0")
    monkeypatch.setenv("TMUX_PANE", mine)
    monkeypatch.chdir(tmp_path)
    try:
        cli.main(["raise", "worker-q", "--state", "attention", "--sublabel", "which capture?"])
        assert json.loads((tmp_path / "signals" / "worker-q.json").read_text())["meta"]["raiser_pane"] \
            == "watched:1.0"                                  # its own window, not watched:0.0
        monkeypatch.delenv("TMUX_PANE")
        cli.main(["raise", "paneless-q", "--state", "attention", "--sublabel", "and now?"])
        paneless = json.loads((tmp_path / "signals" / "paneless-q.json").read_text())
        assert "raiser_pane" not in (paneless.get("meta") or {})
    finally:
        T("kill-server")


def test_raise_stamps_the_raising_session(tmp_path, monkeypatch):
    """`tally raise` from inside a Claude session must produce a key that
    routes back INTO that session, with the ask — not an orphan flag."""
    from tallydeck import cli
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "deadbeef-0000-4000-8000-000000000000")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/x/.claude-b")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.chdir(tmp_path)
    cli.main(["raise", "aurora", "--state", "attention", "--label", "Aurora",
              "--sublabel", "cathode vs anode sphere?"])
    d = json.loads((tmp_path / "signals" / "aurora.json").read_text())
    assert d["detail"] == "cathode vs anode sphere?"   # answerable hub-side
    assert d["meta"]["session"].startswith("deadbeef")
    assert d["meta"]["account"] == "B"
    assert d["meta"]["project"] == str(tmp_path)
    sig = WatchDirSource(path=str(tmp_path / "signals")).poll()[0]
    assert sig.meta["session"].startswith("deadbeef")
    cli.main(["clear", "aurora"])
    assert not (tmp_path / "signals" / "aurora.json").exists()


def test_notification_hook_raises_only_for_real_asks(tmp_path):
    """idle_prompt = 'waiting for your input' = the turn merely ended. It
    was raised as BLOCKED, so every finished session went red a minute
    after it stopped talking."""
    import subprocess, sys, os
    from pathlib import Path
    hook = Path(__file__).resolve().parent.parent / "contrib" / "tally-hook-notify"
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TMUX="")
    env.pop("TMUX")

    def fire(kind, sid, msg):
        subprocess.run([sys.executable, str(hook)], input=json.dumps(
            {"session_id": sid, "cwd": "/tmp/p", "notification_type": kind,
             "message": msg}), text=True, env=env, check=True, timeout=10)
    fire("idle_prompt", "11111111-a", "Claude is waiting for your input")
    assert not list((tmp_path / "signals").glob("*.json")) \
        if (tmp_path / "signals").exists() else True
    fire("permission_prompt", "22222222-b", "Claude needs your permission to use Bash")
    d = json.loads((tmp_path / "signals" / "ask-22222222.json").read_text())
    assert d["state"] == "blocked" and d["priority"] == 900
    fire("elicitation_dialog", "33333333-c", "The server wants a value")
    d = json.loads((tmp_path / "signals" / "ask-33333333.json").read_text())
    assert d["state"] == "attention"
    fire("agent_completed", "44444444-d", "Agent finished")
    assert not (tmp_path / "signals" / "ask-44444444.json").exists()
    # older Claude Code without notification_type: classify by message
    subprocess.run([sys.executable, str(hook)], input=json.dumps(
        {"session_id": "55555555-e", "cwd": "/tmp/p",
         "message": "Claude is waiting for your input"}),
        text=True, env=env, check=True, timeout=10)
    assert not (tmp_path / "signals" / "ask-55555555.json").exists()
    # and clear removes it from the same dir
    clear = hook.parent / "tally-hook-clear"
    subprocess.run([sys.executable, str(clear)], input=json.dumps(
        {"session_id": "22222222-b"}), text=True, env=env, check=True, timeout=10)
    assert not (tmp_path / "signals" / "ask-22222222.json").exists()


def test_brief_shows_the_ask_without_a_log(tmp_path):
    from tallydeck.brief import build
    out = build("", "", label="Aurora", roots=[tmp_path], state="attention",
                ask="cathode vs anode sphere?")
    assert "THE ASK" in out and "cathode vs anode sphere?" in out


def test_press_script_gets_the_decks_own_ssh_target():
    """The Mac press script guessed the hub alias; on a machine where that
    alias resolved to the wrong user it died with 'Permission denied' while
    the deck was connected fine. Derive it from what the deck uses."""
    from tallydeck.cli import ssh_host
    assert ssh_host(["ssh", "claw", "~/.local/bin/tallyd"]) == "claw"
    assert ssh_host(["ssh", "-o", "BatchMode=yes", "-p", "2222",
                     "openclaw@hub.example", "tallyd"]) == "openclaw@hub.example"
    assert ssh_host(["/usr/bin/ssh", "-tt", "me@box"]) == "me@box"
    assert ssh_host(["tallyd"]) == ""
    assert ssh_host(None) == ""


# ── the quiet-deck mural ─────────────────────────────────────────────────────

def test_mural_only_when_nothing_is_live(monkeypatch):
    v = View(profile=NEO)
    assert v.layout([]).mural is True
    assert v.layout([Signal(id="a", label="a", state=IDLE),
                     Signal(id="b", label="b", state=SUCCESS)]).mural is True
    for live in (WORKING, ATTENTION, BLOCKED):
        assert v.layout([Signal(id="a", label="a", state=live)]).mural is False
    # a press on the mural peeks at the plain grid, then it returns
    v.peek(seconds=60)
    assert v.layout([]).mural is False
    v.peek_until = 0.0
    assert v.layout([]).mural is True
    assert View(profile=NEO, mural=False).layout([]).mural is False


def test_mural_tiles_cover_every_key_and_render_everywhere(tmp_path):
    from tallydeck.render import mural
    from tallydeck.render.png import render_png
    from tallydeck.render.term import render_term
    tiles = mural.tiles(NEO, t=0)
    assert len(tiles) == NEO.keys
    assert all(t.size == (NEO.key_px, NEO.key_px) for t in tiles)
    # it is a picture, not eight dark keys: every tile has ink on it
    assert all(t.convert("L").getextrema()[1] > 60 for t in tiles)
    art = mural.text(NEO)
    assert "> all quiet_" in art and "@" in art
    assert "> all quiet " in mural.text(NEO, t=1)     # the cursor blinks
    lay = View(profile=NEO).layout([])
    assert lay.mural
    img = render_png(NEO, lay, scale=1, t=0)
    assert img.size[0] > 0
    assert "all quiet" in render_term(NEO, lay)


# ── press feedback: fireworks, and steady once the popup is up ───────────────

def test_fireworks_overlay_keeps_size_and_changes_the_face():
    from tallydeck.render.fx import fireworks
    from tallydeck.render.keycard import draw_key
    sig = Signal(id="sig/x", label="Aurora", state=ATTENTION)
    face = draw_key(sig, 96)
    burst = fireworks(face, 0.15, "#FFB224", seed="sig/x")
    assert burst.size == face.size
    assert list(burst.getdata()) != list(face.getdata())
    late = fireworks(face, 0.98, "#FFB224", seed="sig/x")
    assert late.size == face.size


def test_hub_marks_a_press_whose_popup_is_up_as_opened(tmp_path):
    """A decide popup blocks its command until answered: a press action
    still alive after a beat means the popup is UP — the key stops
    shouting (flash=False, meta.opened). A quick exit keeps it flashing."""
    import sys, time as _t
    from tallydeck import hub as hubmod
    from tallydeck.sources.base import Source

    class Acts(Source):          # source-defined actions, no real popups
        group = "sig"
        def poll(self):
            return [Signal(id="sig/slow", label="slow", state=ATTENTION, detail="q?", group="sig",
                           action={"type": "cmd", "argv": [sys.executable, "-c", "import time; time.sleep(5)"]}),
                    Signal(id="sig/fast", label="fast", state=ATTENTION, detail="q?", group="sig",
                           action={"type": "cmd", "argv": [sys.executable, "-c", "pass"]})]
    h = Hub([Acts()], log=lambda m: None)
    h.poll()
    h.press("sig/slow"); h.press("sig/fast")
    _t.sleep(hubmod.OPEN_AFTER + 0.4)
    by = {s.id: s for s in h.poll()}
    assert by["sig/slow"].meta.get("opened") is True
    assert by["sig/slow"].wants_flash is False
    assert not by["sig/fast"].meta.get("opened")
    assert by["sig/fast"].wants_flash is True
    for proc, _ in list(h._inflight.values()):
        proc.kill()


def test_raise_carries_markdown_and_options(tmp_path, monkeypatch):
    from tallydeck import cli
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    md = tmp_path / "ask.md"
    md.write_text("## Pick one\n\n| a | b |\n|:--|:--|\n| 1 | 2 |\n")
    cli.main(["raise", "pick", "--state", "attention", "--label", "Pick",
              "--sublabel", "Which?", "--markdown", str(md),
              "--options", "A · first | B · second"])
    d = json.loads((tmp_path / "signals" / "pick.json").read_text())
    assert d["meta"]["markdown"].startswith("## Pick one")
    assert d["detail"]                      # the hub keys its action off this
    assert WatchDirSource(path=str(tmp_path / "signals")).poll()[0].action
    assert d["meta"]["options"] == ["A · first", "B · second"]
    sig = WatchDirSource(path=str(tmp_path / "signals")).poll()[0]
    assert sig.meta["options"][1] == "B · second"


def test_raise_update_keeps_label_and_never_routes_to_the_raisers_pane(tmp_path, monkeypatch):
    """`tally raise <id> --markdown …` on an existing flag must keep its label,
    and a raised QUESTION must not carry the raising session's tmux pane —
    that pane target made a press attach the operator to the agent's
    transcript instead of asking (G, 2026-09-08)."""
    from tallydeck import cli
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "cafe0000-0000-4000-8000-000000000000")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/cc,1,2")
    cli.main(["raise", "q", "--state", "attention", "--label", "Big Q",
              "--sublabel", "which?"])
    cli.main(["raise", "q", "--options", "A|B"])
    d = json.loads((tmp_path / "signals" / "q.json").read_text())
    assert d["label"] == "Big Q"
    assert "tmux" not in d["meta"]                # never a routing target
    assert d["meta"]["session"].startswith("cafe")
    cli.main(["raise", "q", "--tmux", "work:1.1"])
    d = json.loads((tmp_path / "signals" / "q.json").read_text())
    assert d["meta"]["tmux"] == "work:1.1"        # explicit opt-in still works


def test_wait_returns_the_answer_and_exits_when_the_flag_vanishes(tmp_path, monkeypatch, capsys):
    import pytest as _pt, threading, time as _t
    from tallydeck import cli
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    (tmp_path / "signals").mkdir(); (tmp_path / "answers").mkdir()
    (tmp_path / "signals" / "q.json").write_text(json.dumps({"label": "q", "state": "attention"}))
    def answer_later():
        _t.sleep(0.3)
        (tmp_path / "answers" / "q.json").write_text(json.dumps({"id": "q", "answer": "1 — A"}))
    threading.Thread(target=answer_later).start()
    cli.main(["wait", "q", "--every", "0.1", "--consume"])
    assert capsys.readouterr().out.strip() == "1 — A"
    assert not (tmp_path / "answers" / "q.json").exists()
    (tmp_path / "signals" / "q.json").unlink()
    with _pt.raises(SystemExit) as e:
        cli.main(["wait", "q", "--every", "0.1"])
    assert e.value.code == 2
    (tmp_path / "signals" / "q.json").write_text("{}")
    with _pt.raises(SystemExit) as e:
        cli.main(["wait", "q", "--every", "0.1", "--timeout", "0.3"])
    assert e.value.code == 1


def test_hub_answer_is_validated_against_a_raised_question(tmp_path, monkeypatch):
    """The deck machine may only ANSWER a question the hub raised: a bare
    digit must index the option list, session asks and unknown ids are
    refused, and the answer lands in the same answers/<id>.json every
    other surface writes."""
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    sig_dir = tmp_path / "signals"; sig_dir.mkdir()
    (sig_dir / "q.json").write_text(json.dumps(
        {"label": "Q", "state": "attention", "detail": "which?",
         "meta": {"options": ["A · one", "B · two"]}}))
    (sig_dir / "ask-abcd1234.json").write_text(json.dumps(
        {"label": "live", "state": "blocked", "detail": "prompt",
         "meta": {"session": "abcd1234-x"}}))
    h = Hub([WatchDirSource(path=str(sig_dir))], log=lambda m: None)
    monkeypatch.setattr(Hub, "_decide_bin", staticmethod(lambda: ""))
    h.poll()
    assert h.answer("sig/q", "9") is False               # no such option
    assert h.answer("sig/ask-abcd1234", "1") is False    # a live session's prompt
    assert h.answer("sig/nope", "1") is False
    assert h.answer("sig/q", "2") is True
    d = json.loads((tmp_path / "answers" / "q.json").read_text())
    assert d["answer"] == "2 — B · two" and d["via"] == "deck-notification"
    # first answer wins; a second is refused, not merged (audit P1-3)
    assert h.answer("sig/q", "  go with   one ") is False
    assert json.loads((tmp_path / "answers" / "q.json").read_text())["answer"] == "2 — B · two"
    (tmp_path / "answers" / "q.json").unlink()
    assert h.answer("sig/q", "  go with   one ") is True
    assert json.loads((tmp_path / "answers" / "q.json").read_text())["answer"] == "go with one"


def test_notifier_is_inert_without_terminal_notifier(monkeypatch):
    from tallydeck.client import Notifier
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    n = Notifier(link=None)
    assert n.active is False
    n.offer([Signal(id="sig/q", label="Q", state=ATTENTION, detail="?")])   # no crash


def test_signal_reply_resolves_a_pending_question(tmp_path):
    """The inbound matcher: one pending question → a bare '1' is its answer;
    a named id wins over ambiguity; several pending with no id is NOT
    swallowed; a digit with no such option is not an answer."""
    import subprocess, sys, os, time as _t
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "contrib" / "tally-answer"
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_DECIDE_BIN="/bin/true",
               TALLY_DECISION_LOG=str(tmp_path / "log"))
    def run(msg):
        r = subprocess.run([sys.executable, str(script)], input=json.dumps(msg), text=True,
                           capture_output=True, env=env, timeout=10, check=True)
        return json.loads(r.stdout)
    (tmp_path / "signals").mkdir(); (tmp_path / "pending").mkdir()
    def raise_(sid, opts):
        (tmp_path / "signals" / f"{sid}.json").write_text(json.dumps({"label": sid.title(), "state": "attention", "detail": "?"}))
        (tmp_path / "pending" / f"{sid}.json").write_text(json.dumps({"id": sid, "label": sid.title(), "options": opts, "sent_at": _t.time()}))
    assert run({"text": "1"})["handled"] is False                       # nothing pending
    raise_("popups", ["A · Signal", "B · ntfy"])
    assert run({"text": "7"})["handled"] is False                       # no option 7
    r = run({"text": "b"})
    assert r["handled"] and r["answer"] == "2 — B · ntfy"
    a = json.loads((tmp_path / "answers" / "popups.json").read_text())
    assert a["via"] == "signal" and "recorded" in r["reply"]
    (tmp_path / "answers" / "popups.json").unlink()
    raise_("popups", ["A · Signal", "B · ntfy"]); raise_("disk", [])
    r = run({"text": "1"})           # could be a pane's y/n: nudge, never swallow
    assert r["handled"] is False and r["reason"] == "ambiguous" and "Which one" in r["reply"]
    r = run({"text": "disk: kill at 95%"})
    assert r["handled"] and r["id"] == "disk" and r["answer"] == "kill at 95%"
    (tmp_path / "answers" / "disk.json").unlink()
    r = run({"text": "1", "replyContext": {"quoteText": "◆ DECISION — Popups … [popups]"}})
    assert r["handled"] and r["id"] == "popups" and r["answer"].startswith("1 — A")


def test_notifier_holds_fire_while_a_deck_is_connected(tmp_path):
    """Deck first, phone when away: a fresh hub.alive means the question is
    on the keys in front of the operator — no Signal. Stale → send."""
    import subprocess, sys, os, time as _t
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "contrib" / "tally-notify"
    (tmp_path / "signals").mkdir()
    (tmp_path / "signals" / "q.json").write_text(json.dumps(
        {"label": "Q", "state": "attention", "detail": "?", "updated": _t.time(), "ttl": 3600}))
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_DECK_PROC="0")
    def once():
        r = subprocess.run([sys.executable, str(script), "--once", "--dry-run"],
                           capture_output=True, text=True, env=env, timeout=15, check=True)
        return r.stdout
    (tmp_path / "hub.alive").touch()                       # deck connected
    assert "DRY-RUN" not in once() and not (tmp_path / "pending" / "q.json").exists()
    old = _t.time() - 120
    os.utime(tmp_path / "hub.alive", (old, old))           # unplugged 2 min ago
    out = once()
    assert "DRY-RUN" in out and (tmp_path / "pending" / "q.json").exists()


def test_hub_heartbeat_marks_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    h = Hub([], log=lambda m: None)
    h.heartbeat()
    assert (tmp_path / "hub.alive").is_file()
    h._clear_heartbeat()
    assert not (tmp_path / "hub.alive").exists()


def test_hush_pauses_phone_notifications_and_unhush_resumes(tmp_path, capsys):
    import subprocess, sys, os, time as _t
    from pathlib import Path
    from tallydeck import cli, paths
    os.environ["TALLYDECK_STATE"] = str(tmp_path)
    try:
        assert paths.parse_duration("2h") == 7200 and paths.parse_duration("45m") == 2700
        assert paths.parse_duration("") is None
        (tmp_path / "signals").mkdir()
        (tmp_path / "signals" / "q.json").write_text(json.dumps(
            {"label": "Q", "state": "attention", "detail": "?", "updated": _t.time(), "ttl": 3600}))
        script = Path(__file__).resolve().parent.parent / "contrib" / "tally-notify"
        env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_DECK_PROC="0")
        once = lambda: subprocess.run([sys.executable, str(script), "--once", "--dry-run"],
                                      capture_output=True, text=True, env=env, timeout=15, check=True).stdout
        cli.main(["hush", "2h"])
        assert "hushed until" in capsys.readouterr().out
        assert "DRY-RUN" not in once()                       # held
        cli.main(["unhush"])
        assert "1 question" in capsys.readouterr().out
        assert "DRY-RUN" in once()                           # sent on lift
        # timed hush expires on its own
        paths.set_hush(0.01); _t.sleep(0.05)
        assert paths.hushed() is None
        # the Signal control words, through the matcher
        answer = Path(__file__).resolve().parent.parent / "contrib" / "tally-answer"
        run = lambda t: json.loads(subprocess.run([sys.executable, str(answer)], input=json.dumps({"text": t}),
                                                  capture_output=True, text=True, env=env, timeout=10, check=True).stdout)
        r = run("hush")
        assert r["handled"] and "hushed until you say unhush" in r["reply"]
        r = run("unhush")
        assert r["handled"] and "1 waiting" in r["reply"]
        assert run("hush puppies are shoes")["handled"] is False
    finally:
        os.environ.pop("TALLYDECK_STATE", None)


# ── audit fixes: the phone path must never eat a real message ────────────────

def _answer_runner(tmp_path):
    import subprocess, sys, os, time as _t
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "contrib" / "tally-answer"
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_DECIDE_BIN="/bin/true",
               TALLY_DECISION_LOG=str(tmp_path / "log"))
    (tmp_path / "signals").mkdir(exist_ok=True); (tmp_path / "pending").mkdir(exist_ok=True)
    def raise_(sid, opts, age=0):
        (tmp_path / "signals" / f"{sid}.json").write_text(json.dumps({"label": sid.title(), "state": "attention", "detail": "?"}))
        (tmp_path / "pending" / f"{sid}.json").write_text(json.dumps({"id": sid, "label": sid.title(), "options": opts, "sent_at": _t.time() - age}))
    def run(msg):
        r = subprocess.run([sys.executable, str(script)], input=json.dumps(msg), text=True,
                           capture_output=True, env=env, timeout=10, check=True)
        return json.loads(r.stdout)
    return raise_, run


def test_words_alone_never_count_as_an_implicit_answer(tmp_path):
    raise_, run = _answer_runner(tmp_path)
    raise_("popups", ["A · Signal", "B · ntfy"])
    # an unrelated DM while one question is pending must fall through
    assert run({"text": "check the deploy log on hermes"})["handled"] is False
    assert run({"text": "/status"})["handled"] is False
    # a bare option IS the answer…
    assert run({"text": "2"})["handled"] is True
    (tmp_path / "answers" / "popups.json").unlink()
    # …but not after the implicit window
    raise_("popups", ["A · Signal", "B · ntfy"], age=7200)
    assert run({"text": "2"})["reason"] == "stale"
    # words are fine when the question is named or quoted
    r = run({"text": "[popups] go with ntfy"})
    assert r["handled"] and r["answer"] == "go with ntfy"
    (tmp_path / "answers" / "popups.json").unlink()
    r = run({"text": "ntfy, final", "replyContext": {"quoteText": "◆ DECISION … [popups]"}})
    assert r["handled"] and r["answer"] == "ntfy, final"


def test_slug_prefix_sentences_are_not_answers(tmp_path):
    """'wires done?' starts with a pending slug and is a sentence for the
    pane, not an answer (audit P1-1). 'wires 1' is an answer."""
    raise_, run = _answer_runner(tmp_path)
    raise_("wires", ["A · rotate", "B · keep"]); raise_("gas", [])
    assert run({"text": "wires done?"})["handled"] is False
    r = run({"text": "wires 1"})
    assert r["handled"] and r["id"] == "wires"
    # two options sharing a letter: a bare letter is ambiguous, not an answer
    (tmp_path / "answers" / "wires.json").unlink()
    raise_("wires", ["Alpha", "Apex"])
    assert run({"text": "wires a"})["handled"] is False


def test_second_answer_loses_and_stale_answer_is_archived(tmp_path, monkeypatch):
    from tallydeck import cli
    raise_, run = _answer_runner(tmp_path)
    raise_("q", ["A", "B"])
    assert run({"text": "1"})["handled"] is True
    dup = run({"text": "2"})
    assert dup["handled"] is True and dup.get("duplicate") is True
    assert json.loads((tmp_path / "answers" / "q.json").read_text())["answer"].startswith("1")
    # a fresh raise of the same slug archives the old answer so `tally wait`
    # cannot return last round's decision (audit P0-2)
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    (tmp_path / "signals" / "q.json").unlink()
    cli.main(["raise", "q", "--state", "attention", "--sublabel", "again?"])
    assert not (tmp_path / "answers" / "q.json").exists()
    assert list((tmp_path / "answers" / ".archive").glob("q.*.json"))


def test_wait_ignores_torn_and_stale_answer_files(tmp_path, monkeypatch):
    import pytest as _pt, os, time as _t
    from tallydeck import cli
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    (tmp_path / "signals").mkdir(); (tmp_path / "answers").mkdir()
    (tmp_path / "signals" / "q.json").write_text("{}")
    (tmp_path / "answers" / "q.json").write_text("")                  # torn
    with _pt.raises(SystemExit) as e:
        cli.main(["wait", "q", "--every", "0.05", "--timeout", "0.3"])
    assert e.value.code == 1                                           # not "answered: ''"
    (tmp_path / "answers" / "q.json").write_text(json.dumps({"answer": "old"}))
    old = _t.time() - 100
    os.utime(tmp_path / "answers" / "q.json", (old, old))              # older than the flag
    with _pt.raises(SystemExit) as e:
        cli.main(["wait", "q", "--every", "0.05", "--timeout", "0.3"])
    assert e.value.code == 1


def test_raise_rejects_unsafe_ids(tmp_path, monkeypatch):
    import pytest as _pt
    from tallydeck import cli
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    for bad in ("../x", "a/b", ".hidden", ""):
        with _pt.raises(SystemExit):
            cli.main(["raise", bad, "--sublabel", "?"])


def test_notifier_survives_a_bad_drop_and_backs_off(tmp_path):
    import subprocess, sys, os, time as _t
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "contrib" / "tally-notify"
    (tmp_path / "signals").mkdir()
    (tmp_path / "signals" / "bad.json").write_text(json.dumps({"label": "b", "state": "attention", "detail": "?", "updated": None}))
    (tmp_path / "signals" / "good.json").write_text(json.dumps({"label": "g", "state": "attention", "detail": "?", "updated": _t.time()}))
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_DECK_PROC="0")
    out = subprocess.run([sys.executable, str(script), "--once", "--dry-run"], capture_output=True, text=True, env=env, timeout=15, check=True).stdout
    assert "notified good" in out and "notified bad" in out      # None updated → now; both fine
    # kill switch silences it without a restart
    (tmp_path / "answer-quiet").touch()
    (tmp_path / "signals" / "late.json").write_text(json.dumps({"label": "l", "state": "attention", "detail": "?", "updated": _t.time()}))
    out = subprocess.run([sys.executable, str(script), "--once", "--dry-run"], capture_output=True, text=True, env=env, timeout=15, check=True).stdout
    assert "notified late" not in out and "phone path OFF" in out


def test_notifier_treats_a_running_hub_process_as_deck_connected(tmp_path):
    """G got a Signal message with the deck plugged in: his hub predated the
    heartbeat. A live `tallydeck.cli serve` process must count too."""
    import subprocess, sys, os, time as _t
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "contrib" / "tally-notify"
    (tmp_path / "signals").mkdir()
    (tmp_path / "signals" / "q.json").write_text(json.dumps(
        {"label": "Q", "state": "attention", "detail": "?", "updated": _t.time(), "ttl": 3600}))
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_DECK_PROC="1")
    fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                             "tallydeck.cli", "serve"])          # looks like a hub
    try:
        _t.sleep(0.3)
        out = subprocess.run([sys.executable, str(script), "--once", "--dry-run"],
                             capture_output=True, text=True, env=env, timeout=15, check=True).stdout
        assert "DRY-RUN" not in out and "deck connected" in out
    finally:
        fake.kill(); fake.wait()
    # (a real hub may be running on this box; disable the process check
    # to assert the send path itself still works)
    env["TALLY_DECK_PROC"] = "0"
    out = subprocess.run([sys.executable, str(script), "--once", "--dry-run"],
                         capture_output=True, text=True, env=env, timeout=15, check=True).stdout
    assert "DRY-RUN" in out


def test_telegram_path_is_digits_only_fresh_only_and_never_swallows(tmp_path):
    import subprocess, sys, os, time as _t
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "contrib" / "tally-answer"
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_DECIDE_BIN="/bin/true",
               TALLY_DECISION_LOG=str(tmp_path / "log"))
    (tmp_path / "signals").mkdir()
    def raise_(sid, opts, age=0):
        (tmp_path / "signals" / f"{sid}.json").write_text(json.dumps(
            {"label": sid, "state": "attention", "detail": "?", "updated": _t.time() - age,
             "meta": {"options": opts}}))
    def run(text, **kw):
        r = subprocess.run([sys.executable, str(script)], input=json.dumps({"text": text, "channel": "telegram", **kw}),
                           text=True, capture_output=True, env=env, timeout=10, check=True)
        return json.loads(r.stdout)
    raise_("ram", ["Yes stop it", "Keep"])
    assert run("y")["handled"] is False                     # a casual ack, not option Y
    assert run("ok do it")["handled"] is False
    r = run("1")
    assert r["handled"] is True and r["swallow"] is False   # recorded, still routed
    (tmp_path / "answers" / "ram.json").unlink()
    raise_("ram", ["Yes stop it", "Keep"], age=7200)
    assert run("1")["reason"] == "stale"                    # hours-old flag claims nothing
    r = run("hush 1h")
    assert r["handled"] is True and r["swallow"] is False   # set, but the session sees it
    assert (tmp_path / "hush").exists()


def test_lone_letter_only_answers_a_lettered_label(tmp_path):
    raise_, run = _answer_runner(tmp_path)
    raise_("q", ["Yes stop it", "Keep"])
    assert run({"text": "y"})["handled"] is False          # an ack, not "Yes stop it"
    raise_("q2", ["A · Signal-first", "B · ntfy"])
    (tmp_path / "signals" / "q.json").unlink(); (tmp_path / "pending" / "q.json").unlink()
    r = run({"text": "b"})
    assert r["handled"] and r["answer"].startswith("2 — B")


def test_session_keys_carry_a_hub_side_route_action(tmp_path, monkeypatch):
    """Standardized presses: a session key, like a raised question, is
    answered by the hub putting the router popup up — not by anything on
    the deck machine. One-shots get no action."""
    proj = tmp_path / "-home-me-projects-w"
    proj.mkdir()
    _write_jsonl(proj, "abcd1234", [{"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}}])
    src = ClaudeSessionsSource(root=str(tmp_path))
    monkeypatch.setattr(src, "_session_panes", lambda: {"abcd1234": "work:1.1"})
    monkeypatch.setattr(src, "_all_pane_targets", lambda: {"work:1.1"})
    s = src.poll()[0]
    assert s.action and s.action["argv"][0].endswith("tally-popup-route")
    assert s.action["argv"][1] == "work:1.1" and s.action["argv"][2] == "abcd1234"
    assert s.action["argv"][-1] == s.id
    monkeypatch.setattr(src, "_session_panes", lambda: {"abcd1234": "oneshot:1.1"})
    monkeypatch.setattr(src, "_all_pane_targets", lambda: {"oneshot:1.1"})
    src._exact_memo = {}
    assert src.poll()[0].action is None


def test_hook_recorded_pane_makes_identity_durable(tmp_path, monkeypatch):
    """The env scan sees a session only while a tool subprocess lives; the
    PostToolUse hook's panes/<sid8>.json must carry it between tool calls,
    and only while that pane still exists."""
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    (tmp_path / "panes").mkdir()
    (tmp_path / "panes" / "abcd1234.json").write_text(json.dumps({"session": "abcd1234-x", "tmux": "work:2.1"}))
    src = ClaudeSessionsSource()
    monkeypatch.setattr(src, "_session_panes", lambda: {})
    monkeypatch.setattr(src, "_all_pane_targets", lambda: {"work:2.1"})
    assert src._exact_pane("abcd1234-x") == "work:2.1"
    monkeypatch.setattr(src, "_all_pane_targets", lambda: set())     # pane gone
    src._exact_memo = {}
    assert src._exact_pane("abcd1234-x") == ""


def test_hook_clear_records_the_pane(tmp_path):
    import subprocess, sys, os
    from pathlib import Path
    hook = Path(__file__).resolve().parent.parent / "contrib" / "tally-hook-clear"
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TMUX="/tmp/x,1,2", PATH=str(tmp_path) + ":" + os.environ["PATH"])
    (tmp_path / "tmux").write_text("#!/bin/sh\necho 'work:3.0|work|2|claude'\n"); os.chmod(tmp_path / "tmux", 0o755)
    subprocess.run([sys.executable, str(hook)], input=json.dumps({"session_id": "feedbeef-1"}), text=True, env=env, check=True, timeout=10)
    d = json.loads((tmp_path / "panes" / "feedbeef.json").read_text())
    assert d["tmux"] == "work:3.0" and d["session"] == "feedbeef-1"


def test_working_but_silent_for_long_is_stalled_not_blue(tmp_path):
    """G pressed a blue key whose brief said 'quiet 21m, nothing happening'.
    A Claude's-move tail that has not moved in 15 min is stalled → gray."""
    import os, time as _t
    proj = tmp_path / "-home-me-projects-w"
    proj.mkdir()
    p = _write_jsonl(proj, "stall001", [{"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}}])
    t0 = _t.time() - 20 * 60
    os.utime(p, (t0, t0))
    assert ClaudeSessionsSource(root=str(tmp_path)).poll()[0].state == IDLE
    assert ClaudeSessionsSource(root=str(tmp_path), stall=3600).poll()[0].state == WORKING


def test_session_ask_gets_the_router_popup_action(tmp_path):
    """A permission prompt (ask-<sid>) must open the router on press, hub-side —
    the Mac script no longer routes, so without this the red key was dead."""
    src = WatchDirSource(path=str(tmp_path))
    (tmp_path / "ask-c64c64c6.json").write_text(json.dumps(
        {"label": "c64", "state": "blocked", "detail": "Claude needs your permission to use Bash",
         "meta": {"session": "c64c64c6-x", "tmux": "c64:1.1", "project": "/p", "account": "A"}}))
    s = src.poll()[0]
    assert s.action and s.action["argv"][0].endswith("tally-popup-route")
    assert s.action["argv"][1] == "c64:1.1" and s.action["argv"][2] == "c64c64c6-x"
    assert "tally-popup-decide" not in s.action["argv"][0]


def test_hub_notices_its_own_code_changing(tmp_path, monkeypatch):
    from tallydeck import hub as hubmod
    h = Hub([], log=lambda m: None)
    t0 = h.code_mtime()
    assert t0 > 0
    calls = []
    monkeypatch.setattr(hubmod.os, "execv", lambda *a: calls.append(a))
    h._maybe_reexec(t0)                       # unchanged → no exec
    assert calls == []
    h._maybe_reexec(t0 - 100)                 # "older start" → code looks newer → exec
    assert calls and calls[0][1][1:3] == ["-m", "tallydeck.cli"]


def test_session_ask_pane_comes_from_the_registry_not_the_drop(tmp_path, monkeypatch):
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path / "state"))
    (tmp_path / "state" / "panes").mkdir(parents=True)
    (tmp_path / "state" / "panes" / "c64c64c6.json").write_text(json.dumps({"session": "c64c64c6-x", "tmux": "c64:1.1"}))
    src = WatchDirSource(path=str(tmp_path))
    (tmp_path / "ask-c64c64c6.json").write_text(json.dumps(
        {"label": "c64", "state": "blocked", "detail": "needs permission",
         "meta": {"session": "c64c64c6-x", "tmux": "victim:9.9"}}))      # hostile pane
    s = src.poll()[0]
    assert s.action["argv"][1] == "c64:1.1"


def test_deferral_in_a_report_is_not_an_ask():
    from tallydeck.sources.claude_sessions import asks_question as q
    assert not q("All done. System is healthy.\n\nThere are also 78 orphaned memory files — "
                 "pruning those is a bigger cleanup and your call, not something I'd do unilaterally.")
    assert q("Two options remain.\n\nYour call: prune them now, or leave them?")
    assert q("Ready to push.\n\nYour call — keep the old key or rotate it.")
    # An explicit unresolved approval remains an ask even before supporting status.
    assert q("I need your sign-off on the plan below.\n\nMeanwhile I fixed the tests; all green.")


# ── dedicated sessions: spawn / offer / phone fallback for session prompts ───

def _scratch_tmux():
    import subprocess, tempfile, os
    sock = tempfile.mktemp(prefix="tally-test-", suffix=".sock", dir="/tmp")
    return sock, (lambda *a: subprocess.run(["tmux", "-S", sock, *a], capture_output=True, text=True, timeout=10))


def test_spawn_makes_its_own_tmux_session_with_the_task_as_first_prompt(tmp_path):
    import subprocess, os, time as _t
    from pathlib import Path
    sock, T = _scratch_tmux()
    fake = tmp_path / "fakeclaude"; fake.write_text("#!/bin/sh\necho \"PROMPT:$2\"; sleep 20\n"); fake.chmod(0o755)
    spawn = Path(__file__).resolve().parent.parent / "contrib" / "tally-spawn"
    # NO_GATE: this asserts spawn's routing/naming, not the memory admission
    # gate (tested in test_launch_routing.py). Without it the test fails on any
    # box actually under the pressure the gate was written for.
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_TMUX_SOCKET=sock, CLAUDE_BIN=str(fake),
               TALLY_SPAWN_NO_GATE="1")
    try:
        r = subprocess.run([str(spawn), "demo-task", "-a", "A", "-c", "/tmp", "Build it, test it, push."],
                           capture_output=True, text=True, env=env, timeout=20)
        assert r.returncode == 0 and r.stdout.strip() == "demo-task", r.stderr
        _t.sleep(0.8)
        assert "PROMPT:Build it, test it, push." in T("capture-pane", "-t", "demo-task", "-p").stdout
        rec = json.loads((tmp_path / "spawned" / "demo-task.json").read_text())
        assert rec["target"] == T("list-panes", "-t", "demo-task", "-F", "#S:#I.#P").stdout.strip() and rec["cwd"] == "/tmp"
        r2 = subprocess.run([str(spawn), "demo-task", "-a", "A", "-c", "/tmp", "again"], capture_output=True, text=True, env=env, timeout=20)
        assert r2.stdout.strip() == "demo-task-2"                 # unique slugs
        assert subprocess.run([str(spawn), "../evil", "x"], capture_output=True, env=env).returncode == 2
    finally:
        T("kill-server")


def test_offer_spawns_immediately_and_asks_nobody(tmp_path):
    """The old offer raised a decision and held the work until it was answered,
    which made the operator's answer a precondition for anything starting. It
    spawns on the spot now: no signal raised, no waiter, no offer record."""
    import subprocess, os, sys, time as _t
    from pathlib import Path
    sock, T = _scratch_tmux()
    contrib = Path(__file__).resolve().parent.parent / "contrib"
    fake = tmp_path / "fakeclaude"; fake.write_text("#!/bin/sh\necho \"PROMPT:$2\"; sleep 20\n"); fake.chmod(0o755)
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_TMUX_SOCKET=sock, CLAUDE_BIN=str(fake),
               TALLY_BIN=str(contrib / "tally"), TALLY_SPAWN_BIN=str(contrib / "tally-spawn"),
               TALLY_SPAWN_NO_GATE="1")   # offer's contract, not the memory gate's
    env.pop("TMUX", None)
    try:
        r = subprocess.run([sys.executable, str(contrib / "tally-offer"), "big-job", "Rebuild the datum", "-a", "A", "-c", "/tmp",
                            "-p", "-"], input="Full brief here.", capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stderr
        assert "spawned big-job" in r.stdout                       # the caller learns the name at once
        assert "big-job" in T("list-sessions", "-F", "#S").stdout  # already running, nothing answered
        _t.sleep(0.8)
        assert "PROMPT:Full brief here." in T("capture-pane", "-t", "big-job", "-p").stdout
        assert not (tmp_path / "signals" / "offer-big-job.json").exists()
        assert not (tmp_path / "offers" / "big-job.json").exists()
    finally:
        T("kill-server")


def test_notifier_announces_session_prompts_when_the_deck_is_away(tmp_path):
    """Permission prompts and ended-with-a-question turns reach the phone
    once per ask when no deck is connected — notify-only, nothing pending."""
    import subprocess, sys, os, time as _t
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "contrib" / "tally-notify"
    (tmp_path / "signals").mkdir()
    (tmp_path / "signals" / "ask-c64c64c6.json").write_text(json.dumps(
        {"label": "c64", "state": "blocked", "detail": "Claude needs your permission to use Bash",
         "updated": _t.time(), "meta": {"session": "c64c64c6-x"}}))
    root = tmp_path / "root" / "-home-me-projects-w"; root.mkdir(parents=True)
    _write_jsonl(root, "asker001", [{"type": "assistant", "message": {"stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Two options. Which one do you want?"}]}}])
    t0 = _t.time() - 120
    os.utime(root / "asker001.jsonl", (t0, t0))
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(f'[[sources]]\nkind = "claude-sessions"\nroots = [{{ label = "T", path = "{tmp_path / "root"}" }}]\n')
    env = dict(os.environ, TALLYDECK_STATE=str(tmp_path), TALLY_DECK_PROC="0", TALLYDECK_CONFIG=str(cfg))
    once = lambda: subprocess.run([sys.executable, str(script), "--once", "--dry-run"],
                                  capture_output=True, text=True, env=env, timeout=20, check=True).stdout
    out = once()
    assert "needs your permission" in out and "Which one do you want" in out
    assert not list((tmp_path / "pending").glob("ask-*"))        # notify-only: never answerable
    assert "DRY-RUN" not in once()                                # announced once
    (tmp_path / "hub.alive").touch()
    os.utime(root / "asker001.jsonl", None)                       # a new ask, but deck is back
    assert "Which one" not in once()


def test_pipelink_reconnects_when_the_hub_dies_and_keeps_the_last_snapshot():
    import sys, time as _t
    from tallydeck.client import PipeLink
    # a "hub" that says hello, sends one snapshot, and exits
    hub = [sys.executable, "-c",
           'import json,sys; print(json.dumps({"type":"hello","name":"t"})); '
           'print(json.dumps({"type":"snapshot","signals":[{"id":"a","label":"a","state":"working"}]})); '
           'sys.stdout.flush()']
    link = PipeLink(hub)
    link.RECONNECT_AFTER = 0.2
    for _ in range(40):
        if link.poll(): break
        _t.sleep(0.05)
    assert [s.id for s in link.poll()] == ["a"]
    for _ in range(40):
        if not link.alive: break
        _t.sleep(0.05)
    assert link.alive is False
    assert [s.id for s in link.poll()] == ["a"]          # last frame stays up
    _t.sleep(0.25)
    assert link.reconnect() is True and link.reconnects == 1
    for _ in range(40):
        if link.alive or link.poll(): break
        _t.sleep(0.05)
    link.close()


# ── info bar v2: split A/B bar + Codex lane, two layouts ─────────────────────

def test_burn_codex_lane_real_and_dummy():
    from tallydeck.sources.burn import TokenBurnSource
    payload, targets = _burn_fixture()
    src = TokenBurnSource(codex_dummy=True)
    m = src.signals_from(payload, targets, now=0)[0].meta
    # `codex` is a LIST of lanes — one per Codex account — since the second
    # account arrived; the dummy stands in as a single lane.
    assert m["codex"][0]["dummy"] is True and m["codex"][0]["id"] == "1"
    assert m["lanes"][0]["burned_m"] > 0 and m["lanes"][0]["target_m"] > 0   # for the overlay figures
    payload["accounts"].append({"id": "codex", "provider": "openai", "enabled": True,
                                "session_pct": 41, "session_reset": "2026-09-07T08:00:00+00:00"})
    m = src.signals_from(payload, targets, now=0)[0].meta
    assert m["codex"][0]["pct"] == 41 and not m["codex"][0].get("dummy")
    assert [l["id"] for l in m["lanes"]] == ["A", "B"]            # codex never a lane
    assert TokenBurnSource().signals_from(_burn_fixture()[0], targets, now=0)[0].meta["codex"] == []


def test_burn_emits_one_lane_per_codex_account():
    """Two Codex accounts, two lanes on the bottom bar, badged 1 and 2.

    A single merged Codex figure would average a spent plan with an untouched
    one — the difference between "stop routing there" and "route it all there".
    """
    from tallydeck.sources.burn import TokenBurnSource
    payload, targets = _burn_fixture()
    payload["accounts"] += [
        {"id": "O", "provider": "codex", "enabled": True, "session_pct": 0,
         "session_reset_mins": None, "weekly_pct": 64.0, "weekly_reset_mins": 9000.0,
         "codex": {"target_pct": 70, "age_mins": 2.0, "stale": False}},
        {"id": "O2", "provider": "codex", "enabled": True, "session_pct": 0,
         "session_reset_mins": None, "weekly_pct": 0.0, "weekly_reset_mins": 10070.0,
         "codex": {"target_pct": 70, "age_mins": 2.0, "stale": False}},
    ]
    cx = TokenBurnSource().signals_from(payload, targets, now=0)[0].meta["codex"]
    assert [l["id"] for l in cx] == ["1", "2"]            # the badge G reads
    assert [l["account"] for l in cx] == ["O", "O2"]      # the id config uses
    assert [l["pct"] for l in cx] == [64.0, 0.0]
    # A parked account leaves the deck entirely, so account-mode.sh single
    # shows one lane rather than a lane that cannot be used.
    payload["accounts"][-1]["enabled"] = False
    cx = TokenBurnSource().signals_from(payload, targets, now=0)[0].meta["codex"]
    assert [l["account"] for l in cx] == ["O"]


def test_meter2_renders_the_split_bar_with_and_without_codex():
    from tallydeck.render.meter import draw_meter2
    lanes = [{"id": "A", "pct": 26, "frac": 0.26, "target": 0.4, "clock": "1:48"},
             {"id": "B", "pct": 58, "frac": 0.58, "target": 0.7, "clock": "3:05"}]
    codex = {"id": "X", "pct": 37, "frac": 0.37, "target": 0.6, "clock": "2:10", "dummy": True}
    a = draw_meter2((248, 58), lanes, codex, hot="B")
    b = draw_meter2((248, 58), lanes, None, hot="A")
    assert a.size == b.size == (248, 58)
    assert list(a.getdata()) != list(b.getdata())


def test_png_screen_face_uses_v2_with_two_lanes():
    from tallydeck.render.png import _screen_face
    sig = Signal(id="burn/session", label="burn", state=WORKING, meta={
        "meter": True, "frac": 0.4, "lanes": [{"id": "A", "frac": 0.2, "pct": 20}, {"id": "B", "frac": 0.5, "pct": 50}],
        "codex": {"id": "X", "pct": 10, "frac": 0.1}})
    lay = View(profile=NEO).layout([sig])
    assert lay.meter is sig
    assert _screen_face(NEO, lay, 0.0).size == (248, 58)


# ── Codex on the deck: left-edge tally bar, Account O on the info bar ────────

def test_codex_key_wears_its_tally_bar_on_the_left_edge():
    """The harness has to read from the SHAPE of the key — G glances at the
    deck, he does not read labels. Codex: bar down the left. Claude: bar
    across the top. Same state colour in both, because colour means state."""
    from tallydeck.render.keycard import draw_key
    px = 96
    cc = draw_key(Signal(id="cc/x", label="pv", state=WORKING), px)
    cx = draw_key(Signal(id="cx/x", label="pv", state=WORKING,
                         meta={"harness": "codex"}), px)
    blue = theme.hex_rgb(theme.STATE_COLOR[WORKING])

    def near(px_rgb, ref, tol=26):
        return all(abs(a - b) <= tol for a, b in zip(px_rgb[:3], ref))

    # Claude: top-right is bar, mid-left is not.
    assert near(cc.getpixel((px - 3, 2)), blue)
    assert not near(cc.getpixel((1, px // 2)), blue)
    # Codex: mid-left and bottom-left are bar, top-right is not.
    assert near(cx.getpixel((1, px // 2)), blue)
    assert near(cx.getpixel((1, px - 3)), blue)
    assert not near(cx.getpixel((px - 3, 2)), blue)
    # The flash frame keeps the tell: a flooded Codex key still has its edge.
    lit = draw_key(Signal(id="cx/x", label="pv", state=ATTENTION,
                          meta={"harness": "codex"}), px, lit=True)
    assert lit.getpixel((1, px // 2)) != lit.getpixel((px // 2, px // 2))


def test_codex_account_2_wears_its_tally_bar_on_the_right_edge():
    """Two Codex accounts share the deck now. Account 1 keeps the left edge,
    account 2 takes the right — the side of the bar is the account tell, and
    it reads across the room where the two-character bottom-right badge
    ("O" vs "O2") does not (G, 2026-09-09)."""
    from tallydeck.render.keycard import draw_key
    px = 96
    one = draw_key(Signal(id="cx/1", label="pv", state=WORKING,
                          meta={"harness": "codex", "account": "O"}), px)
    two = draw_key(Signal(id="cx/2", label="pv", state=WORKING,
                          meta={"harness": "codex", "account": "O2"}), px)
    blue = theme.hex_rgb(theme.STATE_COLOR[WORKING])

    def near(px_rgb, ref, tol=26):
        return all(abs(a - b) <= tol for a, b in zip(px_rgb[:3], ref))

    # Account 1: left edge painted, right edge clear.
    assert near(one.getpixel((1, px // 2)), blue)
    assert not near(one.getpixel((px - 2, px // 2)), blue)
    # Account 2: the mirror image — and still not a Claude key (no top bar).
    assert near(two.getpixel((px - 2, px // 2)), blue)
    assert near(two.getpixel((px - 2, px - 3)), blue)
    assert not near(two.getpixel((1, px // 2)), blue)
    assert not near(two.getpixel((px // 2, 2)), blue)


def test_meter2_splits_the_bottom_bar_across_two_codex_accounts():
    """Same format as A/B: two half-bars, two badges, two countdowns."""
    from tallydeck.render.meter import draw_meter2
    lanes = [{"id": "A", "pct": 26, "frac": 0.26, "target": 0.4, "clock": "1:48"},
             {"id": "B", "pct": 58, "frac": 0.58, "target": 0.7, "clock": "3:05"}]
    two = [{"id": "1", "account": "O", "pct": 64, "frac": 0.64, "target": 0.7,
            "clock": "6d6h", "window": "weekly"},
           {"id": "2", "account": "O2", "pct": 0, "frac": 0.0, "target": 0.7,
            "clock": "6d23h", "window": "weekly"}]
    one = draw_meter2((248, 58), lanes, two[:1], hot="A")
    both = draw_meter2((248, 58), lanes, two, hot="A")
    assert one.size == both.size == (248, 58)
    # A second lane changes the bottom bar; if it did not, account 2 would be
    # invisible on the deck while quietly holding half the Codex capacity.
    assert list(one.getdata()) != list(both.getdata())
    # A single dict still works — callers written before the second account.
    assert draw_meter2((248, 58), lanes, two[0], hot="A").size == (248, 58)
    assert draw_meter2((248, 58), lanes, [], hot="A").size == (248, 58)


def test_codex_key_text_clears_the_left_bar():
    """Shifting the bar without shifting the text would print the label over
    it. The left margin moves with the bar; the badge (right) does not."""
    from tallydeck.render.keycard import draw_key
    px = 96
    sig = Signal(id="cx/x", label="session-codex", state=WORKING,
                 sublabel="astra · gpt-6", meta={"harness": "codex"})
    face = draw_key(sig, px)
    blue = theme.hex_rgb(theme.STATE_COLOR[WORKING])
    col = [face.getpixel((3, y)) for y in range(12, px - 12)]
    assert all(all(abs(a - b) <= 26 for a, b in zip(p[:3], blue)) for p in col)


def test_term_surface_marks_codex_keys_too():
    from tallydeck.render.term import render_term
    sigs = [Signal(id="cx/x", label="codex", state=WORKING,
                   meta={"harness": "codex"})]
    assert "▏" in render_term(NEO, View(profile=NEO).layout(sigs))
    plain = [Signal(id="cc/x", label="claude", state=WORKING)]
    assert "▏" not in render_term(NEO, View(profile=NEO).layout(plain))


def test_raise_from_a_codex_session_stamps_the_harness(tmp_path, monkeypatch):
    """A Codex agent has no CLAUDE_SESSION_ID to give it away, so the raise
    reads the environment Codex actually sets. Explicit --harness wins."""
    from tallydeck import cli
    from tallydeck.signal import is_codex
    monkeypatch.setenv("TALLYDECK_STATE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TALLY_HARNESS", raising=False)
    monkeypatch.setenv("CODEX_HOME", "/home/x/.codex")
    monkeypatch.chdir(tmp_path)
    cli.main(["raise", "cx", "--state", "attention", "--sublabel", "ship it?"])
    sig = WatchDirSource(path=str(tmp_path / "signals")).poll()[0]
    assert sig.meta["harness"] == "codex" and is_codex(sig)
    # A Claude session in the same shell (CODEX_HOME exported by a wrapper it
    # once ran) must NOT be mislabelled: a session id means Claude Code.
    monkeypatch.setenv("CLAUDE_SESSION_ID", "dead-0000-4000-8000-000000000000")
    cli.main(["raise", "cc2", "--state", "attention", "--sublabel", "?"])
    d = json.loads((tmp_path / "signals" / "cc2.json").read_text())
    assert "harness" not in d.get("meta", {})
    cli.main(["raise", "cc2", "--harness", "codex"])
    d = json.loads((tmp_path / "signals" / "cc2.json").read_text())
    assert d["meta"]["harness"] == "codex"


def test_codex_lane_uses_account_o_weekly_window_and_flags_a_stale_snapshot():
    """Account O (ChatGPT Pro) reports a WEEKLY quota and session_pct=0 with
    no 5h reset. Painting the session number would show a permanent 0% next
    to A/B's real 5h figures; and a snapshot whose cron died must say so."""
    from tallydeck.sources.burn import TokenBurnSource
    payload, targets = _burn_fixture()
    o = {"id": "O", "provider": "codex", "enabled": True,
         "session_pct": 0, "session_reset_mins": None,
         "weekly_pct": 12.0, "weekly_reset_mins": 9993.4,
         "codex": {"target_pct": 70, "age_mins": 1.8, "stale": False}}
    payload["accounts"].append(o)
    src = TokenBurnSource()
    cx = src.signals_from(payload, targets, now=0)[0].meta["codex"][0]
    assert cx["pct"] == 12.0 and cx["window"] == "weekly"
    assert cx["target"] == 0.7 and cx["stale"] is False
    assert cx["clock"] == "6d22h"           # days, not a 166-hour clock
    assert cx["id"] == "1" and cx["account"] == "O"
    assert [l["id"] for l in src.signals_from(payload, targets, now=0)[0].meta["lanes"]] \
        == ["A", "B"]                       # codex is its own bar, never a lane
    o["codex"]["age_mins"] = 140            # cron stopped ~2h ago
    assert src.signals_from(payload, targets, now=0)[0].meta["codex"][0]["stale"] is True
    o["codex"]["age_mins"] = 1.8
    o["codex"]["stale"] = True              # tokenburn's own verdict is honoured
    assert src.signals_from(payload, targets, now=0)[0].meta["codex"][0]["stale"] is True


def test_meter2_whispers_the_codex_window_and_staleness():
    from tallydeck.render.meter import draw_meter2
    lanes = [{"id": "A", "pct": 26, "frac": 0.26, "target": 0.4, "clock": "1:48"}]
    live = {"id": "X", "pct": 12, "frac": 0.12, "target": 0.7, "clock": "6d22h",
            "window": "weekly"}
    stale = dict(live, stale=True)
    a = draw_meter2((248, 58), lanes, live, hot="A")
    b = draw_meter2((248, 58), lanes, stale, hot="A")
    assert a.size == b.size == (248, 58)
    assert list(a.getdata()) != list(b.getdata())     # "weekly" vs "stale"


# ── Codex sessions as keys ──────────────────────────────────────────────────

def _rollout(day_dir, uuid, cwd, originator="codex-tui", events=(), ts=None):
    day_dir.mkdir(parents=True, exist_ok=True)
    fp = day_dir / f"rollout-2026-09-08T08-00-00-{uuid}.jsonl"
    recs = [{"type": "session_meta", "payload": {
        "session_id": uuid, "cwd": cwd, "originator": originator,
        "source": "exec" if originator.endswith("exec") else "cli",
        "timestamp": ts or "2026-09-08T08:00:00.000Z"}}]
    recs += list(events)
    fp.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return fp


def _complete(msg):
    return {"type": "event_msg", "payload": {"type": "task_complete",
                                             "last_agent_message": msg}}


def test_codex_classify_reads_the_rollout_grammar():
    from tallydeck.sources.codex_sessions import classify
    started = json.dumps({"type": "event_msg", "payload": {"type": "task_started"}})
    mid = json.dumps({"type": "response_item", "payload": {"type": "reasoning"}})
    noise = json.dumps({"type": "token_usage_record", "payload": {"x": 1}})
    count = json.dumps({"type": "event_msg", "payload": {"type": "token_count"}})
    assert classify([started])[0] == WORKING
    assert classify([started, mid, noise, count])[0] == WORKING
    assert classify([started, json.dumps(_complete("Done — pushed a3f1."))])[0] == SUCCESS
    ask = json.dumps(_complete("Two options here. Which do you want?"))
    assert classify([started, ask])[0] == ATTENTION
    assert classify([started, json.dumps(
        {"type": "event_msg", "payload": {"type": "turn_aborted"}})])[0] == IDLE
    assert classify([noise])[0] == IDLE


def test_codex_source_emits_keys_marked_as_codex(tmp_path):
    """Every Codex key must carry meta.harness — that marker is the ONLY
    thing the left-edge bar keys off, on every surface."""
    from tallydeck.sources.codex_sessions import CodexSessionsSource
    from tallydeck.signal import is_codex
    day = tmp_path / "2026" / "09" / "08"
    _rollout(day, "aaaaaaaa-1111-4000-8000-000000000000", "/home/x/projects/wires",
             events=[_complete("Which scope should I implement for v2?")])
    src = CodexSessionsSource(root=str(tmp_path), dwell=0, socket="/nonexistent")
    sigs = src.poll()
    assert len(sigs) == 1
    s = sigs[0]
    assert is_codex(s) and s.meta["harness"] == "codex"
    assert s.state == ATTENTION and s.label == "wires"
    assert s.meta["account"] == "O" and s.meta["project"].endswith("/wires")
    assert "v2" in s.sublabel                      # the ask, not an age
    assert s.action["argv"][2] == s.meta["session"]  # full UUID brief + Codex resume


def test_codex_exec_oneshots_stay_off_the_deck_unless_asked(tmp_path):
    """codex-oneshot runs are disposable: nobody answers one, and four of them
    would push the real fleet off an 8-key deck."""
    from tallydeck.sources.codex_sessions import CodexSessionsSource
    day = tmp_path / "2026" / "09" / "08"
    _rollout(day, "bbbbbbbb-2222-4000-8000-000000000000", "/tmp/job",
             originator="codex_exec",
             events=[_complete("Which model should I use?")])
    assert CodexSessionsSource(root=str(tmp_path), dwell=0,
                               socket="/nonexistent").poll() == []
    on = CodexSessionsSource(root=str(tmp_path), dwell=0, include_exec=True,
                             socket="/nonexistent").poll()
    assert len(on) == 1 and on[0].meta["oneshot"] is True
    assert on[0].state == WORKING and on[0].priority == -10   # never ATTENTION


def test_codex_stale_rollouts_and_stalled_turns_drop_out(tmp_path):
    from tallydeck.sources.codex_sessions import CodexSessionsSource
    day = tmp_path / "2026" / "09" / "08"
    fp = _rollout(day, "cccccccc-3333-4000-8000-000000000000", "/home/x/a",
                  events=[{"type": "event_msg",
                           "payload": {"type": "task_started"}}])
    old = time.time() - 4000
    os.utime(fp, (old, old))
    src = CodexSessionsSource(root=str(tmp_path), dwell=0, socket="/nonexistent")
    assert src.poll() == []                        # older than `stale`
    mid = time.time() - 1200                       # inside stale, past stall
    os.utime(fp, (mid, mid))
    assert src.poll()[0].state == IDLE             # "working" with no output


def test_codex_pane_match_requires_the_rollout_to_postdate_the_process(tmp_path,
                                                                      monkeypatch):
    """The rollout is written on the session's first turn, which can be hours
    after launch — so ordering is the evidence, not proximity. A rollout from a
    PREVIOUS session in the same directory must not capture the live pane."""
    from tallydeck.sources import codex_sessions as cx
    day = tmp_path / "2026" / "09" / "08"
    start = time.time()
    _rollout(day, "dddddddd-4444-4000-8000-000000000000", "/home/x/p",
             ts=_iso(start - 3600))                # last session: before launch
    _rollout(day, "eeeeeeee-5555-4000-8000-000000000000", "/home/x/p",
             ts=_iso(start + 30))                  # this one: after launch
    # codex_home points at an empty directory: no logs database, so no pid →
    # thread answer, which is exactly when this fallback has to carry.
    src = cx.CodexSessionsSource(root=str(tmp_path), dwell=0,
                                 codex_home=str(tmp_path / "no-codex"),
                                 sync_titles=False)
    monkeypatch.setattr(src, "_live_codex_procs",
                        lambda: [("cx:1.1", "/home/x/p", start, 4242)])
    assert src._codex_panes() == {"eeeeeeee-5555-4000-8000-000000000000": "cx:1.1"}
    # Two live processes in one directory are ambiguous: a guess must not route.
    src._cp_ts = 0
    monkeypatch.setattr(src, "_live_codex_procs",
                        lambda: [("cx:1.1", "/home/x/p", start, 4242),
                                 ("cx:1.2", "/home/x/p", start, 4243)])
    assert src._codex_panes() == {}


def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
