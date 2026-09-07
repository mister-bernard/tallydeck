"""Marbled card backgrounds — closed-form vector marbling, pure PIL.

Math from Lu, Jaffer, Jin, Zhao & Mao, *Mathematical Marbling* (IEEE CG&A
2012): real paper-marbling actions (ink drops, tine combs, vortices) are
closed-form displacement fields — no fluid solver, no per-pixel Python.
State is a list of ink polygons; every operator moves vertices; PIL's C
rasterizer does the pixels. ~10 ms per card, cached per
(id, state, tint, heat-bucket).

Design guarantees, enforced in code rather than tuned by eye:
- Deterministic: seeded with blake2b(id|state) — never Python's hash(),
  which PEP 456 randomizes per process and would reshuffle the deck on
  every restart. Same card → same swirl, forever.
- Text-safe: palette luminance is FORCED onto a lo..hi ramp, and the ramp's
  ceiling is enforced on the composite including the burn-heat base —
  measured worst-case white-text contrast stays above ~12:1 across the
  whole heat range.
- Heat-composed: the burn-rate heat color is the canvas base; swirl ink
  dissolves into it at the dark end and carries the state accent only in
  the highlights.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageFilter

SS = 3  # supersample inside the swirl render

# Burn-rate heat ramp: near-black → indigo → violet → ember.
HEAT = [(0.00, (6, 7, 12)), (0.35, (28, 24, 74)), (0.65, (96, 42, 140)),
        (0.85, (190, 72, 96)), (1.00, (238, 132, 48))]

HEAT_BUCKETS = 8          # cache granularity for the continuous heat value
CEILING = 0.20            # max composite luminance under the text
_CACHE_MAX = 512


def seed_of(card_id: str, state: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(f"{card_id}|{state}".encode(), digest_size=8).digest(),
        "big")


def heat_color(b: float) -> tuple[float, float, float]:
    b = min(1.0, max(0.0, b))
    for i in range(len(HEAT) - 1):
        t0, c0 = HEAT[i]
        t1, c1 = HEAT[i + 1]
        if b <= t1:
            f = (b - t0) / (t1 - t0) if t1 > t0 else 0.0
            return tuple(c0[k] + (c1[k] - c0[k]) * f for k in range(3))
    return HEAT[-1][1]


def _lum(rgb) -> float:
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def plan(burn: float, ceiling: float = CEILING):
    """(base_rgb, lo, hi) with the luminance ceiling enforced on the
    COMPOSITE — as heat brightens the base, the base is clamped so the ink
    ramp above it keeps its contrast headroom instead of silently eating it."""
    base = heat_color(burn)
    bl = _lum(base)
    k = min(1.0, (ceiling * 0.55) / bl) if bl > 1e-4 else 1.0
    base = tuple(c * k for c in base)
    return base, _lum(base), ceiling


# ── closed-form operators (Lu et al. 2012) ───────────────────────────────────

def op_drop(pts, cx, cy, r):
    """O(p|c,r) = c + (p−c)·√(1 + r²/‖p−c‖²). Area-preserving — drops push
    existing ink aside instead of covering it."""
    rr = r * r
    for i in range(0, len(pts), 2):
        dx = pts[i] - cx
        dy = pts[i + 1] - cy
        m2 = dx * dx + dy * dy
        if m2 < 1e-9:
            continue
        s = math.sqrt(1.0 + rr / m2)
        pts[i] = cx + dx * s
        pts[i + 1] = cy + dy * s


def op_tine(pts, cx, cy, ux, uy, alpha, lam):
    nx, ny = -uy, ux
    for i in range(0, len(pts), 2):
        d = abs((pts[i] - cx) * nx + (pts[i + 1] - cy) * ny)
        f = alpha * lam / (d + lam)
        pts[i] += ux * f
        pts[i + 1] += uy * f


def op_wavy(pts, theta, amp, omega):
    st, ct = math.sin(theta), math.cos(theta)
    for i in range(0, len(pts), 2):
        f = amp * math.sin(omega * (pts[i] * ct + pts[i + 1] * st))
        pts[i] += -st * f
        pts[i + 1] += ct * f


def op_vortex(pts, cx, cy, alpha, lam, max_turn=1.2):
    """The signature curl. θ blows up as d→0, which out-twists the vertex
    spacing and facets the spiral core — hence the per-point clamp, and the
    caller integrates in steps with re-subdivision between."""
    for i in range(0, len(pts), 2):
        dx = pts[i] - cx
        dy = pts[i + 1] - cy
        d = math.hypot(dx, dy)
        if d < 1e-6:
            continue
        th = min((alpha * lam / (d + lam)) / d, max_turn)
        s, c = math.sin(th), math.cos(th)
        pts[i] = cx + dx * c - dy * s
        pts[i + 1] = cy + dx * s + dy * c


def subdivide(pts, max_seg):
    """Insert midpoints where the polyline has stretched past max_seg — the
    operators stretch non-uniformly, exactly where the pattern gets good."""
    out = []
    n = len(pts)
    for i in range(0, n, 2):
        x0, y0 = pts[i], pts[i + 1]
        x1, y1 = pts[(i + 2) % n], pts[(i + 3) % n]
        out += [x0, y0]
        k = int(math.hypot(x1 - x0, y1 - y0) / max_seg)
        for j in range(1, min(k, 8)):
            f = j / k
            out += [x0 + (x1 - x0) * f, y0 + (y1 - y0) * f]
    return out


def _circle(cx, cy, r, n):
    out = []
    for i in range(n):
        a = 6.28318 * i / n
        out += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return out


def _cos_palette(t, tint, lo, hi, chroma=0.22, base=None):
    """IQ cosine palette near the state tint; luminance forced onto lo..hi.
    With a base, ink dissolves into the heat at the dark end and carries the
    accent only in the highlights."""
    rgb = [0.5 + 0.5 * math.cos(6.28318 * (t + p)) for p in (0.0, 0.33, 0.67)]
    rgb = [rgb[k] * chroma + (tint[k] / 255.0) * (1 - chroma) for k in range(3)]
    if base is not None:
        rgb = [rgb[k] * (0.35 + 0.65 * t) +
               (base[k] / 255.0) * (0.65 - 0.65 * t) for k in range(3)]
    lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    s = (lo + (hi - lo) * t) / lum if lum > 1e-4 else 0.0
    return tuple(int(255 * min(1.0, v * s)) for v in rgb)


# ── the swirl ────────────────────────────────────────────────────────────────

def swirl(size, card_id, state, tint, lo=0.05, hi=0.17,
          ndrops=9, nverts=120, base=None) -> Image.Image:
    rng = random.Random(seed_of(card_id, state))
    W, H = size[0] * SS, size[1] * SS
    S = min(W, H)
    # Drop scale off the diagonal, not min(W,H) — sized off the short side,
    # the 248×58 strip came out almost empty.
    D = math.hypot(W, H) * 0.62
    drops = []

    for k in range(ndrops):
        cx = rng.uniform(-0.05, 1.05) * W
        cy = rng.uniform(-0.05, 1.05) * H
        r = D * rng.uniform(0.13, 0.26)
        for pts, _ in drops:
            op_drop(pts, cx, cy, r)
        t = (k + 0.5) / ndrops
        drops.append((_circle(cx, cy, r, nverts),
                      _cos_palette(t, tint, lo, hi, base=base)))
        th = rng.uniform(0, math.pi)   # comb after every drop → ribbons
        for pts, _ in drops:
            if k % 3 == 2:
                op_wavy(pts, th, D * 0.09,
                        rng.uniform(1.2, 2.4) * math.pi / S)
            else:
                op_tine(pts, rng.uniform(0, W), rng.uniform(0, H),
                        math.cos(th), math.sin(th),
                        D * rng.uniform(0.20, 0.40), D * 0.30)

    # one strong vortex, integrated in steps with re-subdivision between
    vx = W * rng.uniform(0.35, 0.65)
    vy = H * rng.uniform(0.35, 0.65)
    a = D * rng.uniform(1.4, 2.4)
    for _ in range(4):
        drops = [(subdivide(pts, SS * 1.2), col) for pts, col in drops]
        for pts, _ in drops:
            op_vortex(pts, vx, vy, a / 4, D * 0.45)

    bg = tuple(int(c) for c in base) if base else \
        _cos_palette(0.0, tint, lo * 0.55, lo * 0.55)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    for pts, col in drops:
        d.polygon(pts, fill=col)
    return img.resize(size, Image.LANCZOS).filter(ImageFilter.SMOOTH)


# ── cached front door for the keycards ───────────────────────────────────────

_cache: OrderedDict[tuple, Image.Image] = OrderedDict()


def card_bg(size: tuple[int, int], card_id: str, state: str,
            tint: tuple[int, int, int], heat: float = 0.0) -> Image.Image:
    """Marbled background for one card, cached. Callers must .copy() before
    drawing on it. Heat is quantized to HEAT_BUCKETS so the cache holds."""
    bucket = round(min(1.0, max(0.0, heat)) * (HEAT_BUCKETS - 1))
    key = (size, card_id, state, tint, bucket)
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return hit
    base, lo, hi = plan(bucket / (HEAT_BUCKETS - 1))
    img = swirl(size, card_id, state, tint, lo=lo, hi=hi, base=base)
    _cache[key] = img
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return img
