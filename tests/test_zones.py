"""The Plus strip as captions of the focused pane (meta.zones signals)."""

import time

from tallydeck.devices import PROFILES
from tallydeck.render.png import _screen_face, render_png
from tallydeck.render.zones import draw_zones, is_stale
from tallydeck.signal import Signal, WORKING
from tallydeck.view import Layout, View

ZONES = [
    {"key": "model", "title": "model", "value": "Fable 5.1", "sub": "acct C", "rgb": [45, 14, 0]},
    {"key": "strength", "title": "strength", "value": "max", "sub": "thinking on", "rgb": [60, 46, 32]},
    {"key": "action", "title": "action", "value": "auto", "sub": "", "rgb": [50, 46, 0]},
    {"key": "burn", "title": "burn", "value": "—", "sub": "step 6", "rgb": [26, 26, 26]},
]


def zsig(updated=None, zones=None):
    return Signal(id="focus/zones", label="focus", state=WORKING, group="focus",
                  updated=time.time() if updated is None else updated, ttl=12,
                  meta={"zones": ZONES if zones is None else zones})


def test_zones_signal_goes_to_the_strip_not_to_a_key_nor_the_summary():
    v = View(profile=PROFILES["plus"])
    lay = v.layout([zsig(), Signal(id="cc/a", label="a", state=WORKING)])
    assert lay.zones is not None and lay.zones.id == "focus/zones"
    assert all(k is None or k.id != "focus/zones" for k in lay.keys)
    assert "1 working" in lay.summary          # the zones signal is not counted


def test_draw_zones_is_strip_sized_for_any_zone_count():
    for n in (0, 1, 4, 6):
        img = draw_zones((800, 100), (ZONES * 2)[:n])
        assert img.size == (800, 100)


def test_draw_zones_tolerates_garbage_without_raising():
    img = draw_zones((800, 100), [None, {"rgb": "nope"}, {"value": None}, 42, "x"])
    assert img.size == (800, 100)


def test_screen_face_prefers_zones_over_meter_and_summary():
    meter = Signal(id="m", label="m", state=WORKING, meta={"meter": True, "frac": 0.5})
    lay = Layout(keys=[None] * 8, page=0, pages=1, summary="quiet",
                 meter=meter, zones=zsig())
    assert _screen_face(PROFILES["plus"], lay, 0.0).size == (800, 100)
    # and the whole face still renders with zones on the strip
    assert render_png(PROFILES["plus"], lay, scale=1).size[0] >= 800


def test_stale_is_drawn_from_age_never_assumed():
    fresh, old = zsig(), zsig(updated=time.time() - 60)
    assert not is_stale(fresh) and is_stale(old)
    a = draw_zones((800, 100), ZONES, stale=False)
    b = draw_zones((800, 100), ZONES, stale=True)
    assert a.tobytes() != b.tobytes()
