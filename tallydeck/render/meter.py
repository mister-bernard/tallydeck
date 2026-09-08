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
               frac: float, t: float, label: str = "",
               target: float | None = None) -> None:
    """Paint one bar into `img` between y0 and y1. Extracted verbatim from the
    single-bar path so both modes render identically — two bars must not drift
    into looking like a different product from one.

    With `target`, the fill is a fraction of the whole bar (so it matches
    whatever number is engraved on it), the target sits on the bar as a
    bright notch, and only the portion PAST the notch goes molten. Without
    it, legacy semantics: frac is progress toward target, >1 refills molten.
    """
    inner_w, h_ = x1 - x0, y1 - y0
    if inner_w <= 0 or h_ <= 0:
        return
    if target is not None:
        fill_frac = min(1.0, max(0.0, frac))
        over = fill_frac > target
    else:
        over = frac > 1.0
        fill_frac = min(1.0, frac - 1.0 if over else max(0.0, frac))

    d.rounded_rectangle([x0, y0, x1, y1], radius=h_ // 2,
                        fill="#0B0E16", outline="#1C2030", width=SS)
    bar = Image.new("RGB", (inner_w, h_), "#0B0E16")
    bd = ImageDraw.Draw(bar)

    def paint(frac_w: float, ramp_, dim: float = 1.0, start: float = 0.0):
        for x in range(round(inner_w * start), round(inner_w * frac_w)):
            r, g, b = _lerp3(ramp_, x / max(1, inner_w - 1))
            bd.line([(x, 0), (x, h_)],
                    fill=(round(r * dim), round(g * dim), round(b * dim)))

    if target is not None:
        paint(min(fill_frac, target), RAMP_NORMAL)
        if over:  # only the excess burns molten
            paint(fill_frac, RAMP_OVER, start=target)
    elif over:
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

    if target is not None and 0.0 < target < 1.0:
        # The burn target, as a bright notch ON the bar. Amber past it.
        nx = round(inner_w * target)
        bd.line([(nx, 0), (nx, h_)], fill=(255, 178, 36), width=SS * 2)

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
               hot: str = "") -> Image.Image:
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
    if len(lane_list) >= 2:
        # Full-width stacked lanes: every pixel of the strip is
        # bar. Text is ENGRAVED on the lanes with a dark stroke — badge on
        # the left cap, useful info in the middle, the countdown embedded at
        # the right end of its own bar.
        gap = max(SS, round((y1 - y0) * 0.10))
        span = (y1 - y0 - gap * (len(lane_list) - 1)) / len(lane_list)
        bands = []
        for i, l in enumerate(lane_list):
            top = round(y0 + i * (span + gap))
            bands.append((top, round(top + span), float(l.get("frac", 0.0)), l))
    else:
        bands = [(y0, y1, frac, None)]

    for top, bot, lane_frac, lane in bands:
        tgt = lane.get("target") if lane else None
        _draw_band(img, d, x0, x1, top, bot, lane_frac, t,
                   target=float(tgt) if tgt else None)

    if len(lane_list) >= 2:
        pad_s = round(h * 0.14)
        span_h = bands[0][1] - bands[0][0]
        f_badge = theme.font("display", max(8, round(span_h * 0.60)))
        f_info = theme.font("semibold", max(8, round(span_h * 0.52)))
        kw = dict(stroke_width=SS + 1, stroke_fill=(3, 4, 8))
        for top, bot, _, lane in bands:
            if not lane:
                continue
            cy_b = top + (bot - top - f_badge.size) // 2 - SS
            cy_i = top + (bot - top - f_info.size) // 2 - SS
            d.text((x0 + pad_s, cy_b), str(lane.get("id", "")),
                   font=f_badge, fill="#F2F6FF", **kw)
            midtxt = str(lane.get("mid", ""))
            if midtxt:
                mw = d.textlength(midtxt, font=f_info)
                d.text(((w - mw) / 2, cy_i), midtxt, font=f_info,
                       fill="#E8ECF8", **kw)
            clock = str(lane.get("clock", ""))
            if clock:
                is_hot = hot and lane.get("id") == hot
                cw = d.textlength(clock, font=f_info)
                cx = x1 - pad_s - cw
                d.text((cx, cy_i), clock, font=f_info,
                       fill="#00E5FF" if is_hot else "#D7DEF2", **kw)
                if is_hot:   # underline = the account burning right now
                    uy = cy_i + f_info.size + SS
                    d.line([(cx, uy), (cx + cw, uy)],
                           fill="#00E5FF", width=max(1, SS))

    # ── text, engraved over everything ──────────────────────────────────────
    f_big = theme.font("display", round(h * 0.28))
    f_mid = theme.font("semibold", round(h * 0.20))
    ty = (h - f_big.size) // 2 - round(h * 0.02)
    pad = round(h * 0.30)
    kw = dict(stroke_width=SS + 1, stroke_fill=(3, 4, 8))
    lw = rw = 0.0
    if lane_list and len(lane_list) >= 2:
        left = mid = right = ""   # the lanes carry all of it now — badge,
        hot = ""                  # info, and their own embedded countdowns
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
        if hot:
            tok = f"{hot} "
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


