"""Terminal renderer: the deck as an ANSI grid, for quick checks over SSH."""

from __future__ import annotations

from ..devices import DeviceProfile
from ..signal import Signal, is_codex, is_grok
from ..view import Layout

_ANSI = {
    "blocked": "\033[97;41m",     # white on red
    "attention": "\033[30;43m",   # black on yellow
    "working": "\033[97;44m",     # white on blue
    "success": "\033[30;42m",     # black on green
    # 90 and 100 are the SAME palette entry (bright black) as foreground and
    # background — an idle cell rendered its label in exactly its own background
    # colour, so the keys came out as blank grey blocks. Observed 2026-09-07 on a
    # live fleet: the data was arriving fine, it was simply invisible.
    # 37 keeps idle visually quiet without making it unreadable.
    "idle": "\033[37;100m",       # dim white on gray — MUST contrast with bg
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
    # Codex keys carry their bar on the left edge on the hardware; the text
    # surface says the same thing with a left rule, so a check over SSH shows
    # the same fleet the deck does.
    e = "▏" if is_codex(sig) else ("▁" if is_grok(sig) else " ")
    return [
        f"{color}{e}{label:<{_W - 2}} {_RESET}",
        f"{color}{e}{sub:<{_W - 2}} {_RESET}",
        f"{color}{e}{foot:<{_W - 2}.{_W - 2}} {_RESET}",
    ]


def render_term(profile: DeviceProfile, layout: Layout,
                lit: dict[str, bool] | None = None) -> str:
    lit = lit or {}
    if layout.mural:
        from . import mural
        return mural.text(profile) + "\n\n  " + layout.summary
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
    if layout.meter is not None:
        m = layout.meter.meta
        lanes = [l for l in m.get("lanes", []) if isinstance(l, dict)]
        if len(lanes) >= 2:      # one line per account, mirroring the strip
            for l in lanes:
                frac = float(l.get("frac", 0))
                filled = min(_W * 2, round(_W * 2 * frac))
                bar = "▰" * filled + "▱" * max(0, _W * 2 - filled)
                soon = "*" if l.get("id") == m.get("hot") else " "
                rows_out.append(
                    f"\033[95m{l.get('id', '?')} {bar}\033[0m "
                    f"{l.get('mid', '')}  {l.get('clock', '')}{soon}")
        else:
            frac = float(m.get("frac", 0))
            filled = min(_W * 2, round(_W * 2 * frac))
            bar = "▰" * filled + "▱" * max(0, _W * 2 - filled)
            rows_out.append(f"\033[95m{bar}\033[0m {m.get('left', '')} "
                            f"{m.get('mid', '')} {m.get('right', '')}")
    rows_out.append(f"\033[2m{layout.summary}{pager}\033[0m")
    return "\n".join(rows_out)
