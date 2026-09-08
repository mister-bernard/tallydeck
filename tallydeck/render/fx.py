"""Key effects: the fireworks burst a key throws when it is pressed.

A press on the deck used to be answered by nothing visible on the key
itself — the popup appeared on the Mac, the key kept blinking, and it was
not obvious the press had landed. Now the key explodes: two staggered
bursts of particles in the state colour and white, fading and falling
over ~1.2 s, drawn over the key's own face. Pure PIL, deterministic per
key id so a given key always throws the same shape.
"""

from __future__ import annotations

import math
import random

from PIL import Image, ImageDraw

from . import theme

LENGTH = 1.2          # seconds


def _ease_out(p: float) -> float:
    return 1 - (1 - p) ** 2.2


def fireworks(face: Image.Image, phase: float, color: str, seed: str = "") -> Image.Image:
    """Composite a burst at `phase` (0..1) over `face`; returns a new image."""
    img = face.convert("RGBA")
    s = img.size[0]
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    rnd = random.Random(seed or "tally")
    col = theme.hex_rgb(color)
    bursts = [(s * 0.5, s * 0.5, 0.0, 44),
              (s * rnd.uniform(0.3, 0.7), s * rnd.uniform(0.3, 0.6), 0.35, 26)]
    for bx, by, delay, n in bursts:
        p = (phase - delay) / (1 - delay)
        if p <= 0:
            continue
        p = min(1.0, p)
        e = _ease_out(p)
        for i in range(n):
            ang = rnd.uniform(0, 2 * math.pi)
            spd = rnd.uniform(0.45, 1.0)
            r = e * spd * s * 0.46
            x = bx + math.cos(ang) * r
            y = by + math.sin(ang) * r + (p * p) * s * 0.12      # gravity
            k = max(0.0, 1 - p) ** 1.3
            white = rnd.random() < 0.3
            rgb = (255, 255, 255) if white else col
            a = int(255 * k)
            # trailing tail toward the origin
            tx = bx + math.cos(ang) * r * 0.75
            ty = by + math.sin(ang) * r * 0.75 + (p * p) * s * 0.10
            d.line([tx, ty, x, y], fill=(*rgb, int(a * 0.45)), width=1)
            rad = max(1.2, (1 - p) * s * 0.04)
            d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(*rgb, a))
    # flash of light at ignition
    if phase < 0.18:
        a = int(120 * (1 - phase / 0.18))
        d.rectangle([0, 0, s, s], fill=(*col, a))
    return Image.alpha_composite(img, over).convert("RGB")
