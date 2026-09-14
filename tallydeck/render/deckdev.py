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

import json
import sys
import threading

from ..devices import DeviceProfile, PROFILES
from ..view import Layout
from .keycard import draw_key
from .png import _screen_face


def _strip_of(deck) -> tuple[str | None, tuple[int, int] | None]:
    """Which API the deck's secondary strip speaks, and its size.

    ("screen", (w, h)) for a Neo-style info bar, ("touch", (w, h)) for a
    Stream Deck + touch strip, (None, None) for a keys-only deck.

    Presence is not capability. A screenless Mini still answers
    screen_image_format() — with {'size': (0, 0)} — and (0, 0) is TRUTHY, so
    testing for the key recorded a screen that did not exist and show() then
    died in Pillow on a font of size 0. The Plus fails the same probe the
    other way round: its (0, 0) screen is honest, but its strip lives behind
    touchscreen_image_format(), so a probe that stops at the first answer
    leaves the strip dark for the life of the process. Require a non-zero
    area, and ask both. Class constants only — no open() needed.
    """
    for api, fmt in (("screen", "screen_image_format"),
                     ("touch", "touchscreen_image_format")):
        if api == "touch" and not getattr(deck, "is_touch", lambda: False)():
            continue
        if not hasattr(deck, fmt):
            continue
        try:
            f = getattr(deck, fmt)()
        except Exception:
            continue
        size = (f or {}).get("size")
        if size and all(size):
            return api, tuple(size)
    return None, None


def _profile_for(deck) -> DeviceProfile:
    """Derive a DeviceProfile from the device. Geometry is class constants in
    python-elgato-streamdeck, so this needs no open()."""
    kf = deck.key_image_format()
    size = kf["size"][0] if kf and kf.get("size") else 96
    layout = deck.key_layout() if hasattr(deck, "key_layout") else None
    rows, cols = layout if layout else {
        6: (2, 3), 8: (2, 4), 15: (3, 5), 32: (4, 8),
    }.get(deck.key_count(), (1, deck.key_count()))
    _, screen = _strip_of(deck)
    for p in PROFILES.values():
        if (p.rows, p.cols, p.key_px) == (rows, cols, size):
            return p
    return DeviceProfile("detected", rows=rows, cols=cols, key_px=size,
                         screen_px=screen)


def _serial_of(deck) -> str | None:
    """The serial is a feature report, so it needs a live handle: open, read,
    close. Only ever called on decks already the right shape."""
    try:
        deck.open()
        try:
            return str(deck.get_serial_number()).strip()
        finally:
            deck.close()
    except Exception:
        return None


def _serial_match(reported: str | None, wanted: str) -> bool:
    """The USB descriptor may say A00XX1234ABCD while the deck's own feature
    report says XX1234ABCD — python-elgato-streamdeck trims a model prefix on
    some models. Both name one unit, so a pin copied from `system_profiler` must
    still match what the library reports: equal, or one is a suffix of the
    other (at least 8 chars, so a short tail cannot claim two decks)."""
    if not reported or not wanted:
        return False
    a, b = reported.strip().upper(), wanted.strip().upper()
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 8 and long_.endswith(short)


def _pick_deck(decks: list, want: str | None = None,
               serial: str | None = None, serial_of=_serial_of):
    """Choose the deck to drive. Returns (deck, reason).

    The reason is logged, because a wrong pick is otherwise invisible: both
    decks light up *something*, and until now the daemon's log said nothing
    at all about which device it held.

    `decks[0]` was the whole rule. With two decks on one hub that is a coin
    toss — hidapi lists them in USB order, which nobody chose. In order:
      1. the configured serial, if attached — the only name that survives a
         replug, and the only way to tell two decks of one model apart;
      2. the deck whose geometry matches the configured profile — a Mini and
         a Plus differ in key count and pixel size, decided from class
         constants without opening anything;
      3. the first deck, out loud — never silently.
    A serial that is not attached falls through to 2, and says so.
    """
    if not decks:
        raise SystemExit(
            "no Stream Deck found (is the Elgato app running? quit it — "
            "it holds the device exclusively)")
    shaped = [d for d in decks
              if want is None or _profile_for(d).name == want]
    note = ""
    if serial:
        for d in shaped:
            if _serial_match(serial_of(d), serial):
                return d, f"serial {serial}"
        note = f"serial {serial} not attached; "
    if shaped:
        return shaped[0], f"{note}profile {want or _profile_for(shaped[0]).name}"
    kinds = ", ".join(d.deck_type() for d in decks)
    return decks[0], f"{note}no {want} attached (have: {kinds}); using first"


