"""Every terminal state must render its text in a colour that isn't its background.

Regression guard for 2026-09-07: `idle` was "\033[90;100m" — ANSI 90 and 100 are
the SAME palette entry (bright black) as foreground and background. Idle keys
therefore drew their labels in exactly their own background colour and appeared
as blank grey blocks on a live fleet. The signals were arriving correctly the
whole time; they were simply invisible, which is the worst kind of broken.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "tallydeck" / "render" / "term.py"


def _palette_index(code: int) -> int:
    """Map an SGR colour code to its palette slot, so fg and bg are comparable."""
    if 30 <= code <= 37:
        return code - 30
    if 90 <= code <= 97:
        return code - 90 + 8
    if 40 <= code <= 47:
        return code - 40
    if 100 <= code <= 107:
        return code - 100 + 8
    raise AssertionError(f"unrecognised SGR colour code: {code}")


def test_no_state_is_invisible():
    pairs = dict(re.findall(r'"(\w+)":\s*"\\033\[(\d+;\d+)m"', SRC.read_text()))
    assert pairs, "could not parse the _ANSI table — did the format change?"
    for state, code in pairs.items():
        fg, bg = (int(x) for x in code.split(";"))
        assert _palette_index(fg) != _palette_index(bg), (
            f"state {state!r} draws text in its own background colour ({code}) "
            f"— its keys will render blank"
        )
