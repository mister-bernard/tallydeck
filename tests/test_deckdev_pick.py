"""Which attached deck to drive, decided without opening anything.

`decks[0]` was the whole rule. The moment two decks share a hub that is a
coin toss: hidapi lists them in USB order, which nobody chose. These tests
pin the rule that replaced it, on fakes shaped like python-elgato-streamdeck
devices (class constants only — geometry needs no open()).
"""

from tallydeck.devices import PROFILES
from tallydeck.render.deckdev import (_pick_deck, _profile_for, _serial_match,
                                      _strip_of)


class FakeDeck:
    def __init__(self, kind, rows, cols, px, screen=(0, 0), touch=None,
                 serial="?"):
        self.kind, self._rows, self._cols, self._px = kind, rows, cols, px
        self._screen, self._touch, self.serial = screen, touch, serial
        self.opened = 0

    def deck_type(self):            return self.kind
    def key_count(self):            return self._rows * self._cols
    def key_layout(self):           return (self._rows, self._cols)
    def key_image_format(self):     return {"size": (self._px, self._px)}
    def screen_image_format(self):  return {"size": self._screen, "format": ""}
    def is_touch(self):             return self._touch is not None
    def touchscreen_image_format(self):
        return {"size": self._touch, "format": "JPEG"}
    # serial needs a live handle on real hardware; count the opens
    def open(self):                 self.opened += 1
    def close(self):                pass
    def get_serial_number(self):    return self.serial


def mini(serial="MINI1"):
    return FakeDeck("Stream Deck Mini", 2, 3, 80, serial=serial)


def plus(serial="PLUS1"):
    # Real Plus: screen_image_format answers (0, 0); the strip is the touchscreen.
    return FakeDeck("Stream Deck +", 2, 4, 120, screen=(0, 0),
                    touch=(800, 100), serial=serial)


def neo():
    return FakeDeck("Stream Deck Neo", 2, 4, 96, screen=(248, 58))


# ── profiles ─────────────────────────────────────────────────────────────────

def test_plus_resolves_to_the_plus_profile_with_the_touch_strip_as_its_screen():
    p = _profile_for(plus())
    assert p is PROFILES["plus"]
    assert (p.rows, p.cols, p.key_px) == (2, 4, 120)
    assert p.screen_px == (800, 100)


def test_plus_strip_is_the_touchscreen_api_not_the_screen_api():
    assert _strip_of(plus()) == ("touch", (800, 100))
    assert _strip_of(neo()) == ("screen", (248, 58))
    assert _strip_of(mini()) == (None, None)


def test_mini_still_has_no_screen():
    # The (0, 0) screen that crashed every frame this morning must stay absent.
    assert _profile_for(mini()).screen_px is None


# ── picking ──────────────────────────────────────────────────────────────────

def test_profile_match_beats_usb_order():
    m, p = mini(), plus()
    deck, why = _pick_deck([m, p], want="plus")
    assert deck is p
    assert "plus" in why
    assert p.opened == 0 and m.opened == 0     # geometry needs no open


def test_serial_wins_over_profile_and_order():
    a, b = plus("PLUS-A"), plus("PLUS-B")
    deck, why = _pick_deck([a, b], want="plus", serial="PLUS-B")
    assert deck is b
    assert "PLUS-B" in why


def test_serial_not_attached_falls_back_to_profile_and_says_so():
    m, p = mini(), plus("PLUS-A")
    deck, why = _pick_deck([m, p], want="plus", serial="NOPE")
    assert deck is p
    assert "NOPE" in why and "not attached" in why


def test_no_profile_match_takes_first_deck_loudly():
    m = mini()
    deck, why = _pick_deck([m], want="plus")
    assert deck is m
    assert "no plus" in why


def test_serial_probe_skips_decks_of_the_wrong_shape():
    # Reading a serial means opening the device; do not open the Mini to
    # look for a Plus serial.
    m, p = mini(), plus("PLUS-A")
    _pick_deck([m, p], want="plus", serial="PLUS-A")
    assert m.opened == 0 and p.opened == 1


def test_serial_pin_in_usb_descriptor_form_matches_the_hid_form():
    # USB descriptor: A00XX1234ABCD — get_serial_number(): XX1234ABCD.
    # A pin copied from the descriptor must still match the trimmed form.
    m, p = mini(), plus("XX1234ABCD")
    deck, why = _pick_deck([m, p], want="plus", serial="A00XX1234ABCD")
    assert deck is p
    assert "A00XX1234ABCD" in why and "not attached" not in why


def test_serial_match_is_case_blind_and_refuses_short_tails():
    assert _serial_match("XX1234ABCD", "a00xx1234abcd")
    assert _serial_match("A00XX1234ABCD", "XX1234ABCD")
    assert not _serial_match("XX1234ABCD", "CD")
    assert not _serial_match(None, "XX1234ABCD")


def test_png_render_keeps_a_strip_wider_than_the_key_field_inside_the_image():
    # The Plus strip (800px) is wider than its 4x120 key field. The PNG face
    # centred the strip on the key field and pasted it at a NEGATIVE x, so the
    # "pixel-true render" cut off the left third of the strip — an artifact of
    # the render, not the device — and a verifier reading the PNG would chase
    # a clipping bug that does not exist on the hardware.
    from tallydeck.render.png import render_png
    from tallydeck.view import Layout
    layout = Layout(keys=[None] * 8, page=0, pages=1, summary="1 needs you",
                    mural=False)
    img = render_png(PROFILES["plus"], layout, scale=1)
    assert img.size[0] >= 800
