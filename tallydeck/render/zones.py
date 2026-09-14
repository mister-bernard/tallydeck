"""The strip as four captions over the four dials (Stream Deck +).

Each zone names one property of the pane the operator last acted on —
MODEL · STRENGTH · ACTION · BURN — in words, where a coloured key can only
hint. The values arrive as a `meta.zones` signal (from any source — typically a
script through the exec source), so this file knows
nothing about where truth comes from: it draws what it is handed.

Age is drawn, never assumed. A caption that keeps its last value after its
writer died looks exactly like a live one, so a stale signal is dimmed and
labelled, and the hub's ttl removes it entirely a few seconds later.
"""

from __future__ import annotations

import time

from PIL import Image, ImageDraw

from . import theme
from .keycard import SS, _truncate

COLUMNS = 4               # one zone per dial on the Plus
FRESH_FOR = 6.0           # seconds before a zones signal is drawn as stale


def is_stale(sig, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        fresh_for = float(sig.meta.get("fresh_for") or FRESH_FOR)
    except Exception:
        fresh_for = FRESH_FOR
    return (now - float(getattr(sig, "updated", 0) or 0)) > fresh_for


def _lcd(rgb, stale: bool = False):
    """Tartarus colours are LED-dim (max channel ~60): lift to a display
    accent, keeping the hue the source chose. Garbage → the neutral track colour."""
    try:
        r, g, b = [max(0, int(v)) for v in rgb]
    except Exception:
        return theme.TRACK
    m = max(r, g, b) or 1
    k = (110 if stale else 210) / m
    return tuple(min(255, round(c * k)) for c in (r, g, b))


def draw_zones(size: tuple[int, int], zones, t: float = 0.0,
               stale: bool = False) -> Image.Image:
    w, h = size[0] * SS, size[1] * SS
    img = Image.new("RGB", (w, h), theme.BG_EMPTY)
    d = ImageDraw.Draw(img)
    cw = w // COLUMNS
    pad = round(h * 0.10)
    f_title = theme.font("semibold", max(8, round(h * 0.15)))
    f_value = theme.font("display", max(10, round(h * 0.34)))
    f_sub = theme.font("semibold", max(8, round(h * 0.16)))
    fg = theme.FG_DIM if stale else theme.FG
    bar_h = max(SS, round(h * 0.06))
    zones = list(zones or [])
    for i in range(COLUMNS):
        x0 = i * cw
        if i:
            d.line([(x0, pad), (x0, h - pad)], fill=theme.TRACK, width=SS)
        z = zones[i] if i < len(zones) else None
        if not isinstance(z, dict):
            continue
        accent = _lcd(z.get("rgb") or (26, 26, 26), stale)
        pending = z.get("pending")
        if pending:
            # A preview: hollow bar, candidate in the accent colour, "›" —
            # visibly NOT the current value, so a glance cannot mistake a
            # turned dial for an applied change.
            d.rectangle([x0 + pad, 0, x0 + cw - pad, bar_h],
                        outline=accent, width=SS)
        else:
            d.rectangle([x0 + pad, 0, x0 + cw - pad, bar_h], fill=accent)
        maxw = cw - 2 * pad
        title = _truncate(d, str(z.get("title") or "").upper(), f_title, maxw)
        if pending:
            value = _truncate(d, "› " + str(pending), f_value, maxw)
        else:
            value = _truncate(d, str(z.get("value") if z.get("value") is not None
                                     else "—"), f_value, maxw)
        sub = _truncate(d, str(z.get("sub") or ""), f_sub, maxw)
        y = bar_h + round(h * 0.06)
        d.text((x0 + pad, y), title, font=f_title, fill=theme.FG_DIM)
        y += f_title.size + round(h * 0.04)
        d.text((x0 + pad, y), value, font=f_value,
               fill=accent if pending and not stale else fg)
        y += f_value.size + round(h * 0.04)
        d.text((x0 + pad, y), sub, font=f_sub, fill=theme.FG_DIM)
    if stale:
        d.text((w - pad - d.textlength("stale", font=f_sub), pad), "stale",
               font=f_sub, fill=theme.FG_DIM)
    return img.resize(size, Image.LANCZOS)