# ═══ v2: one split bar for A/B, colour-coded, with a Codex lane ═════════════
#
# G, 2026-09-08: "put them into one row and colour-code them: A on top a
# different colour from B on the bottom, letters matching, sharing the top and
# bottom of the same bar; colour-coded targets, colour-coded time-remaining
# blocks, selected underlines on the times — that way Codex fits underneath.
# Or squash it to the left and put Codex bottom-right. Build both."
#
# Layout: the A/B split bar full width on top, the Codex lane underneath.
# (A "quadrant" variant — bar left, clocks top-right, Codex bottom-right —
# was built alongside and dropped: G judged it, it did not look good.)

LANE_COLORS = {          # letter, fill ramp, notch — one hue per account
    "A": ("#00E5FF", ("#063C48", "#0FA7C4", "#00E5FF")),
    "B": ("#FF3DF2", ("#3E0B3C", "#B32AA9", "#FF3DF2")),
    "X": ("#8CFF5A", ("#173A12", "#4FB33A", "#8CFF5A")),   # Codex
}
LANE_FALLBACK = ("#FFB224", ("#3F2C08", "#B07A16", "#FFB224"))


def lane_color(lane_id: str) -> tuple[str, tuple[str, str, str]]:
    return LANE_COLORS.get(str(lane_id)[:1].upper(), LANE_FALLBACK)


def _half_bar(img, d, x0, x1, y0, y1, frac, target, ramp, accent, t,
              round_top: bool, round_bottom: bool) -> None:
    """One half of the shared bar: its own hue, its own notch. The two halves
    share the bar's outline (rounded only on their outer corners) so they read
    as ONE bar with two fills, not two bars."""
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    frac = min(1.0, max(0.0, frac))
    band = Image.new("RGB", (w, h), "#0B0E16")
    bd = ImageDraw.Draw(band)
    fw = round(w * frac)
    for x in range(fw):
        k = x / max(1, w - 1)
        r, g, b = _lerp3(ramp, k)
        if target is not None and x > w * target:      # past the notch: molten
            r, g, b = _lerp3(RAMP_OVER, k)
        bd.line([(x, 0), (x, h)], fill=(r, g, b))
    if fw > 0:                                          # moving hatch + hot head
        hatch = Image.new("L", (fw, h), 0)
        hd = ImageDraw.Draw(hatch)
        period, offset = 12 * SS, int(t * 10 * SS) % (12 * SS)
        for xx in range(-h, fw + period, period):
            hd.line([(xx + offset, h), (xx + offset + h, 0)], fill=40, width=3 * SS)
        band.paste(Image.new("RGB", (fw, h), "#FFFFFF"), (0, 0), hatch)
        band.paste("#F5FBFF", (max(0, fw - SS), 0, min(fw + SS, w), h))
    for q in (0.25, 0.5, 0.75):                          # ticks on the empty track only
        qx = round(w * q)
        if qx > fw + SS:
            bd.line([(qx, round(h * 0.25)), (qx, round(h * 0.75))], fill=(28, 32, 48), width=SS)
    if round_top:                                       # chrome bevel: light on top …
        bd.line([(0, 0), (w, 0)], fill=(255, 255, 255), width=1)
    if round_bottom:                                    # … shadow underneath
        bd.line([(0, h - 1), (w, h - 1)], fill=(0, 0, 0), width=1)
    if target is not None and 0.0 < target < 1.0:
        nx = round(w * target)                          # colour-coded target notch
        bd.line([(nx, 0), (nx, h)], fill=theme.hex_rgb(accent), width=SS * 2)
        bd.line([(nx - SS * 3, 0), (nx + SS * 3, 0)], fill=theme.hex_rgb(accent), width=SS)
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    rad = h                                              # outer corners only
    md.rectangle([0, 0, w - 1, h - 1], fill=255)
    if round_top or round_bottom:
        full = Image.new("L", (w, h * 2), 0)
        ImageDraw.Draw(full).rounded_rectangle([0, 0, w - 1, h * 2 - 1], radius=rad, fill=255)
        mask = full.crop((0, 0, w, h)) if round_top else full.crop((0, h, w, h * 2))
    img.paste(band, (x0, y0), mask)


