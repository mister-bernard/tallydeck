"""Demo source: a small scripted fleet for screenshots and dry runs."""

from __future__ import annotations

import math
import time

from ..signal import (Signal, ATTENTION, BLOCKED, WORKING, SUCCESS, IDLE,
                      OFFLINE)
from .base import Source


class DemoSource(Source):
    group = "demo"

    def __init__(self, **opts):
        super().__init__(**opts)
        self._t0 = time.time()

    def poll(self) -> list[Signal]:
        t = time.time() - self._t0
        crawl = (math.sin(t / 8) + 1) / 2          # slow 0→1→0 sweep
        return [
            Signal(id="demo/arb", label="arb bot", sublabel="awaiting ack",
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
        ]
