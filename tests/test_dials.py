"""Dials: a turn previews, never applies."""

from tallydeck.dials import PREVIEW_FOR, expire, overlay, step
from tallydeck.render.deckdev import DeckSurface
from tallydeck.render.zones import draw_zones

Z = [
    {"key": "model", "title": "model", "value": "Fable 5.1", "sub": "acct C", "rgb": [45, 14, 0],
     "current": "fable", "options": [{"id": "default", "label": "Default"}, {"id": "opus", "label": "Opus"},
                                     {"id": "fable", "label": "Fable"}, {"id": "sonnet", "label": "Sonnet"}]},
    {"key": "strength", "title": "strength", "value": "high", "sub": "", "rgb": [45, 32, 0],
     "current": "high", "options": [{"id": l, "label": l} for l in ("low", "medium", "high", "xhigh", "max")]},
    {"key": "action", "title": "action", "value": "auto", "sub": "", "rgb": [50, 46, 0],
     "current": "auto", "options": [{"id": "default", "label": "manual"}, {"id": "plan", "label": "plan"},
                                    {"id": "auto", "label": "auto"}]},
    {"key": "burn", "title": "burn", "value": "—", "sub": "step 6", "rgb": [26, 26, 26]},
]
NOW = 1_000_000.0


def test_a_turn_previews_the_neighbour_and_returning_cancels():
    p = step(Z, {}, 0, +1, NOW)
    assert p[0]["idx"] == 3 and overlay(Z, p, NOW)[0]["pending"] == "Sonnet"
    p = step(Z, p, 0, -1, NOW)
    assert 0 not in p                       # back on the current value: no preview


def test_turns_clamp_at_the_ends_instead_of_wrapping():
    p = step(Z, {}, 1, +40, NOW)
    assert p[1]["idx"] == 4                 # max, not wrapped to low
    p = step(Z, p, 1, -40, NOW)
    assert p[1]["idx"] == 0


def test_a_zone_without_options_is_inert():
    assert step(Z, {}, 3, +1, NOW) == {}
    assert step(Z, {}, 9, +1, NOW) == {}


def test_previews_expire_and_expired_ones_do_not_overlay():
    p = step(Z, {}, 2, -1, NOW)
    assert overlay(Z, p, NOW + PREVIEW_FOR - 0.1)[2].get("pending") == "plan"
    assert "pending" not in overlay(Z, p, NOW + PREVIEW_FOR + 0.1)[2]
    assert expire(p, NOW + PREVIEW_FOR + 0.1) == {}


def test_overlay_is_a_copy_and_writes_the_hint_into_the_sub():
    p = step(Z, {}, 0, +1, NOW)
    out = overlay(Z, p, NOW, hint="preview · push: not wired yet")
    assert out[0]["sub"] == "preview · push: not wired yet"
    assert Z[0]["sub"] == "acct C" and "pending" not in Z[0]


def test_pending_draws_differently_from_current():
    a = draw_zones((800, 100), Z)
    b = draw_zones((800, 100), overlay(Z, step(Z, {}, 0, +1, NOW), NOW))
    assert a.size == b.size == (800, 100) and a.tobytes() != b.tobytes()


class _Ev:
    def __init__(self, name): self.name = name


def test_driver_routes_turns_and_ignores_pushes():
    s = object.__new__(DeckSurface)
    seen = []
    s._on_dial = lambda d, v: seen.append((d, v))
    s._dial_event(None, 2, _Ev("TURN"), -3)
    s._dial_event(None, 0, _Ev("PUSH"), True)
    assert seen == [(2, -3)]
