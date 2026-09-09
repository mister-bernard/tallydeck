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

A Codex-harness key wears the same bar down its LEFT edge instead:

    ┌──────────────┐
    │▏ astra       │  ← tally bar (state color), vertical
    │▏ 3 files in  │
    │▏             │
    │▏▂▂▂▂▂▂▁▁▁▁▁▁ │
    └──────────────┘

Position, not color, carries the harness — state color still has to mean
state, and an L-vs-T edge reads across the room before any label does.

Flashing keys alternate with a "flood" frame: the whole face fills with
the state color and the text inverts — unmissable in peripheral vision.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from ..signal import Signal, is_codex
from . import theme

SS = 2  # supersample factor


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if not text or draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _wrap2(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """Wrap onto up to two lines, breaking at - or space where possible.
    Ellipsis only when even two lines cannot hold it."""
    if not text or draw.textlength(text, font=font) <= max_w:
        return [text]
    cut = len(text)
    while cut > 1 and draw.textlength(text[:cut], font=font) > max_w:
        cut -= 1
    nice = max(text.rfind("-", 1, cut + 1), text.rfind(" ", 1, cut + 1))
    if nice >= max(2, cut // 2):          # break at a boundary if it's not tiny
        head, rest = text[:nice + 1].rstrip(), text[nice + 1:]
        if text[nice] == "-":
            head = text[:nice + 1]         # keep the hyphen visible
    else:
        head, rest = text[:cut], text[cut:]
    return [head, _truncate(draw, rest, font, max_w)]


def _wrap_n(draw, text, font, max_w, max_lines):
    words, lines, line = text.split(), [], ""
    for w in words:
        cand = f"{line} {w}".strip()
        if draw.textlength(cand, font=font) > max_w and line:
            lines.append(line)
            line = w
            if len(lines) == max_lines:
                break
        else:
            line = cand
    if line and len(lines) < max_lines:
        lines.append(line)
    return lines


def draw_key(sig: Signal | None, px: int, lit: bool = False,
             pressed: bool = False, askpage: int = 0) -> Image.Image:
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

    heat = float((sig.meta or {}).get("heat", 0.0) or 0.0)
    if (sig.meta or {}).get("oneshot"):
        muted = True          # disposable worker: renders quiet, ranks last

    if lit:
        bg, fg, sub = color, theme.FLOOD_TEXT, theme.FLOOD_TEXT
        bar = theme.mix(color, "#000000", 0.35)
        track = theme.mix(color, "#000000", 0.25)
        fill = theme.FLOOD_TEXT
        d.rectangle([0, 0, s, s], fill=bg)
    else:
        # Marbled background: a deterministic swirl per card, its base color
        # carrying relative burn heat (near-black → indigo → ember), state
        # accent only in the ink highlights. Luminance-ceilinged, so text
        # contrast is a property of the code, not a hope. Flood frames stay
        # flat — flash must dominate everything.
        from .marble import card_bg
        img = card_bg((s, s), sig.id, sig.state,
                      theme.hex_rgb(color), heat).copy()
        d = ImageDraw.Draw(img)
        fg = theme.FG_DIM if muted else theme.FG
        # Hot cards glow: dim gray would sink into the ember, so the
        # sublabel steps up to full white once the card is bright.
        sub = theme.FG if heat >= 0.45 and not muted else theme.FG_DIM
        bar = theme.mix(color, theme.BG, 0.55) if muted else color
        track = theme.TRACK
        fill = color

    # tally bar — across the top, or down an edge for a Codex session.
    # The vertical bar is the harness tell; everything else about the key is
    # unchanged, including where the first line of text sits, so a mixed deck
    # still reads as rows rather than as two different products.
    #
    # WHICH edge is the Codex account tell (G, 2026-09-09): account 1 keeps the
    # left edge, account 2 takes the right. Two Codex accounts now share the
    # deck, and the bottom-right badge alone ("O" vs "O2") is two characters of
    # difference on a 96px key — the side of the bar reads at a glance, across
    # the room, which is what the deck is for.
    bar_h = round(s * 0.085)
    codex = is_codex(sig)
    acct = str((sig.meta or {}).get("account", ""))
    codex_right = codex and acct.strip().upper() in ("O2", "2")
    if codex_right:
        d.rectangle([s - bar_h, 0, s, s], fill=bar)
    elif codex:
        d.rectangle([0, 0, bar_h, s], fill=bar)
    else:
        d.rectangle([0, 0, s, bar_h], fill=bar)

    pad = round(s * 0.10)
    lx = bar_h + round(s * 0.06) if (codex and not codex_right) else pad
    # A right-edge bar eats from the text's right margin, not its left, or the
    # label would run underneath it.
    avail = s - lx - pad - (bar_h if codex_right else 0)
    f_label = theme.font("display", round(s * 0.195))
    f_sub = theme.font("regular", round(s * 0.135))

    # Wrap instead of amputate: a name that overflows drops to a slightly
    # smaller face and takes two lines, which lets the break land on a
    # hyphen/space instead of mid-word. Ellipsis only past two full lines.
    y = bar_h + round(s * 0.10)
    if d.textlength(sig.label, font=f_label) <= avail:
        d.text((lx, y), sig.label, font=f_label, fill=fg)
    else:
        f_label = theme.font("display", round(s * 0.155))
        lines = _wrap2(d, sig.label, f_label, avail)
        d.text((lx, y), lines[0], font=f_label, fill=fg)
        if len(lines) > 1:
            d.text((lx, y + round(s * 0.165)), lines[1], font=f_label, fill=fg)
            y += round(s * 0.13)

    # Account badge, bottom-right. Which of the two quotas a session is
    # draining is invisible otherwise — you can watch it work with no idea
    # whose ceiling it is walking toward. Drawn before the sublabel so the
    # sublabel can be truncated around it rather than run underneath.
    badge_w = 0
    if acct:
        f_acct = theme.font("semibold", round(s * 0.135))
        aw = d.textlength(acct, font=f_acct)
        bw, bh = aw + round(s * 0.09), round(s * 0.16)
        # Clear of a right-edge Codex bar, so the badge never sits on it.
        bx1, by1 = s - pad - (bar_h if codex_right else 0), s - round(s * 0.035)
        bx0, by0 = bx1 - bw, by1 - bh
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=round(s * 0.04),
                            fill=track)
        d.text((bx0 + (bw - aw) / 2, by0 + round(s * 0.012)), acct,
               font=f_acct, fill=sub)
        badge_w = bw + pad

    if sig.sublabel:
        y2 = y + round(s * 0.24)
        if sig.state in ("attention", "blocked") and len(sig.sublabel) > 30:
            # The ask gets the key's whole empty middle: 3 wrapped lines
            # per page, cycling through as many pages as the message needs
            # (the flash draws the eye; the page flips finish the story).
            lines = _wrap_n(d, sig.sublabel, f_sub, avail, 12)
            per = 3
            pages = max(1, (len(lines) + per - 1) // per)
            pg = askpage % pages
            for i, ln in enumerate(lines[pg * per:(pg + 1) * per]):
                d.text((lx, y2 + i * round(s * 0.145)), ln,
                       font=f_sub, fill=sub)
            if pages > 1:      # tiny page dots, bottom-left
                r_ = max(2, s // 40)
                for pi in range(pages):
                    x0d = lx + pi * r_ * 3
                    y0d = s - round(s * 0.05) - r_
                    d.ellipse([x0d, y0d, x0d + r_, y0d + r_],
                              fill=fill if pi == pg else track)
        elif d.textlength(sig.sublabel, font=f_sub) <= avail:
            d.text((lx, y2), sig.sublabel, font=f_sub, fill=sub)
        else:
            # Wrap rather than amputate: "3m · 1704k/m" lost its burn rate
            # to an ellipsis on the hottest key of the fleet — the one
            # number that key exists to show.
            parts = [p_.strip() for p_ in sig.sublabel.split(" · ")]
            if len(parts) > 1 and all(
                    d.textlength(p_, font=f_sub) <= avail for p_ in parts):
                lines = [parts[0], " · ".join(parts[1:])]   # break at the dot
                if d.textlength(lines[1], font=f_sub) > avail:
                    lines = parts[:2]
            else:
                lines = _wrap2(d, sig.sublabel, f_sub, avail)
            for i, ln in enumerate(lines[:2]):
                d.text((lx, y2 + i * round(s * 0.145)), ln,
                       font=f_sub, fill=sub)

    # progress
    if sig.progress is not None:
        h = round(s * 0.07)
        y0 = s - round(s * 0.145)
        x0, x1 = lx, s - pad - badge_w
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
