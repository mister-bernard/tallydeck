"""Terminal renderer: the deck as an ANSI grid, for quick checks over SSH."""

from __future__ import annotations

from ..devices import DeviceProfile
from ..signal import Signal
from ..view import Layout

_ANSI = {
    "blocked": "\033[97;41m",     # white on red
    "attention": "\033[30;43m",   # black on yellow
    "working": "\033[97;44m",     # white on blue
    "success": "\033[30;42m",     # black on green
    "idle": "\033[90;100m",       # gray
    "offline": "\033[90;40m",
}
_RESET = "\033[0m"
_W = 16  # inner cell width


def _cell_lines(sig: Signal | None, lit: bool) -> list[str]:
    if sig is None:
        return [" " * _W, f"{'·':^{_W}}", " " * _W]
    color = _ANSI.get(sig.state, _ANSI["idle"])
    if lit:
        color = "\033[7m" + color   # inverse video = flash frame
    label = sig.label[:_W - 2]
    sub = sig.sublabel[:_W - 2]
    if sig.progress is not None:
        filled = round((_W - 4) * sig.progress)
        bar = "▰" * filled + "▱" * ((_W - 4) - filled)
        foot = f" {bar} "
    else:
        foot = " " * _W
    return [
        f"{color} {label:<{_W - 2}} {_RESET}",
        f"{color} {sub:<{_W - 2}} {_RESET}",
        f"{color} {foot:<{_W - 2}.{_W - 2}} {_RESET}",
    ]


def render_term(profile: DeviceProfile, layout: Layout,
                lit: dict[str, bool] | None = None) -> str:
    lit = lit or {}
    rows_out: list[str] = []
    for r in range(profile.rows):
        cells = []
        for c in range(profile.cols):
            sig = layout.keys[r * profile.cols + c]
            cells.append(_cell_lines(sig, bool(sig and lit.get(sig.id))))
        for line_i in range(3):
            rows_out.append("  ".join(cell[line_i] for cell in cells))
        rows_out.append("")
    pager = f"  page {layout.page + 1}/{layout.pages}" if layout.pages > 1 else ""
    rows_out.append(f"\033[2m{layout.summary}{pager}\033[0m")
    return "\n".join(rows_out)