def _chip(d, x1: int, cy: int, text: str, font, color: str, underline: bool,
          align_right: bool = True) -> float:
    """A countdown block in the lane's colour; underline = burning now."""
    tw = d.textlength(text, font=font)
    padx, pady = round(font.size * 0.35), round(font.size * 0.12)
    bx1 = x1
    bx0 = bx1 - tw - 2 * padx
    by0, by1 = cy - font.size // 2 - pady, cy + font.size // 2 + pady
    rgb = theme.hex_rgb(color)
    if underline:                                       # burning NOW: selected
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=(by1 - by0) // 2, fill=rgb)
        d.text((bx0 + padx, cy - font.size // 2 - SS), text, font=font, fill="#06080C")
        d.line([(bx0 + padx, by1 + SS * 2), (bx0 + padx + tw, by1 + SS * 2)],
               fill=rgb, width=max(1, SS // 2))
    else:
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=(by1 - by0) // 2,
                            fill=tuple(int(c * 0.20) for c in rgb), outline=rgb, width=SS)
        d.text((bx0 + padx, cy - font.size // 2 - SS), text, font=font, fill=color)
    return bx0


def _engrave(d, xy, text, font, fill):
    d.text(xy, text, font=font, fill=fill, stroke_width=SS + 1, stroke_fill=(3, 4, 8))


def _mtok(lane: dict) -> str:
    b, tg = lane.get("burned_m"), lane.get("target_m")
    if b is None or tg is None:
        return ""
    return f"{b:.1f}M/{tg:.1f}M"


def draw_meter2(size: tuple[int, int], lanes: list[dict], codex: dict | None,
                hot: str = "", t: float = 0.0) -> Image.Image:
    """Two equal bars as the BACKDROP; big colour-coded numerals as the
    display. Top: the shared A/B bar (A its colour on the top half, B on the
    bottom) with the two percentages side by side on the left, the two
    countdowns side by side on the right, the token figures small between
    them — everything in its account's colour, so large overlapping
    elements stay readable (G, 2026-09-08). Bottom: Codex, same treatment."""
    w, h = size[0] * SS, size[1] * SS
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=theme.mix("#070910", "#0B0E18", y / max(1, h - 1)))
    for y in range(0, h, 3 * SS):
        d.line([(0, y), (w, y)], fill=GRID, width=1)

    lanes = [l for l in lanes if isinstance(l, dict)][:2]
    ins_y, ins_x, gap = round(h * 0.05), round(w * 0.016), round(h * 0.09)
    bar_h = (h - 2 * ins_y - gap) // 2
    ab = (ins_x, ins_y, w - ins_x, ins_y + bar_h)
    cx = (ins_x, h - ins_y - bar_h, w - ins_x, h - ins_y)

    def frame(box, radius):
        x0, y0, x1, y1 = box
        d.rounded_rectangle([x0 - SS, y0 - SS, x1 + SS, y1 + SS], radius=radius + SS,
                            fill="#0A0D15", outline="#222739", width=SS)

    # ── backdrop: the shared A/B bar ─────────────────────────────────────────
    x0, y0, x1, y1 = ab
    n = max(1, len(lanes))
    span = (y1 - y0) / n
    frame(ab, round(span))
    for i, lane in enumerate(lanes):
        ly0, ly1 = round(y0 + i * span), round(y0 + (i + 1) * span)
        accent, ramp = lane_color(lane.get("id", ""))
        tgt = lane.get("target")
        _half_bar(img, d, x0, x1, ly0, ly1, float(lane.get("frac", 0.0)),
                  float(tgt) if tgt else None, ramp, accent, t,
                  round_top=(i == 0), round_bottom=(i == n - 1))
    d = ImageDraw.Draw(img)

    # ── overlay: big numerals across the whole bar ───────────────────────────
    bh = y1 - y0
    f_big = theme.font("display", max(10, round(bh * 0.74)))
    f_tag = theme.font("semibold", max(7, round(bh * 0.34)))
    f_small = theme.font("semibold", max(7, round(bh * 0.27)))
    cy = (y0 + y1) / 2
    pad = round(bh * 0.30)
    # left cluster: A% B%
    x = x0 + pad
    for lane in lanes:
        accent, _ = lane_color(lane.get("id", ""))
        tag = str(lane.get("id", ""))[:1]
        _engrave(d, (x, cy - f_tag.size * 0.55 - f_big.size * 0.28), tag, f_tag, accent)
        x += d.textlength(tag, font=f_tag) + SS * 2
        pct = f"{round(float(lane.get('pct', 0)))}%"
        _engrave(d, (x, cy - f_big.size * 0.58), pct, f_big, accent)
        x += d.textlength(pct, font=f_big) + pad
    left_end = x
    # right cluster: countdowns, right-aligned, A then B; hot one underlined
    clocks = [(l, str(l.get("clock") or "")) for l in lanes if l.get("clock")]
    xr = x1 - pad
    for lane, clock in reversed(clocks):
        accent, _ = lane_color(lane.get("id", ""))
        cw = d.textlength(clock, font=f_big)
        xr -= cw
        _engrave(d, (xr, cy - f_big.size * 0.58), clock, f_big, accent)
        if hot and lane.get("id") == hot:
            uy = cy + f_big.size * 0.50
            d.line([(xr, uy), (xr + cw, uy)], fill=theme.hex_rgb(accent), width=SS * 2)
        xr -= pad
    right_start = xr + pad
    # middle: token figures, small, colour-coded, STACKED (A over B) so both
    # fit between the two big clusters
    figs = [(lane_color(l.get("id", ""))[0], _mtok(l)) for l in lanes if _mtok(l)]
    if figs:
        widest = max(d.textlength(f, font=f_small) for _, f in figs)
        room = right_start - left_end + pad          # the cluster pads are slack
        if widest > room:                             # no room for both: the hot one
            figs = [f for f in figs if hot and f[0] == lane_color(hot)[0]][:1] or figs[:1]
            widest = max(d.textlength(f, font=f_small) for _, f in figs)
        if widest <= room:
            fx = left_end - pad / 2 + (room - widest) / 2
            rows = len(figs)
            for k, (col, f) in enumerate(figs):
                fy = cy - (rows * f_small.size) / 2 + k * f_small.size * 1.05 - SS
                _engrave(d, (fx, fy), f, f_small, col)

    # ── Codex: same treatment on its own bar ────────────────────────────────
    if codex:
        x0, y0, x1, y1 = cx
        ch = y1 - y0
        accent, ramp = lane_color("X")
        frame(cx, ch // 2)
        tgt = codex.get("target")
        _half_bar(img, d, x0, x1, y0, y1, float(codex.get("frac", 0.0)),
                  float(tgt) if tgt else None, ramp, accent, t, True, True)
        d = ImageDraw.Draw(img)
        cy = (y0 + y1) / 2
        x = x0 + pad
        _engrave(d, (x, cy - f_tag.size * 0.55 - f_big.size * 0.28), "CODEX", f_tag, accent)
        x += d.textlength("CODEX", font=f_tag) + SS * 3
        pct = f"{round(float(codex.get('pct', 0)))}%"
        _engrave(d, (x, cy - f_big.size * 0.58), pct, f_big, accent)
        x += d.textlength(pct, font=f_big) + pad
        clock = str(codex.get("clock") or "")
        xr = x1 - pad
        if clock:
            cw = d.textlength(clock, font=f_big)
            xr -= cw
            _engrave(d, (xr, cy - f_big.size * 0.58), clock, f_big, accent)
            if hot == "X":
                uy = cy + f_big.size * 0.50
                d.line([(xr, uy), (xr + cw, uy)], fill=theme.hex_rgb(accent), width=SS * 2)
        mid = _mtok(codex)
        # Whisper slot, in priority order: stand-in data, a snapshot that has
        # stopped refreshing, or which window the percentage describes (the
        # Pro plan reports weekly, the A/B lanes report 5h — unlabelled, the
        # two invite being read as the same kind of number).
        if codex.get("dummy"):
            tag, tag_col = "dummy", "#3C4458"
        elif codex.get("stale"):
            tag, tag_col = "stale", "#7A6430"
        else:
            tag, tag_col = str(codex.get("window") or ""), "#3C4458"
        mw = d.textlength(mid, font=f_small) if mid else 0
        tw = d.textlength(tag, font=f_small) if tag else 0
        room = (xr - pad) - x
        total = mw + (SS * 8 + tw if tag else 0)
        if total <= room:
            # Centred when there are token figures to centre; with only the
            # window whisper, hug the percentage it qualifies — dead-centre put
            # "weekly" right under the target notch, which struck through it.
            fx = x + (room - total) / 2 if mid else x
            if mid:
                _engrave(d, (fx, cy - f_small.size * 0.55), mid, f_small, accent)
                fx += mw + SS * 8
            if tag:                                      # never shouts
                d.text((fx, cy - f_small.size * 0.55), tag, font=f_small, fill=tag_col)
    return img.resize(size, Image.LANCZOS)
