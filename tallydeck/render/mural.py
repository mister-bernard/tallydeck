"""The quiet-deck mural: Mr. B, in ASCII, across every key.

When nothing is blocked, asking, or working, the deck has nothing to say —
"all quiet" on the info bar and eight dark tiles. Instead, the eight keys
become one canvas: a fedora-and-magnifier portrait on the left, MR. B in
block lettering across the right, a caption and a prompt line with a
blinking cursor along the bottom. The moment anything needs the operator,
the tiles come back; a press on the mural peeks at the plain grid.

The art is characters. Two source images (a drawn silhouette and a
rendered word) are sampled per character cell and mapped onto a density
ramp; each character is painted in a gradient — indigo at the top fading
to the deck's amber at the bottom, the same palette as a working fleet,
just quieter. Cells are tiny (4×7 px): at arm's length the picture reads
as a picture, up close it is text. The caption and prompt are drawn as
readable text, since those are meant to be read.

Physical keys have bezels between them; the virtual canvas leaves a GAP so
a line that runs off one key continues where it should on the next. The
same character grid feeds the terminal renderer.
"""

from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image, ImageDraw

from ..devices import DeviceProfile
from . import theme

GAP = 18            # virtual px between keys (the bezel, roughly)
CW, CH = 4, 7       # character cell, px (DejaVu Sans Mono at 7 px)
RAMP = " .:-=+*#%@"
INK_TOP = "#7B88FF"       # indigo
INK_BOTTOM = "#FFB224"    # the deck's amber
TAG = "fixer · analyst · the impossible, handled"
PROMPT = "> all quiet"


def canvas_size(profile: DeviceProfile) -> tuple[int, int]:
    w = profile.cols * profile.key_px + (profile.cols - 1) * GAP
    h = profile.rows * profile.key_px + (profile.rows - 1) * GAP
    return w, h


def _ramp_char(v: float) -> str:
    v = max(0.0, min(1.0, v))
    return RAMP[min(len(RAMP) - 1, int(v * (len(RAMP) - 1) + 0.5))]


def _portrait(w: int, h: int) -> Image.Image:
    """A fixer in a fedora, magnifier up, as a shaded silhouette. Tones are
    spread across the ramp on purpose: shoulders sparse, face mid, hat
    dense, so the figure still reads at ~35 characters wide."""
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    d.ellipse([w * -0.05, h * 0.78, w * 1.05, h * 1.60], fill=90)         # shoulders
    d.rectangle([w * 0.43, h * 0.68, w * 0.57, h * 0.86], fill=115)       # neck
    d.ellipse([w * 0.30, h * 0.28, w * 0.70, h * 0.74], fill=165)         # head
    d.chord([w * 0.30, h * 0.28, w * 0.70, h * 0.74], 25, 155, fill=125)  # jaw
    d.rounded_rectangle([w * 0.32, h * 0.06, w * 0.68, h * 0.34],
                        radius=int(w * 0.08), fill=250)                   # crown
    d.ellipse([w * 0.41, h * 0.03, w * 0.59, h * 0.13], fill=185)         # dent
    d.rectangle([w * 0.32, h * 0.26, w * 0.68, h * 0.32], fill=35)        # band
    d.ellipse([w * 0.10, h * 0.29, w * 0.90, h * 0.40], fill=255)         # brim
    d.rectangle([w * 0.10, h * 0.345, w * 0.90, h * 0.365], fill=60)      # brim shadow
    d.ellipse([w * 0.39, h * 0.47, w * 0.45, h * 0.51], fill=20)          # left eye
    r = w * 0.135
    ex, ey = w * 0.61, h * 0.50
    d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=40)                  # lens
    d.ellipse([ex - r, ey - r, ex + r, ey + r], outline=255,
              width=max(3, int(w * 0.04)))                               # rim
    d.ellipse([ex - 0.035 * w, ey - 0.025 * h, ex + 0.035 * w, ey + 0.025 * h],
              fill=255)                                                   # the eye
    hx, hy = ex + r * 0.70, ey + r * 0.70
    d.line([hx, hy, hx + w * 0.22, hy + h * 0.20], fill=255,
           width=max(4, int(w * 0.055)))                                  # handle
    return img


def _lettering(w: int, h: int, text: str) -> Image.Image:
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    size = h
    while size > 8:
        f = theme.font("display", size)
        bbox = d.textbbox((0, 0), text, font=f)
        if bbox[2] - bbox[0] <= w * 0.97 and bbox[3] - bbox[1] <= h * 0.97:
            break
        size -= 2
    f = theme.font("display", size)
    bbox = d.textbbox((0, 0), text, font=f)
    x = (w - (bbox[2] - bbox[0])) / 2 - bbox[0]
    y = (h - (bbox[3] - bbox[1])) / 2 - bbox[1]
    d.text((x, y), text, font=f, fill=255)
    return img


def _sample(img: Image.Image, cols: int, rows: int) -> list[list[float]]:
    """Mean luminance per character cell, 0..1."""
    small = img.resize((cols, rows), Image.BOX)
    px = small.load()
    return [[px[x, y] / 255.0 for x in range(cols)] for y in range(rows)]


def _noise(x: int, y: int) -> float:
    return math.modf(math.sin(x * 12.9898 + y * 78.233) * 43758.5453)[0] % 1.0


