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
from .keycard import draw_key
from .png import _screen_face


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
        self._drawn: dict[int, tuple] = {}   # key index → content fingerprint
        self._leds: tuple | None = None
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

    def show(self, layout: Layout, lit: dict[str, bool],
             t: float = 0.0, pressed: frozenset = frozenset()) -> None:
        with self._lock:
            for i, sig in enumerate(layout.keys[:self.profile.keys]):
                is_lit = bool(sig and lit.get(sig.id))
                is_pressed = i in pressed
                # Skip HID writes for unchanged keys — flashing 2 of 8 keys
                # should cost 2 updates per frame, not 8.
                print_key = (None if sig is None else
                             (sig.id, sig.state, sig.label, sig.sublabel,
                              sig.progress, sig.color,
                              round(float(sig.meta.get('heat', 0) or 0), 2)),
                             is_lit, is_pressed)
                if self._drawn.get(i) == print_key:
                    continue
                img = draw_key(sig, self.profile.key_px, lit=is_lit,
                               pressed=is_pressed)
                native = self._pil.to_native_key_format(
                    self.deck, self._pil.create_scaled_key_image(self.deck, img))
                self.deck.set_key_image(i, native)
                self._drawn[i] = print_key
            if self.profile.screen_px and hasattr(self.deck, "set_screen_image"):
                m = layout.meter
                bar = (layout.summary, layout.page, layout.pages,
                       None if m is None else tuple(sorted(
                           (k, v) for k, v in m.meta.items()
                           if isinstance(v, (str, int, float, bool)))),
                       int(t * 0.5) if m is not None else 0)  # 2 s hatch tick
                if self._drawn.get(-1) != bar:
                    scr = _screen_face(self.profile, layout, t)
                    try:
                        self.deck.set_screen_image(
                            self._pil.to_native_screen_format(self.deck, scr))
                        self._drawn[-1] = bar
                    except Exception:
                        pass
            self._touch_leds(layout, lit)

    def _touch_leds(self, layout: Layout, lit: dict[str, bool]) -> None:
        """Neo touch points are RGB LEDs (set_key_color).

        With multiple pages they are paging arrows: lit amber when a page
        exists in that direction. On a single page they become ambient
        indicators — left is the fleet urgency beacon (red = blocked,
        amber = attention, blinking in phase with the keys; faint blue =
        all working), right tracks the burn meter (cool → hot with frac,
        molten red past target)."""
        if self.profile.touch_points < 2 or not hasattr(self.deck, "set_key_color"):
            return
        # The LEFT point is the urgency beacon, ALWAYS — demoting it to a
        # page arrow right when the fleet is crowded enough to page is
        # exactly backwards. It also covers urgency parked on OTHER pages.
        states = {s.state for s in layout.keys if s}
        urgent_here = "blocked" in states or "attention" in states
        if urgent_here or layout.offpage_urgent:
            on = any(lit.values()) if lit else True
            color = (229, 72, 77) if "blocked" in states else (255, 178, 36)
            left = color if on else (10, 6, 2)
        elif "working" in states:
            left = (8, 20, 46)
        else:
            left = (0, 0, 0)
        # RIGHT: pager when there is anywhere to go, burn gauge otherwise.
        if layout.pages > 1:
            right = (70, 52, 14)
        else:
            m = layout.meter
            if m is not None:
                frac = float(m.meta.get("frac", 0.0))
                if frac > 1.0:
                    right = (229, 60, 40)
                else:
                    from .meter import RAMP_NORMAL, _lerp3
                    right = tuple(round(c * 0.45)
                                  for c in _lerp3(RAMP_NORMAL, min(1.0, frac)))
            else:
                right = (0, 0, 0)
        if self._leds == (left, right):
            return
        try:
            self.deck.set_key_color(self.profile.keys, *left)
            self.deck.set_key_color(self.profile.keys + 1, *right)
            self._leds = (left, right)
        except Exception:
            pass

    def close(self) -> None:
        try:
            with self._lock:
                self.deck.reset()
                self.deck.close()
        except Exception:
            pass
