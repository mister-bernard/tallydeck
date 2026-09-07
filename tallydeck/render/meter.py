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



def _draw_band(img, d, x0: int, x1: int, y0: int, y1: int,
               frac: float, t: float, label: str = "") -> None:
    """Paint one bar into `img` between y0 and y1. Extracted verbatim from the
    single-bar path so both modes render identically — two bars must not drift
    into looking like a different product from one."""
    inner_w, h_ = x1 - x0, y1 - y0
    if inner_w <= 0 or h_ <= 0:
        return
    over = frac > 1.0
    fill_frac = min(1.0, frac - 1.0 if over else max(0.0, frac))

    d.rounded_rectangle([x0, y0, x1, y1], radius=h_ // 2,
                        fill="#0B0E16", outline="#1C2030", width=SS)
    bar = Image.new("RGB", (inner_w, h_), "#0B0E16")
    bd = ImageDraw.Draw(bar)

    def paint(frac_w: float, ramp_, dim: float = 1.0):
        fw_ = round(inner_w * frac_w)
        for x in range(fw_):
            r, g, b = _lerp3(ramp_, x / max(1, inner_w - 1))
            bd.line([(x, 0), (x, h_)],
                    fill=(round(r * dim), round(g * dim), round(b * dim)))

    if over:
        paint(1.0, RAMP_NORMAL, dim=0.35)
        paint(fill_frac, RAMP_OVER)
    else:
        paint(fill_frac, RAMP_NORMAL)

    fw = round(inner_w * fill_frac)
    if fw > 0:
        hatch = Image.new("L", (fw, h_), 0)
        hd = ImageDraw.Draw(hatch)
        period = 14 * SS
        offset = int(t * 10 * SS) % period
        for xx in range(-h_, fw + period, period):
            x_a = xx + offset
            hd.line([(x_a, h_), (x_a + h_, 0)], fill=46, width=4 * SS)
        bar.paste(Image.new("RGB", (fw, h_), "#FFFFFF"), (0, 0), hatch)

        head = Image.new("L", bar.size, 0)
        ImageDraw.Draw(head).line([(fw - 1, 0), (fw - 1, h_)],
                                  fill=200, width=4 * SS)
        head = head.filter(ImageFilter.GaussianBlur(max(1, h_ // 4)))
        bar.paste(Image.new("RGB", bar.size, "#DCEBFF"), (0, 0), head)
        bar.paste("#F5FBFF", (max(0, fw - SS * 2), 0,
                              min(fw + SS, inner_w), h_))

    bd.line([(0, SS), (inner_w, SS)], fill=(255, 255, 255), width=1)
    bd.line([(0, h_ - SS), (inner_w, h_ - SS)], fill=(0, 0, 0), width=1)
    for q in (0.25, 0.5, 0.75):
        qx = round(inner_w * q)
        bd.line([(qx, 0), (qx, h_)], fill=(5, 6, 10), width=SS)

    mask = Image.new("L", (inner_w, h_), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, inner_w - 1, h_ - 1], radius=h_ // 2, fill=255)
    img.paste(bar, (x0, y0), mask)

    if label:  # tiny account tag riding the left cap of its own lane
        f = theme.font("semibold", max(7, round(h_ * 0.62)))
        ImageDraw.Draw(img).text((x0 + round(h_ * 0.28), y0 + h_ * 0.14),
                                 label, font=f, fill="#0A0C12")


def draw_meter(size: tuple[int, int], frac: float, left: str = "",
               mid: str = "", right: str = "", t: float = 0.0,
               lanes: list[dict] | None = None,
               soonest: str = "") -> Image.Image:
    """One bar per account when `lanes` is given, otherwise a single aggregate.

    The aggregate hid the only thing worth knowing: a combined 20% is fine, a
    combined 20% that is 5% on one account and 35% on the other is not. Each
    lane gets its own fill so the imbalance is visible at a glance.
    """
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

    # Lane geometry. Two accounts → two stacked half-height bars with a hairline
    # gutter; anything else keeps the original single track, so nothing changes
    # for callers that do not supply lanes.
    lane_list = [l for l in (lanes or []) if isinstance(l, dict)]
    bar_x1 = x1
    if len(lane_list) >= 2:
        # Two bars plus engraved text over them is unreadable at 248x58 — the
        # first render had "20%" sitting on top of lane A. Give the bars the
        # left column and the clocks the right; nothing overlaps anything.
        bar_x1 = x0 + round((x1 - x0) * 0.46)
        gap = max(SS, round((y1 - y0) * 0.10))
        span = (y1 - y0 - gap * (len(lane_list) - 1)) / len(lane_list)
        bands = []
        for i, l in enumerate(lane_list):
            top = round(y0 + i * (span + gap))
            bands.append((top, round(top + span), float(l.get("frac", 0.0)), l))
    else:
        bands = [(y0, y1, frac, None)]

    for top, bot, lane_frac, lane in bands:
        _draw_band(img, d, x0, bar_x1, top, bot, lane_frac, t,
                   label=(lane or {}).get("id", ""))

    # ── text, engraved over everything ──────────────────────────────────────
    f_big = theme.font("display", round(h * 0.28))
    f_mid = theme.font("semibold", round(h * 0.20))
    ty = (h - f_big.size) // 2 - round(h * 0.02)
    pad = round(h * 0.30)
    kw = dict(stroke_width=SS + 1, stroke_fill=(3, 4, 8))
    lw = rw = 0.0
    if lane_list and len(lane_list) >= 2:
        left = mid = ""     # the lanes already say this, and say it better
    if left:
        lw = d.textlength(left, font=f_big)
        d.text((x0 + pad, ty), left, font=f_big, fill="#F2F6FF", **kw)
    if right:
        rw = d.textlength(right, font=f_big)
        rx = x1 - pad - rw
        d.text((rx, ty), right, font=f_big, fill="#D7DEF2", **kw)
        # Underline the account that resets FIRST — with two countdowns side by
        # side, which one binds is the whole question, and colour alone would be
        # lost on a 248x58 strip.
        if soonest:
            tok = f"{soonest} "
            i = right.find(tok)
            if i >= 0:
                seg_x = rx + d.textlength(right[:i], font=f_big)
                # Exactly this account's token, not a fixed slice — "A 1:48"
                # and "B 12:05" are different widths and the first version
                # underlined into the neighbour.
                seg = right[i:].split("  ")[0]
                seg_w = d.textlength(seg, font=f_big)
                uy = ty + f_big.size + SS
                d.line([(seg_x, uy), (seg_x + seg_w, uy)],
                       fill="#00E5FF", width=max(1, SS))
    if mid:  # optional; yields silently when the ends leave no room
        mw = d.textlength(mid, font=f_mid)
        gap_l = x0 + pad + lw + pad
        gap_r = x1 - pad - rw - pad
        if gap_l + mw <= gap_r:
            d.text(((gap_l + gap_r - mw) / 2, (h - f_mid.size) // 2),
                   mid, font=f_mid, fill="#C6CDE4", **kw)

    return img.resize(size, Image.LANCZOS)
