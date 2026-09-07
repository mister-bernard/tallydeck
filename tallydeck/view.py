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
    offpage_urgent: bool = False     # something NOT visible needs the human


@dataclass
class View:
    profile: DeviceProfile
    pinned: list[str] = field(default_factory=list)   # signal ids to fix first
    hide_idle: bool = False
    fill: str = "columns"    # "columns": rank flows top-left ↓ then next
    page: int = 0            # column; "rows": left→right per row
    sticky: bool = True      # a signal keeps its key while visible — keys
    _slots: dict = field(default_factory=dict)  # must not move under a finger

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
        # Relative heat: each key's burn priority against the hottest visible
        # one. Renderers tint backgrounds with it; recomputed every layout so
        # it tracks the fleet, not an absolute scale nobody calibrated.
        hottest = max((s.priority for s in window if s.priority > 0),
                      default=0)
        for s in window:
            s.meta["heat"] = (s.priority / hottest) if hottest else 0.0

        rows, cols = self.profile.rows, self.profile.cols
        # Column-major position sequence: rank #1 top-left, #2 below it…
        seq = [(i % rows) * cols + (i // rows) for i in range(per_page)] \
            if self.fill == "columns" else list(range(per_page))

        keys: list[Signal | None] = [None] * per_page
        if self.sticky and pages == 1:
            # STABILITY BEATS PERFECT RANK: a key must not move between the
            # glance and the press. Signals keep their slot while visible;
            # rank only decides where NEWCOMERS land (best free position).
            # With multiple pages, ranking takes over — a crowded fleet needs
            # order more than stillness.
            alive = {s.id for s in window}
            self._slots = {sid: pos for sid, pos in self._slots.items()
                           if sid in alive}
            used = set(self._slots.values())
            free = [p for p in seq if p not in used]
            for s in window:
                if s.id not in self._slots:
                    if not free:
                        break
                    self._slots[s.id] = free.pop(0)
            by_id = {s.id: s for s in window}
            for sid, pos in self._slots.items():
                keys[pos] = by_id[sid]
        else:
            for i, s in enumerate(window):
                keys[seq[i]] = s

        self.last_order = [s.id for s in ordered]   # for beacon page-jumps
        urgent = {"blocked", "attention"}
        offpage = any(s.state in urgent
                      for s in ordered[:self.page * per_page]
                      + ordered[(self.page + 1) * per_page:])
        return Layout(keys=keys, page=self.page, pages=pages,
                      summary=summarize(signals),
                      meter=meters[0] if meters else None,
                      offpage_urgent=offpage)

    def jump_to(self, sid: str) -> None:
        """Flip to the page holding this signal (rank order, latest layout)."""
        order = getattr(self, "last_order", [])
        if sid in order:
            self.page = order.index(sid) // self.profile.keys

    def page_next(self) -> None:
        self.page += 1     # clamped on next layout()

    def page_prev(self) -> None:
        self.page = max(0, self.page - 1)
