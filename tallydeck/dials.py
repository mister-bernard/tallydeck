"""Dial state for the Stream Deck +: a turn is a PREVIEW, nothing else.

Turning a dial steps a candidate through that zone's `options` and shows it
in the zone; nothing is sent anywhere. Landing back on the current value
cancels; PREVIEW_FOR idle seconds cancel. The push that applies a candidate
is a separate act with its own gate (docs/TALLYDECK-DIALS.md, steps 3–5).

Pure functions over plain data so the whole behaviour is testable without a
deck: `pending` is {dial: {"idx": int, "until": epoch}}.
"""

from __future__ import annotations

import copy
import time

PREVIEW_FOR = 5.0     # seconds a candidate stays up with no further turn


def _zone(zones, dial: int):
    if 0 <= dial < len(zones) and isinstance(zones[dial], dict):
        return zones[dial]
    return None


def expire(pending: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {d: p for d, p in pending.items()
            if isinstance(p, dict) and float(p.get("until", 0)) > now}


def step(zones, pending: dict, dial: int, delta: int,
         now: float | None = None) -> dict:
    """The new pending map after `delta` detents on `dial`. Clamped at the
    ends (a mashed dial parks at an end, never wraps past what you meant); a
    zone without options is inert; returning to the current value cancels."""
    now = time.time() if now is None else now
    pend = expire(pending, now)
    z = _zone(zones, dial)
    opts = [o for o in ((z or {}).get("options") or []) if isinstance(o, dict)]
    if not opts or not delta:
        return pend
    ids = [str(o.get("id")) for o in opts]
    cur = ids.index(str(z.get("current"))) if str(z.get("current")) in ids else 0
    idx = int(pend.get(dial, {}).get("idx", cur))
    idx = max(0, min(len(opts) - 1, idx + int(delta)))
    if idx == cur:
        pend.pop(dial, None)
    else:
        pend[dial] = {"idx": idx, "until": now + PREVIEW_FOR}
    return pend


def overlay(zones, pending: dict, now: float | None = None,
            hint: str = "preview") -> list:
    """A COPY of `zones` with each live preview written into its zone:
    `pending` (the candidate's label), `pending_id`, and the sub line
    replaced by `hint` so the zone says what a push would do (or not do)."""
    now = time.time() if now is None else now
    out = copy.deepcopy(list(zones or []))
    for dial, p in expire(pending, now).items():
        z = _zone(out, dial)
        if z is None:
            continue
        opts = [o for o in (z.get("options") or []) if isinstance(o, dict)]
        idx = int(p.get("idx", -1))
        if 0 <= idx < len(opts):
            o = opts[idx]
            z["pending"] = str(o.get("label") or o.get("id"))
            z["pending_id"] = str(o.get("id"))
            z["sub"] = hint
    return out
