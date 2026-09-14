"""Device profiles: geometry the view + renderers share.

Numbers here describe the *drawing surface*; the hardware backend maps
them onto the real device (and asserts they match at open time).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    rows: int
    cols: int
    key_px: int                 # keys are square
    screen_px: tuple[int, int] | None = None   # secondary screen (w, h)
    touch_points: int = 0       # capacitive points (Neo: page left/right)

    @property
    def keys(self) -> int:
        return self.rows * self.cols


NEO = DeviceProfile("neo", rows=2, cols=4, key_px=96,
                    screen_px=(248, 58), touch_points=2)
MK2 = DeviceProfile("mk2", rows=3, cols=5, key_px=72)
# A real Stream Deck Mini reports 80x80 key images, not 72. At 72 the
# (rows, cols, key_px) lookup in deckdev._profile_for never matches, so
# every real Mini fell through to the synthetic "detected" profile.
MINI = DeviceProfile("mini", rows=2, cols=3, key_px=80)
XL = DeviceProfile("xl", rows=4, cols=8, key_px=96)
# Stream Deck +: 8 keys at 120px, an 800x100 strip, and four dials. The dials
# are unwired on purpose — a dial that pages is a habit and a dial that acts
# is a trick, and neither has earned a key yet. The strip is the deck's
# TOUCHSCREEN: on this model screen_image_format() answers (0, 0) and
# set_screen_image() is a silent no-op, so the Neo's info-bar path would draw
# nothing and raise nothing. deckdev._strip_of resolves which API it speaks.
PLUS = DeviceProfile("plus", rows=2, cols=4, key_px=120, screen_px=(800, 100))

PROFILES = {p.name: p for p in (NEO, MK2, MINI, XL, PLUS)}
