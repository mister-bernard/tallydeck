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
MINI = DeviceProfile("mini", rows=2, cols=3, key_px=72)
XL = DeviceProfile("xl", rows=4, cols=8, key_px=96)

PROFILES = {p.name: p for p in (NEO, MK2, MINI, XL)}
