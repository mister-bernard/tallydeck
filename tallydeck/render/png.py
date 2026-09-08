"""PNG renderer: draws the whole deck as a contact sheet.

Used headless — for development without hardware, for CI snapshots, and
for sending the operator a picture of what the deck looks like right now.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from ..devices import DeviceProfile
from ..view import Layout
from . import theme
from .keycard import draw_key, draw_screen
from .meter import draw_meter

GAP = 14          # px between keys
BEZEL = 26        # px around the key field
CORNER = 22


def _screen_face(profile: DeviceProfile, layout: Layout, t: float):
    if layout.meter is not None:
        m = layout.meter.meta
        lanes = [l for l in (m.get("lanes") or []) if isinstance(l, dict)]
        if len(lanes) >= 2:
            from .meter import draw_meter2
            return draw_meter2(profile.screen_px, lanes, m.get("codex"),
                               hot=m.get("hot", ""), t=t)
        return draw_meter(profile.screen_px,
                          float(m.get("frac", layout.meter.progress or 0.0)),
                          m.get("left", ""), m.get("mid", ""),
                          m.get("right", ""), t=t,
                          lanes=m.get("lanes"), hot=m.get("hot", ""))
    return draw_screen(profile.screen_px, layout.summary,
                       layout.page, layout.pages)


def render_png(profile: DeviceProfile, layout: Layout,
               lit: dict[str, bool] | None = None,
               scale: int = 2, t: float = 0.0,
               fx: dict | None = None) -> Image.Image:
    """Render the deck face. `lit` maps signal id → flash frame on/off;
    `fx` maps key index → fireworks phase 0..1."""
    lit = lit or {}
    fx = fx or {}
    kp = profile.key_px * scale
    gap, bez = GAP * scale, BEZEL * scale

    field_w = profile.cols * kp + (profile.cols - 1) * gap
    field_h = profile.rows * kp + (profile.rows - 1) * gap
    screen_h = 0
    if profile.screen_px:
        screen_h = profile.screen_px[1] * scale + gap

    W = field_w + 2 * bez
    H = field_h + 2 * bez + screen_h
    img = Image.new("RGB", (W, H), "#000000")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=CORNER * scale,
                        fill="#16171B", outline="#26282E", width=scale)

    faces = None
    if layout.mural:
        from . import mural
        faces = mural.tiles(profile, t)
    for i, sig in enumerate(layout.keys):
        r, c = divmod(i, profile.cols)
        x = bez + c * (kp + gap)
        y = bez + r * (kp + gap)
        if faces is not None:
            face = faces[i].resize((kp, kp))
        else:
            face = draw_key(sig, profile.key_px,
                            lit=bool(sig and lit.get(sig.id)),
                            askpage=int(t / 2))
            if i in fx and sig is not None:
                from .fx import fireworks
                face = fireworks(face, fx[i], sig.color or theme.STATE_COLOR.get(
                    sig.state, theme.STATE_COLOR["idle"]), seed=sig.id)
            face = face.resize((kp, kp))
        img.paste(face, (x, y))
        d.rounded_rectangle([x - 1, y - 1, x + kp, y + kp],
                            radius=6 * scale, outline="#000000", width=scale)

    if profile.screen_px:
        sw = profile.screen_px[0] * scale
        sh = profile.screen_px[1] * scale
        sx = (W - sw) // 2
        sy = bez + field_h + gap
        scr = _screen_face(profile, layout, t).resize((sw, sh))
        img.paste(scr, (sx, sy))
        d.rounded_rectangle([sx - 1, sy - 1, sx + sw, sy + sh],
                            radius=4 * scale, outline="#000000", width=scale)

    return img
