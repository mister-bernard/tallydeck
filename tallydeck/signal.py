"""Signal — the unit of attention.

Everything tallydeck does is projecting a set of Signals onto a surface.
A Signal is a small, serializable statement: *this thing, in this state,
maybe this far along, maybe wants you*.

Sources emit them, the hub merges them, views arrange them, renderers
draw them. Nothing else crosses those boundaries.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict, replace
from typing import Any

# ── states ───────────────────────────────────────────────────────────────────
# Ordered by urgency. The wire format is the lowercase string.

BLOCKED = "blocked"        # hard stop: error, failed gate, approval required
ATTENTION = "attention"    # wants the operator now (agent finished / asked)
WORKING = "working"        # busy, no action needed
SUCCESS = "success"        # finished well; auto-expires
IDLE = "idle"              # alive but nothing happening
OFFLINE = "offline"        # stale / unreachable

STATES = (BLOCKED, ATTENTION, WORKING, SUCCESS, IDLE, OFFLINE)

# Higher weight sorts earlier on the deck.
_STATE_WEIGHT = {
    BLOCKED: 50,
    ATTENTION: 40,
    WORKING: 30,
    SUCCESS: 20,
    IDLE: 10,
    OFFLINE: 0,
}

# States that flash by default (a signal can override with `flash`).
FLASHING_STATES = {BLOCKED, ATTENTION}


@dataclass
class Signal:
    id: str                          # stable key, e.g. "cc/api-gateway"
    label: str                       # short name shown big on the key
    state: str = IDLE
    sublabel: str = ""               # second line, e.g. current phase
    detail: str = ""                 # long text (info bar / logs), not drawn on keys
    progress: float | None = None    # 0.0–1.0 mission progress, or None
    priority: int = 0                # tie-breaker within a state; higher = earlier
    group: str = ""                  # source namespace, e.g. "cc", "tasks"
    updated: float = field(default_factory=time.time)
    ttl: float | None = None         # seconds after `updated` until auto-expiry
    flash: bool | None = None        # override default flash behaviour
    color: str | None = None         # optional hex override, else themed by state
    action: dict[str, Any] | None = None  # server-side press action, e.g.
                                          # {"type": "cmd", "argv": [...]}
    meta: dict[str, Any] = field(default_factory=dict)

    # ── derived ──────────────────────────────────────────────────────────────

    @property
    def wants_flash(self) -> bool:
        if self.flash is not None:
            return self.flash
        return self.state in FLASHING_STATES

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.updated)

    def expired(self, now: float | None = None) -> bool:
        if self.ttl is None:
            return False
        return ((now or time.time()) - self.updated) > self.ttl

    def sort_key(self) -> tuple:
        """Deck order: urgency, then priority, then recency."""
        return (
            -_STATE_WEIGHT.get(self.state, 0),
            -self.priority,
            -self.updated,
        )

    # ── wire format ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        # Keep the wire lean: drop empty optionals.
        return {k: v for k, v in d.items() if v not in (None, "", {}, [])}

    @classmethod
    def from_dict(cls, d: dict) -> "Signal":
        if "id" not in d or "label" not in d:
            raise ValueError("signal requires at least: id, label")
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in d.items() if k in known}
        state = clean.get("state", IDLE)
        if state not in STATES:
            raise ValueError(f"unknown state {state!r} (valid: {', '.join(STATES)})")
        if clean.get("progress") is not None:
            clean["progress"] = min(1.0, max(0.0, float(clean["progress"])))
        return cls(**clean)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


def is_codex(sig: Signal) -> bool:
    """Does this signal belong to a Codex-harness session?

    `meta.harness` is the one canonical marker (sources and `tally raise`
    stamp it); renderers key the Codex look off this and nothing else, so a
    second harness later is a new value here rather than a new special case
    in every surface."""
    return str((sig.meta or {}).get("harness", "")).strip().lower() == "codex"


def is_grok(sig: Signal) -> bool:
    """Does this signal belong to a Grok Build session?

    Same contract as `is_codex`: one marker, renderers key the Grok look
    (tally bar along the bottom, badge X) off this and nothing else."""
    return str((sig.meta or {}).get("harness", "")).strip().lower() == "grok"


def rank(signals: list[Signal]) -> list[Signal]:
    """Deck ordering for a set of signals."""
    return sorted(signals, key=Signal.sort_key)


def rollup_same_pane(signals: list[Signal]) -> list[Signal]:
    """One key per live tmux pane.

    A hunt board, a Grok TUI, and leftover sessions that all sit on
    `cruiser-finder:1.1` are one Cruiser Finder, not a pad full of clones.
    Paneless signals and unique panes pass through. `main*` stays per-pane
    because those targets already differ (`mainA:1.1` vs `mainA:1.2`).
    """
    groups: dict[str, list[Signal]] = {}
    rest: list[Signal] = []
    for s in signals:
        pane = str((s.meta or {}).get("tmux") or "")
        if not pane:
            rest.append(s)
            continue
        groups.setdefault(pane, []).append(s)
    out = list(rest)
    for pane, members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        primary = _pane_primary(members)
        hottest = max(members, key=lambda m: (
            _STATE_WEIGHT.get(m.state, 0), m.updated))
        n_run = sum(1 for m in members if m.state == WORKING)
        extra = f"{len(members)} sessions"
        if n_run:
            extra += f" · {n_run} running"
        sub = extra if not primary.sublabel else f"{extra} · {primary.sublabel}"
        meta = dict(primary.meta or {})
        meta["rolled"] = len(members)
        meta["tmux"] = pane
        out.append(replace(
            primary,
            state=hottest.state,
            sublabel=sub,
            updated=max(m.updated for m in members),
            meta=meta,
        ))
    return out


def _pane_primary(members: list[Signal]) -> Signal:
    """Hunt board is the public face of a hunt pane; else the hottest."""
    def group_of(s: Signal) -> str:
        return (s.group or s.id.split("/", 1)[0] or "").lower()
    for g in ("hunt", "gk", "cc", "cx"):
        hit = [m for m in members if group_of(m) == g]
        if hit:
            return max(hit, key=lambda m: (
                _STATE_WEIGHT.get(m.state, 0), m.updated))
    return max(members, key=lambda m: (
        _STATE_WEIGHT.get(m.state, 0), m.updated))


def summarize(signals: list[Signal]) -> str:
    """One-line fleet summary, e.g. for the Neo info bar."""
    counts: dict[str, int] = {}
    for s in signals:
        counts[s.state] = counts.get(s.state, 0) + 1
    parts = []
    if counts.get(BLOCKED):
        parts.append(f"{counts[BLOCKED]} blocked")
    if counts.get(ATTENTION):
        parts.append(f"{counts[ATTENTION]} need you")
    if counts.get(WORKING):
        parts.append(f"{counts[WORKING]} working")
    if counts.get(SUCCESS):
        parts.append(f"{counts[SUCCESS]} done")
    if not parts:
        parts.append("all quiet")
    return " · ".join(parts)
