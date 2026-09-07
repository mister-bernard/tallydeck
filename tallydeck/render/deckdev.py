"""Hardware surface: a real Stream Deck via python-elgato-streamdeck.

Requires the `deck` extra (`pip install -e ".[deck]"`) and libhidapi
(`brew install hidapi` on macOS). The official Elgato Stream Deck app
claims the device exclusively — quit it before running.

Feature use is defensive (hasattr checks) so the same surface drives a
Neo (keys + info bar + touch points), a Mini/MK.2 (keys only), or an XL.
On the Neo, python-elgato-streamdeck exposes the two touch points as
extra key indices past the LCD keys; we map them to page prev/next.
"""

from __future__ import annotations

import threading

from ..devices import DeviceProfile, PROFILES
from ..view import Layout
from .keycard import draw_key, draw_screen


def _profile_for(deck) -> DeviceProfile:
    """Derive a DeviceProfile from the opened device."""
    kf = deck.key_image_format()
    size = kf["size"][0] if kf and kf.get("size") else 96
    layout = deck.key_layout() if hasattr(deck, "key_layout") else None
    rows, cols = layout if layout else {
        6: (2, 3), 8: (2, 4), 15: (3, 5), 32: (4, 8),
    }.get(deck.key_count(), (1, deck.key_count()))
    screen = None
    if hasattr(deck, "screen_image_format"):
        try:
            sf = deck.screen_image_format()
            if sf and sf.get("size"):
                screen = tuple(sf["size"])
        except Exception:
            screen = None
    for p in PROFILES.values():
        if (p.rows, p.cols, p.key_px) == (rows, cols, size):
            return p
    return DeviceProfile("detected", rows=rows, cols=cols, key_px=size,
                         screen_px=screen)


class DeckSurface:
    def __init__(self, preferred: str = "neo", brightness: int = 80):
        try:
            from StreamDeck.DeviceManager import DeviceManager
            from StreamDeck.ImageHelpers import PILHelper
        except ImportError as e:
            raise SystemExit(
                "streamdeck library not installed — pip install -e '.[deck]' "
                "(and `brew install hidapi` on macOS)") from e
        self._pil = PILHelper
        decks = DeviceManager().enumerate()
        if not decks:
            raise SystemExit(
                "no Stream Deck found (is the Elgato app running? quit it — "
                "it holds the device exclusively)")
        self.deck = decks[0]
        self.deck.open()
        self.deck.reset()
        self.deck.set_brightness(brightness)
        self.profile = _profile_for(self.deck)
        self._lock = threading.Lock()
        self._on_key = None
        self._on_touch = None
        self.deck.set_key_callback(self._key_event)
        if hasattr(self.deck, "set_touchscreen_callback"):
            try:
                self.deck.set_touchscreen_callback(self._touch_event)
            except Exception:
                pass

    # ── events ───────────────────────────────────────────────────────────────

    def set_callbacks(self, on_key=None, on_touch=None) -> None:
        self._on_key = on_key
        self._on_touch = on_touch

    def _key_event(self, _deck, key: int, pressed: bool) -> None:
        if key >= self.profile.keys:
            # Neo touch points arrive as key indices past the LCD keys:
            # first = page left, second = page right.
            if pressed and self._on_touch:
                self._on_touch(-1 if key == self.profile.keys else +1)
            return
        if self._on_key:
            self._on_key(key, pressed)

    def _touch_event(self, _deck, _evt, args) -> None:
        if self._on_touch and isinstance(args, dict):
            x = args.get("x", 0)
            w = (self.profile.screen_px or (100, 0))[0]
            self._on_touch(+1 if x > w / 2 else -1)

    # ── drawing ──────────────────────────────────────────────────────────────

    def show(self, layout: Layout, lit: dict[str, bool]) -> None:
        with self._lock:
            for i, sig in enumerate(layout.keys[:self.profile.keys]):
                img = draw_key(sig, self.profile.key_px,
                               lit=bool(sig and lit.get(sig.id)))
                native = self._pil.to_native_key_format(
                    self.deck, self._pil.create_scaled_key_image(self.deck, img))
                self.deck.set_key_image(i, native)
            if self.profile.screen_px and hasattr(self.deck, "set_screen_image"):
                scr = draw_screen(self.profile.screen_px, layout.summary,
                                  layout.page, layout.pages)
                try:
                    self.deck.set_screen_image(
                        self._pil.to_native_screen_format(self.deck, scr))
                except Exception:
                    pass

    def close(self) -> None:
        try:
            with self._lock:
                self.deck.reset()
                self.deck.close()
        except Exception:
            pass
