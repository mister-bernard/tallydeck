"""Keycard: draw one key face (and the Neo info bar) with PIL.

Everything renders at 2× and downsamples for clean anti-aliasing on the
tiny LCDs. The same drawing serves the hardware, the PNG contact sheet,
and the terminal preview — one visual truth.

Anatomy of a key (96 px nominal):

    ┌──────────────┐
    │▔▔▔▔▔▔▔▔▔▔▔▔▔▔│  ← tally bar (state color)
    │ arb bot      │  ← label (Inter Display Bold)
    │ awaiting ack │  ← sublabel (dim)
    │              │
    │ ▂▂▂▂▂▂▂▁▁▁▁▁ │  ← progress (when known)
    └──────────────┘

Flashing keys alternate with a "flood" frame: the whole face fills with
the state color and the text inverts — unmissable in peripheral vision.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from ..signal import Signal
from . import theme

SS = 2  # supersample factor


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if not text or draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def draw_key(sig: Signal | None, px: int, lit: bool = False,
             pressed: bool = False) -> Image.Image:
    """Render one key face at px × px. `lit` = flash flood frame,
    `pressed` = finger currently down (bright ring, instant feedback)."""
    s = px * SS
    img = Image.new("RGB", (s, s), theme.BG_EMPTY)
    d = ImageDraw.Draw(img)

    if sig is None:
        r = max(2, s // 32)
        d.ellipse([(s - r) // 2, (s - r) // 2,
                   (s + r) // 2, (s + r) // 2], fill=theme.TRACK)
        if pressed:
            _press_ring(d, s, theme.FG_DIM)
        return img.resize((px, px), Image.LANCZOS)

    color = sig.color or theme.STATE_COLOR.get(sig.state, theme.STATE_COLOR["idle"])
    muted = sig.state in theme.MUTED_STATES

    if lit:
        bg, fg, sub = color, theme.FLOOD_TEXT, theme.FLOOD_TEXT
        bar = theme.mix(color, "#000000", 0.35)
        track = theme.mix(color, "#000000", 0.25)
        fill = theme.FLOOD_TEXT
    else:
        bg = theme.BG
        fg = theme.FG_DIM if muted else theme.FG
        sub = theme.FG_DIM
        bar = theme.mix(color, theme.BG, 0.55) if muted else color
        track = theme.TRACK
        fill = color

    d.rectangle([0, 0, s, s], fill=bg)

    # tally bar
    bar_h = round(s * 0.085)
    d.rectangle([0, 0, s, bar_h], fill=bar)

    pad = round(s * 0.10)
    f_label = theme.font("display", round(s * 0.195))
    f_sub = theme.font("regular", round(s * 0.135))

    label = _truncate(d, sig.label, f_label, s - 2 * pad)
    y = bar_h + round(s * 0.10)
    d.text((pad, y), label, font=f_label, fill=fg)

    if sig.sublabel:
        y2 = y + round(s * 0.24)
        d.text((pad, y2), _truncate(d, sig.sublabel, f_sub, s - 2 * pad),
               font=f_sub, fill=sub)

    # progress
    if sig.progress is not None:
        h = round(s * 0.07)
        y0 = s - round(s * 0.145)
        x0, x1 = pad, s - pad
        d.rounded_rectangle([x0, y0, x1, y0 + h], radius=h // 2, fill=track)
        w = round((x1 - x0) * sig.progress)
        if w > h:  # avoid a smeared nub at ~0%
            d.rounded_rectangle([x0, y0, x0 + w, y0 + h],
                                radius=h // 2, fill=fill)

    if pressed:
        _press_ring(d, s, theme.FLOOD_TEXT if lit else "#F2F6FF")

    return img.resize((px, px), Image.LANCZOS)


def _press_ring(d: ImageDraw.ImageDraw, s: int, color) -> None:
    w = max(2, round(s * 0.045))
    d.rounded_rectangle([w // 2, w // 2, s - 1 - w // 2, s - 1 - w // 2],
                        radius=round(s * 0.09), outline=color, width=w)


def draw_screen(size: tuple[int, int], summary: str,
                page: int = 0, pages: int = 1) -> Image.Image:
    """Neo info bar: fleet summary left, page dots right."""
    w, h = size[0] * SS, size[1] * SS
    img = Image.new("RGB", (w, h), theme.BG_EMPTY)
    d = ImageDraw.Draw(img)

    pad = round(h * 0.30)

    dots_w = 0
    if pages > 1:
        r = max(3, h // 12)
        gap = r * 3
        dots_w = pages * gap + pad
        cx = w - pad - (pages - 1) * gap
        cy = h // 2
        for i in range(pages):
            fillc = theme.FG if i == page else theme.TRACK
            x = cx + i * gap
            d.ellipse([x - r, cy - r, x + r, cy + r], fill=fillc)

    # Fit the summary: shrink before truncating.
    max_w = w - 2 * pad - dots_w
    for factor in (0.42, 0.36, 0.31, 0.26, 0.22):
        f = theme.font("semibold", round(h * factor))
        if d.textlength(summary, font=f) <= max_w:
            break
    text = _truncate(d, summary, f, max_w)
    d.text((pad, (h - f.size) // 2 - round(h * 0.04)), text,
           font=f, fill=theme.FG_DIM)
    return img.resize(size, Image.LANCZOS)