class DeckSurface:
    def __init__(self, preferred: str = "neo", brightness: int = 80,
                 serial: str | None = None):
        try:
            from StreamDeck.DeviceManager import DeviceManager
            from StreamDeck.ImageHelpers import PILHelper
        except ImportError as e:
            raise SystemExit(
                "streamdeck library not installed — pip install -e '.[deck]' "
                "(and `brew install hidapi` on macOS)") from e
        self._pil = PILHelper
        decks = DeviceManager().enumerate()
        self.deck, why = _pick_deck(decks, want=preferred, serial=serial)
        self.deck.open()
        self.deck.reset()
        self.deck.set_brightness(brightness)
        self.profile = _profile_for(self.deck)
        self._strip, _ = _strip_of(self.deck)
        self._strip_err = False   # log a strip-write failure ONCE, not never
        try:
            sn = str(self.deck.get_serial_number()).strip()
        except Exception:
            sn = "?"
        strip = (f"{self._strip} {self.profile.screen_px[0]}x{self.profile.screen_px[1]}"
                 if self._strip and self.profile.screen_px else "none")
        # The one line the log never had: which device this process holds.
        print(f"tallydeck: deck opened: {self.deck.deck_type()} serial={sn} "
              f"keys={self.profile.keys} profile={self.profile.name} "
              f"strip={strip} — {why}; {len(decks)} attached",
              file=sys.stderr, flush=True)
        self._lock = threading.Lock()
        self._drawn: dict[int, tuple] = {}   # key index → content fingerprint
        self._leds: tuple | None = None
        self._on_key = None
        self._on_touch = None
        self._on_dial = None
        self.deck.set_key_callback(self._key_event)
        # Stream Deck +: four push-turn dials. Registered defensively — a deck
        # without dials never fires it, a library without it never breaks us.
        try:
            if (getattr(self.deck, "dial_count", lambda: 0)() > 0
                    and hasattr(self.deck, "set_dial_callback")):
                self.deck.set_dial_callback(self._dial_event)
        except Exception:
            pass
        if hasattr(self.deck, "set_touchscreen_callback"):
            try:
                self.deck.set_touchscreen_callback(self._touch_event)
            except Exception:
                pass

    # ── events ───────────────────────────────────────────────────────────────

    def set_callbacks(self, on_key=None, on_touch=None, on_dial=None) -> None:
        self._on_key = on_key
        self._on_touch = on_touch
        self._on_dial = on_dial

    def _dial_event(self, _deck, dial, event, value) -> None:
        """python-elgato-streamdeck: (deck, dial, DialEventType, value) —
        TURN carries signed detents, PUSH a bool. Matched by name so this
        file never imports the enum (same defensive shape as the touch path).
        A push is deliberately ignored here: applying a preview is a gated
        act that lands in a later step, not a side effect of the driver."""
        name = str(getattr(event, "name", event)).upper()
        if "TURN" in name and self._on_dial:
            try:
                self._on_dial(int(dial), int(value))
            except Exception:
                pass

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
             t: float = 0.0, pressed: frozenset = frozenset(),
             fx: dict | None = None) -> None:
        fx = fx or {}
        with self._lock:
            askpage = int(t / 2)
            if layout.mural:
                from . import mural
                imgs = mural.tiles(self.profile, t)
                for i, img in enumerate(imgs[:self.profile.keys]):
                    key = ("mural", i, int(t) % 2, i in pressed)
                    if self._drawn.get(i) == key:
                        continue
                    native = self._pil.to_native_key_format(
                        self.deck,
                        self._pil.create_scaled_key_image(self.deck, img))
                    self.deck.set_key_image(i, native)
                    self._drawn[i] = key
            for i, sig in enumerate(
                    [] if layout.mural else layout.keys[:self.profile.keys]):
                is_lit = bool(sig and lit.get(sig.id))
                is_pressed = i in pressed
                paged = bool(sig and sig.state in ("attention", "blocked")
                             and len(sig.sublabel) > 55)   # ~2+ pages
                # Skip HID writes for unchanged keys — flashing 2 of 8 keys
                # should cost 2 updates per frame, not 8.
                fxp = int(fx[i] * 14) if i in fx else -1
                print_key = (None if sig is None else
                             (sig.id, sig.state, sig.label, sig.sublabel,
                              sig.progress, sig.color,
                              round(float(sig.meta.get('heat', 0) or 0), 2)),
                             is_lit, is_pressed, askpage if paged else 0, fxp)
                if self._drawn.get(i) == print_key:
                    continue
                img = draw_key(sig, self.profile.key_px, lit=is_lit,
                               pressed=is_pressed,
                               askpage=askpage if paged else 0)
                if fxp >= 0 and sig is not None:
                    # `theme` was referenced two lines down without ever being
                    # imported, so the FIRST press crash-looped the whole client
                    # (NameError, G 2026-09-08). Import it beside fireworks, in the
                    # same lazy spot, so the fix cannot drift away from its use.
                    from . import theme
                    from .fx import fireworks
                    img = fireworks(img, fx[i], sig.color or theme.STATE_COLOR.get(
                        sig.state, theme.STATE_COLOR["idle"]), seed=sig.id)
                native = self._pil.to_native_key_format(
                    self.deck, self._pil.create_scaled_key_image(self.deck, img))
                self.deck.set_key_image(i, native)
                self._drawn[i] = print_key
            if self.profile.screen_px and self._strip:
                m = layout.meter
                z = layout.zones
                if z is None:
                    zkey = None
                else:
                    from .zones import is_stale
                    zkey = (json.dumps(z.meta.get("zones"), sort_keys=True,
                                       default=str), is_stale(z))
                bar = (layout.summary, layout.page, layout.pages,
                       None if m is None else tuple(sorted(
                           (k, v) for k, v in m.meta.items()
                           if isinstance(v, (str, int, float, bool)))),
                       int(t * 0.5) if m is not None else 0,  # 2 s hatch tick
                       zkey)
                if self._drawn.get(-1) != bar:
                    scr = _screen_face(self.profile, layout, t)
                    try:
                        if self._strip == "touch":
                            # Stream Deck +: the strip is the touchscreen;
                            # set_screen_image() is a no-op on this model.
                            # The size MUST be explicit — the library defaults
                            # width/height to 0 and its bounds check raises
                            # IndexError("Invalid draw width 0") on that, an
                            # error this block used to swallow: black strip on
                            # the hardware while the PNG render showed captions.
                            w, h = self.profile.screen_px
                            self.deck.set_touchscreen_image(
                                self._pil.to_native_touchscreen_format(
                                    self.deck, scr),
                                x_pos=0, y_pos=0, width=w, height=h)
                        else:
                            self.deck.set_screen_image(
                                self._pil.to_native_screen_format(self.deck, scr))
                        self._drawn[-1] = bar
                    except Exception as e:
                        # A refusal that cannot name itself reads as a dead
                        # instrument. Say it once per process, then stay quiet.
                        if not self._strip_err:
                            self._strip_err = True
                            print(f"tallydeck: strip write failed: {e!r}",
                                  file=sys.stderr, flush=True)
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
