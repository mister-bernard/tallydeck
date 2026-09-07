"""Theme: the visual language of a tally key.

One place for every color and font so the deck reads as a single system.
State colors are chosen for glanceability at arm's length on a 96 px LCD:
hot states saturate, calm states recede.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from PIL import ImageFont

from ..signal import BLOCKED, ATTENTION, WORKING, SUCCESS, IDLE, OFFLINE

# ── palette ──────────────────────────────────────────────────────────────────

BG = "#0C0D10"            # key background — near-black, slightly blue
BG_EMPTY = "#08090B"      # unassigned key
FG = "#F2F3F5"            # primary text
FG_DIM = "#8B8F98"        # sublabel / chrome
TRACK = "#26282E"         # progress track / hairlines

STATE_COLOR = {
    BLOCKED: "#E5484D",    # red — hard stop
    ATTENTION: "#FFB224",  # amber — wants you
    WORKING: "#3E9BFF",    # blue — in motion
    SUCCESS: "#43B75D",    # green — landed
    IDLE: "#565A64",       # gray — quiet
    OFFLINE: "#33353C",    # near-invisible — gone
}

# Text color used when a flashing key "floods" with its state color.
FLOOD_TEXT = "#0B0C0E"

# States whose label dims with the rest of the key.
MUTED_STATES = {IDLE, OFFLINE}

# ── flash timing ─────────────────────────────────────────────────────────────
# attention: steady 1.25 Hz blink. blocked: urgent double-pulse per period.

FLASH_PERIOD = 0.8         # seconds per cycle
FRAME_INTERVAL = 0.1       # renderer tick while anything is flashing


def flash_lit(state: str, t: float) -> bool:
    """Is a flashing key 'lit' at epoch time t?

    t is wall-clock epoch, not monotonic, on purpose: phase derives from
    absolute time, so every flashing key — across processes, machines and
    restarts — blinks in unison. Synchronized blinkers read as one alarm;
    unsynchronized ones read as noise. (Idea borrowed from Bitfocus
    Companion's epoch-aligned blink timers.)"""
    phase = (t % FLASH_PERIOD) / FLASH_PERIOD
    if state == BLOCKED:                       # ▮▮·▮▮····  double pulse
        return phase < 0.18 or 0.30 < phase < 0.48
    return phase < 0.55                        # ▮▮▮▮▮····  even blink


# ── fonts ────────────────────────────────────────────────────────────────────

_FONT_DIR = Path(str(resources.files("tallydeck") / "fonts"))
_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """kind: 'display' | 'semibold' | 'regular'."""
    key = (kind, size)
    if key in _cache:
        return _cache[key]
    names = {
        "display": "InterDisplay-Bold.ttf",
        "semibold": "Inter-SemiBold.ttf",
        "regular": "Inter-Regular.ttf",
    }
    candidates = [str(_FONT_DIR / names.get(kind, names["regular"]))] + _FALLBACKS
    for path in candidates:
        try:
            f = ImageFont.truetype(path, size)
            _cache[key] = f
            return f
        except OSError:
            continue
    f = ImageFont.load_default()
    _cache[key] = f
    return f


# ── helpers ──────────────────────────────────────────────────────────────────

def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


# Relative burn-rate heat ramp for key backgrounds: cold keys stay near-black,
# warm ones glow indigo → violet → ember. Full spectrum so levels read apart.
HEAT_RAMP = ("#16224E", "#5B2E9E", "#B33A1E")


def heat_bg(h: float) -> tuple[int, int, int]:
    """Background for a key with relative heat h in 0..1."""
    h = min(1.0, max(0.0, h))
    if h <= 0.0:
        return hex_rgb(BG)
    a, b, c = HEAT_RAMP
    tint = mix(a, b, h * 2) if h < 0.5 else mix(b, c, (h - 0.5) * 2)
    base = hex_rgb(BG)
    # Blend strength grows with heat but stays dark enough for white text.
    k = 0.25 + 0.45 * h
    return tuple(round(base[i] + (tint[i] - base[i]) * k) for i in range(3))


def mix(a: str, b: str, t: float) -> tuple[int, int, int]:
    """Linear blend of two hex colors, t in 0..1 toward b."""
    ra, ga, ba = hex_rgb(a)
    rb, gb, bb = hex_rgb(b)
    return (round(ra + (rb - ra) * t),
            round(ga + (gb - ga) * t),
            round(ba + (bb - ba) * t))