def _regions(cols: int, rows: int, key_rows: int = 13) -> dict:
    """Portrait: left ~third, full height (it crosses a bezel; a picture
    survives that). Lettering: the rest of the TOP key row only, so no
    letter is sawn in half by the horizontal bezel. Caption and prompt:
    the bottom-right keys, each line inside one key."""
    pcols = max(20, int(cols * 0.35))
    lx0 = pcols + 3
    return {"pcols": pcols, "lx0": lx0, "lw": cols - lx0 - 1,
            "lrows": min(key_rows, rows), "text_row": rows - 5}


@lru_cache(maxsize=8)
def grid(cols: int, rows: int, phase: int = 0) -> tuple[str, ...]:
    """The mural as rows of characters. `phase` only moves the cursor."""
    R = _regions(cols, rows)
    port = _sample(_portrait(R["pcols"] * CW * 6, rows * CH * 6), R["pcols"], rows)
    letters = _sample(_lettering(R["lw"] * CW * 6, R["lrows"] * CH * 6, "MR. B"),
                      R["lw"], R["lrows"])
    out: list[str] = []
    for y in range(rows):
        line = []
        for x in range(cols):
            ch = " "
            if x < R["pcols"]:
                ch = _ramp_char(port[y][x])
            elif y < R["lrows"] and x >= R["lx0"] and x - R["lx0"] < R["lw"]:
                ch = _ramp_char(letters[y][x - R["lx0"]] * 1.25)
            if ch == " " and _noise(x, y) > 0.975:
                ch = "."                        # a faint starfield
            line.append(ch)
        out.append("".join(line))
    # Caption + prompt as text rows (the terminal view); the image draws
    # them in a readable face instead.
    cursor = "_" if phase % 2 == 0 else " "
    ty = R["text_row"]
    for i, s in ((0, TAG), (2, PROMPT + cursor)):
        if 0 <= ty + i < rows:
            left = out[ty + i][:R["lx0"]]
            out[ty + i] = (left + s)[:cols].ljust(cols)
    return tuple(out)


def text(profile: DeviceProfile, t: float = 0.0) -> str:
    w, h = canvas_size(profile)
    return "\n".join(grid(w // CW, h // CH, int(t)))


@lru_cache(maxsize=8)
def _canvas(profile: DeviceProfile, phase: int) -> Image.Image:
    w, h = canvas_size(profile)
    cols, rows = w // CW, h // CH
    kp = profile.key_px
    R = _regions(cols, rows, key_rows=kp // CH)
    img = Image.new("RGB", (w, h), theme.BG)
    d = ImageDraw.Draw(img)
    f = theme.font("mono", CH)
    top, bot = theme.hex_rgb(INK_TOP), theme.hex_rgb(INK_BOTTOM)
    x0 = (w - cols * CW) // 2
    y0 = (h - rows * CH) // 2
    lines = grid(cols, rows, phase)
    for y, line in enumerate(lines):
        if y >= R["text_row"]:
            line = line[:R["lx0"]]              # text drawn readable below
        ty = y / max(1, rows - 1)
        for x, ch in enumerate(line):
            if ch == " ":
                continue
            k = RAMP.index(ch) / (len(RAMP) - 1) if ch in RAMP else 0.7
            bright = 0.45 + 0.55 * k
            col = tuple(int((top[i] * (1 - ty) + bot[i] * ty) * bright)
                        for i in range(3))
            d.text((x0 + x * CW, y0 + y * CH), ch, font=f, fill=col)
    # Readable caption + prompt, bottom-right keys. Each line lives inside
    # ONE key — text that crosses a bezel loses letters to it.
    if profile.rows >= 2 and profile.cols >= 4:
        amber = theme.hex_rgb(INK_BOTTOM)
        dim = theme.mix(INK_BOTTOM, theme.BG, 0.30)
        f12 = theme.font("regular", 12)
        f_sb = theme.font("semibold", 12)
        f_mono = theme.font("mono", 11)
        ky = (profile.rows - 1) * (kp + GAP)
        kx2 = (profile.cols - 2) * (kp + GAP) + 8
        kx3 = (profile.cols - 1) * (kp + GAP) + 8
        for i, (txt, f_) in enumerate(((("fixer · analyst"), f_sb),
                                        (("the impossible,"), f12),
                                        (("handled."), f12))):
            d.text((kx2, ky + 22 + i * 19), txt, font=f_,
                   fill=amber if i == 0 else dim)
        cursor = "_" if phase % 2 == 0 else ""
        d.text((kx3, ky + 36), PROMPT.split()[0], font=f_mono, fill=dim)
        d.text((kx3 + 14, ky + 36), " ".join(PROMPT.split()[1:]) + cursor,
               font=f_mono, fill=amber)
        d.text((kx3, ky + 60), "MR. BERNARD", font=theme.font("display", 10),
               fill=dim)
    return img


def tiles(profile: DeviceProfile, t: float = 0.0) -> list[Image.Image]:
    """One image per key, cut from the shared canvas (bezel gaps skipped)."""
    canvas = _canvas(profile, int(t))
    kp = profile.key_px
    out = []
    for i in range(profile.keys):
        r, c = divmod(i, profile.cols)
        x = c * (kp + GAP)
        y = r * (kp + GAP)
        out.append(canvas.crop((x, y, x + kp, y + kp)))
    return out
