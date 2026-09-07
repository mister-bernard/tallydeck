"""Software surfaces: terminal and PNG-file implementations of the
surface interface (`show(layout, lit)` / optional callbacks / close)."""

from __future__ import annotations

import sys
from pathlib import Path

from ..devices import DeviceProfile
from ..view import Layout
from .png import render_png
from .term import render_term


class TermSurface:
    def __init__(self, profile: DeviceProfile):
        self.profile = profile

    def show(self, layout: Layout, lit: dict[str, bool]) -> None:
        sys.stdout.write("\033[2J\033[H")   # clear + home
        sys.stdout.write(render_term(self.profile, layout, lit) + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        pass


class PngSurface:
    """Writes the deck face to a PNG on every change (atomic replace)."""

    def __init__(self, profile: DeviceProfile, path: str, scale: int = 2):
        self.profile = profile
        self.path = Path(path)
        self.scale = scale

    def show(self, layout: Layout, lit: dict[str, bool]) -> None:
        img = render_png(self.profile, layout, lit, scale=self.scale)
        tmp = self.path.with_suffix(".tmp.png")
        img.save(tmp)
        tmp.replace(self.path)

    def close(self) -> None:
        pass
