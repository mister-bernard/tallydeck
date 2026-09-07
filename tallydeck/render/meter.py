"""Meter: a 1980s demoscene-style burn bar for the Neo info screen.

Synthwave gradient fill, moving diagonal hatch, scanlines, chrome bevel,
leading-edge glow — and it still has a job: `frac` is progress toward the
*session burn target*, the text says when the window ends. Past 100% the
gradient goes molten (amber→red): you hit the target, ease off or enjoy.

Drawn at 2× and downsampled like every other surface. `t` (epoch seconds)
drives the hatch scroll; callers quantize it so re-renders stay cheap.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFilter

from . import theme

SS = 2

# Synthwave ramp under target, molten ramp past it.
RAMP_NORMAL = ("#00E5FF", "#7A5CFF", "#FF3DF2")
RAMP_OVER = ("#FFB224", "#FF5C33", "#E5484D")
BG = "#05060A"
GRID = "#101320"


def _lerp3(ramp: tuple[str, str, str], t: float) -> tuple[int, int, int]:
    a, b, c = ramp
    if t < 0.5:
        return theme.mix(a, b, t * 2)
    return theme.mix(b, c, (t - 0.5) * 2)


def draw_meter(size: tuple[int, int], frac: float, left: str = "",
               mid: str = "", right: str = "", t: float = 0.0) -> Image.Image:
    w, h = size[0] * SS, size[1] * SS
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    # Under target: synthwave fill 0→100%. Past target: the bar refills in
    # molten colors over the spent (dimmed) synth base.
    over = frac > 1.0
    fill_frac = min(1.0, frac - 1.0 if over else max(0.0, frac))

    # ── scanlines across the whole face ─────────────────────────────────────
    for y in range(0, h, 4 * SS // 2):
        d.line([(0, y), (w, y)], fill=GRID, width=1)

    # ── bar geometry ────────────────────────────────────────────────────────
    inset = round(h * 0.12)
    x0, x1 = inset, w - inset
    y0, y1 = inset, h - inset
    inner_w = x1 - x0

    # track with bevel
    d.rounded_rectangle([x0, y0, x1, y1], radius=(y1 - y0) // 2,
                        fill="#0B0E16", outline="#1C2030", width=SS)

    bar = Image.new("RGB", (inner_w, y1 - y0), "#0B0E16")
    bd = ImageDraw.Draw(bar)

    def paint(frac_w: float, ramp_, dim: float = 1.0):
        fw = round(inner_w * frac_w)
        for x in range(fw):
            r, g, b = _lerp3(ramp_, x / max(1, inner_w - 1))
            bd.line([(x, 0), (x, y1 - y0)],
                    fill=(round(r * dim), round(g * dim), round(b * dim)))

    if over:
        paint(1.0, RAMP_NORMAL, dim=0.35)      # the spent target, dimmed
        paint(fill_frac, RAMP_OVER)
    else:
        paint(fill_frac, RAMP_NORMAL)

    fw = round(inner_w * fill_frac)

    # moving diagonal hatch over the fill
    if fw > 0:
        hatch = Image.new("L", (fw, y1 - y0), 0)
        hd = ImageDraw.Draw(hatch)
        period = 14 * SS
        offset = int(t * 10 * SS) % period
        for xx in range(-(y1 - y0), fw + period, period):
            x_a = xx + offset
            hd.line([(x_a, y1 - y0), (x_a + (y1 - y0), 0)],
                    fill=46, width=4 * SS)
        white = Image.new("RGB", (fw, y1 - y0), "#FFFFFF")
        bar.paste(white, (0, 0), hatch)

        # leading-edge glow: soft bloom behind a hot line
        head = Image.new("L", bar.size, 0)
        ImageDraw.Draw(head).line([(fw - 1, 0), (fw - 1, y1 - y0)],
                                  fill=200, width=4 * SS)
        head = head.filter(ImageFilter.GaussianBlur(4 * SS))
        bar.paste(Image.new("RGB", bar.size, "#DCEBFF"), (0, 0), head)
        bar.paste("#F5FBFF", (max(0, fw - SS * 2), 0,
                              min(fw + SS, inner_w), y1 - y0))

    # chrome: top highlight, bottom shade
    bd.line([(0, SS), (inner_w, SS)], fill=(255, 255, 255), width=1)
    bd.line([(0, (y1 - y0) - SS), (inner_w, (y1 - y0) - SS)],
            fill=(0, 0, 0), width=1)

    # tick notches at quarters
    for q in (0.25, 0.5, 0.75):
        qx = round(inner_w * q)
        bd.line([(qx, 0), (qx, y1 - y0)], fill=(5, 6, 10), width=SS)

    # round the bar ends via mask paste
    mask = Image.new("L", (inner_w, y1 - y0), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, inner_w - 1, (y1 - y0) - 1], radius=(y1 - y0) // 2, fill=255)
    img.paste(bar, (x0, y0), mask)

    # ── text, engraved over everything ──────────────────────────────────────
    f_big = theme.font("display", round(h * 0.28))
    f_mid = theme.font("semibold", round(h * 0.20))
    ty = (h - f_big.size) // 2 - round(h * 0.02)
    pad = round(h * 0.30)
    kw = dict(stroke_width=SS + 1, stroke_fill=(3, 4, 8))
    lw = rw = 0.0
    if left:
        lw = d.textlength(left, font=f_big)
        d.text((x0 + pad, ty), left, font=f_big, fill="#F2F6FF", **kw)
    if right:
        rw = d.textlength(right, font=f_big)
        d.text((x1 - pad - rw, ty), right, font=f_big, fill="#D7DEF2", **kw)
    if mid:  # optional; yields silently when the ends leave no room
        mw = d.textlength(mid, font=f_mid)
        gap_l = x0 + pad + lw + pad
        gap_r = x1 - pad - rw - pad
        if gap_l + mw <= gap_r:
            d.text(((gap_l + gap_r - mw) / 2, (h - f_mid.size) // 2),
                   mid, font=f_mid, fill="#C6CDE4", **kw)

    return img.resize(size, Image.LANCZOS)
