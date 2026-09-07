"""Demo source: a small scripted fleet for screenshots and dry runs.

The demo keys respond to presses like the real thing: a short press on a
hot (attention/blocked) key acks it — it flips green, stops flashing and
says so; pressing it again re-arms it. A long press hides the signal for
a while. That way the interaction model can be felt with no real agents.
"""

from __future__ import annotations

import math
import time

from ..signal import (Signal, ATTENTION, BLOCKED, WORKING, SUCCESS, IDLE,
                      OFFLINE)
from .base import Source


def _demo_lanes(crawl: float) -> list[dict]:
    """Two made-up accounts for the stacked info bar. Invented numbers — the
    point is the shape of the display, never anyone's real quota."""
    lanes = []
    for acct, pct, limit, tgt in (("A", 7 + crawl * 14, 3.6, 0.9),
                                  ("B", 3 + crawl * 6, 6.3, 0.7)):
        lanes.append({
            "id": acct,
            "pct": pct,
            "frac": pct / 100.0,
            "target": tgt,
            "remaining_s": 17160,
            "mid": f"{round(pct)}%  ·  {pct / 100.0 * limit:.1f}M "
                   f"of {tgt * limit:.1f}M",
            "clock": "4:46",
        })
    return lanes


class DemoSource(Source):
    group = "demo"

    def __init__(self, **opts):
        super().__init__(**opts)
        self._t0 = time.time()
        self._acked: set[str] = set()
        self._hidden: dict[str, float] = {}   # id → hide-until epoch

    def poll(self) -> list[Signal]:
        t = time.time() - self._t0
        crawl = (math.sin(t / 8) + 1) / 2          # slow 0→1→0 sweep
        signals = [
            Signal(id="demo/arb", label="arb bot",
                   sublabel="Decision needed: approve the widened spread, or "
                            "hold at the current band?",
                   detail="Decision needed: approve the widened spread, or "
                          "hold at the current band?",
                   state=ATTENTION, priority=5, group=self.group),
            Signal(id="demo/relay", label="relay", sublabel="tests red",
                   state=BLOCKED, progress=0.35, group=self.group),
            Signal(id="demo/audit", label="audit", sublabel="phase 2/3",
                   state=WORKING, progress=0.62, group=self.group),
            Signal(id="demo/indexer", label="indexer", sublabel="crawling",
                   state=WORKING, progress=crawl, group=self.group),
            Signal(id="demo/deploy", label="deploy", sublabel="shipped",
                   state=SUCCESS, progress=1.0, group=self.group),
            Signal(id="demo/scanner", label="scanner", sublabel="idle 2h",
                   state=IDLE, group=self.group),
            Signal(id="demo/miner", label="miner", sublabel="lost link",
                   state=OFFLINE, group=self.group),
            Signal(id="demo/burn", label="burn", state=WORKING, flash=False,
                   progress=min(1.0, 0.15 + crawl * 0.8), group=self.group,
                   meta={"meter": True, "frac": 0.15 + crawl * 1.2,
                         "left": f"{round((0.15 + crawl * 1.2) * 100)}%",
                         "mid": "A 15 · B 10", "right": "→ 01:00",
                         # Two fictional accounts, so the stacked-lane info
                         # bar shows up in demos and screenshots exactly as
                         # it does with a real token-burn API behind it.
                         "hot": "A",
                         "lanes": _demo_lanes(crawl)}),
        ]
        now = time.time()
        out = []
        for s in signals:
            if self._hidden.get(s.id, 0) > now:
                continue
            if s.id in self._acked:
                s.state = SUCCESS
                s.sublabel = "acked ✓"
                s.progress = None
            out.append(s)
        return out

    def on_press(self, sig: Signal, long: bool = False) -> bool:
        if long:
            self._hidden[sig.id] = time.time() + 30
            self._acked.discard(sig.id)
            return True
        if sig.id in self._acked:
            self._acked.discard(sig.id)      # press again to re-arm
        elif sig.state in (ATTENTION, BLOCKED, SUCCESS):
            self._acked.add(sig.id)
        else:
            return False                     # working/idle: nothing to ack
        return True
