"""The Plus strip write must pass the panel size explicitly.

python-elgato-streamdeck's set_touchscreen_image() defaults width/height to 0
and then bounds-checks them, so a call that relies on the defaults raises
IndexError on every frame — and deckdev swallowed it, so the strip was black
on the hardware while `tally png` showed captions. The fake below mirrors the
library's check exactly."""
import threading
import time

from StreamDeck.ImageHelpers import PILHelper

from tallydeck.devices import PROFILES
from tallydeck.render.deckdev import DeckSurface
from tallydeck.signal import Signal, WORKING
from tallydeck.view import Layout


class FakePlus:
    TOUCHSCREEN_PIXEL_WIDTH, TOUCHSCREEN_PIXEL_HEIGHT = 800, 100

    def __init__(self):
        self.writes = []

    def touchscreen_image_format(self):
        return {"size": (800, 100), "format": "JPEG", "flip": (False, False), "rotation": 0}

    def key_image_format(self):
        return {"size": (120, 120), "format": "JPEG", "flip": (False, False), "rotation": 0}

    def set_touchscreen_image(self, image, x_pos=0, y_pos=0, width=0, height=0):
        # verbatim shape of the library's guard
        if min(max(width, 1), self.TOUCHSCREEN_PIXEL_WIDTH - x_pos) != width:
            raise IndexError(f"Invalid draw width {width}.")
        if min(max(height, 1), self.TOUCHSCREEN_PIXEL_HEIGHT - y_pos) != height:
            raise IndexError(f"Invalid draw height {height}.")
        self.writes.append((x_pos, y_pos, width, height, len(bytes(image))))

    def set_key_image(self, *a, **k):
        pass


def surface(deck):
    s = object.__new__(DeckSurface)
    s.deck, s.profile, s._strip, s._pil = deck, PROFILES["plus"], "touch", PILHelper
    s._lock, s._drawn, s._leds, s._strip_err = threading.Lock(), {}, None, False
    s._on_key = s._on_touch = s._on_dial = None
    return s


def test_strip_write_passes_the_panel_size_and_lands():
    deck = FakePlus()
    zones = Signal(id="focus/zones", label="focus", state=WORKING,
                   meta={"zones": [{"title": "model", "value": "Fable 5.1"}]})
    lay = Layout(keys=[None] * 8, page=0, pages=1, summary="quiet", zones=zones)
    surface(deck).show(lay, {}, t=time.time())
    assert deck.writes and deck.writes[0][:4] == (0, 0, 800, 100)


def test_a_strip_write_failure_names_itself_once(capsys):
    class Broken(FakePlus):
        def set_touchscreen_image(self, *a, **k):
            raise IndexError("boom")
    s = surface(Broken())
    lay = Layout(keys=[None] * 8, page=0, pages=1, summary="quiet")
    s.show(lay, {}, t=1.0)
    s.show(lay, {}, t=2.0)
    err = capsys.readouterr().err
    assert err.count("strip write failed") == 1 and "boom" in err
