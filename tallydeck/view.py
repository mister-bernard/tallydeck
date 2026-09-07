"""View: arrange ranked signals onto a device's key grid, with paging.

The view is deliberately dumb: ranking lives in signal.sort_key(), drawing
lives in render/. This just decides *which signal sits on which key* —
pinned ids first (in config order), then everything else by rank, split
into pages. Key 0 is top-left; keys read left-to-right, top-to-bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .devices import DeviceProfile
from .signal import Signal, rank, summarize


@dataclass
class Layout:
    """One frame's worth of placement."""
    keys: list[Signal | None]        # len == profile.keys; None = empty key
    page: int
    pages: int
    summary: str
    meter: Signal | None = None      # meta.meter signal → info bar, not a key


@dataclass
class View:
    profile: DeviceProfile
    pinned: list[str] = field(default_factory=list)   # signal ids to fix first
    hide_idle: bool = False
    page: int = 0

    def layout(self, signals: list[Signal]) -> Layout:
        meters = [s for s in signals if s.meta.get("meter")]
        signals = [s for s in signals if not s.meta.get("meter")]
        if self.hide_idle:
            signals = [s for s in signals if s.state not in ("idle", "offline")]

        by_id = {s.id: s for s in signals}
        head = [by_id[i] for i in self.pinned if i in by_id]
        rest = rank([s for s in signals if s.id not in set(self.pinned)])
        ordered = head + rest

        per_page = self.profile.keys
        pages = max(1, -(-len(ordered) // per_page))
        self.page = max(0, min(self.page, pages - 1))
        window = ordered[self.page * per_page:(self.page + 1) * per_page]
        keys: list[Signal | None] = list(window) + \
            [None] * (per_page - len(window))
        return Layout(keys=keys, page=self.page, pages=pages,
                      summary=summarize(signals),
                      meter=meters[0] if meters else None)

    def page_next(self) -> None:
        self.page += 1     # clamped on next layout()

    def page_prev(self) -> None:
        self.page = max(0, self.page - 1)
