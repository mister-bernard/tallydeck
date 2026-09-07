"""Token-burn source: session-window usage across accounts → the deck meter.

Reads a tokenburn-style accounts API (default http://127.0.0.1:18795/accounts)
plus per-account burn *targets* from ~/.tokenburn.json, and emits a single
meter signal: how far into this session window's intended burn are we, and
when does the window end.

The meter aggregates every enabled anthropic account: burned and target
token counts are summed (pct × window limit per account), so accounts with
different limits and targets weigh in proportionally. frac > 1.0 means the
window target is already spent — the renderer goes molten.

If the API is unreachable the source emits nothing and the info bar falls
back to the fleet summary. Fail open, always.
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..signal import Signal, WORKING
from .base import Source

DEFAULT_URL = "http://127.0.0.1:18795/accounts"
DEFAULT_CONFIG = Path.home() / ".tokenburn.json"


class TokenBurnSource(Source):
    """opts: url, config (targets json), every (sec), tz (for reset time)."""

    group = "burn"

    def __init__(self, **opts):
        super().__init__(**opts)
        self.url = opts.get("url", DEFAULT_URL)
        self.config = Path(opts.get("config", DEFAULT_CONFIG))
        self.every = float(opts.get("every", 20))
        # Unset → this machine's local zone; set `tz` when the deck sits in a
        # different one from the hub.
        self.tz = opts.get("tz") or ""
        self._last_run = 0.0
        self._cache: list[Signal] = []
        # Window-% history per account, for "which account is burning NOW":
        # the lane whose countdown gets the underline.
        self.hot_window = float(opts.get("hot_window", 300))
        self._pct_hist: dict[str, list[tuple[float, float]]] = {}

    # ── polling ──────────────────────────────────────────────────────────────

    def poll(self) -> list[Signal]:
        now = time.time()
        if now - self._last_run < self.every:
            return self._cache
        self._last_run = now
        try:
            with urllib.request.urlopen(self.url, timeout=3) as r:
                payload = json.load(r)
        except Exception:
            self._cache = []
            return self._cache
        self._cache = self.signals_from(payload, self._targets())
        return self._cache

    def _targets(self) -> dict:
        try:
            cfg = json.loads(self.config.read_text())
            return {a.get("id"): a for a in cfg.get("accounts", [])}
        except (OSError, json.JSONDecodeError):
            return {}

    # ── pure computation (unit-testable) ─────────────────────────────────────

    def signals_from(self, payload: dict, targets: dict, now: float | None = None
                     ) -> list[Signal]:
        now = time.time() if now is None else now
        burned = target = 0.0
        parts: list[str] = []
        lanes: list[dict] = []
        for acct in payload.get("accounts", []):
            if acct.get("provider") != "anthropic" or not acct.get("enabled"):
                continue
            pct = acct.get("session_pct")
            if pct is None:
                continue
            t = targets.get(acct.get("id"), {})
            limit = float(t.get("window_5h_limit", 9_000_000))
            tgt_pct = float(t.get("target_pct_5h", 100))
            burned += pct / 100.0 * limit
            target += tgt_pct / 100.0 * limit
            parts.append(f"{acct.get('id')} {round(pct)}")
            # Each account gets its own lane: its own fill and its own clock.
            # Aggregating them hid the thing that actually matters — WHICH
            # account is close to its ceiling, and when that ceiling lifts.
            remaining = self._remaining(acct.get("session_reset"), now)
            lanes.append({
                "id": str(acct.get("id")),
                "pct": float(pct),
                # Fill matches the number engraved on the lane — fraction of
                # the WINDOW, not of the target. The target is drawn as a
                # notch on the bar instead ("26%" filling 65% of the bar
                # because the target was 40% read as nonsense).
                "frac": pct / 100.0,
                "target": tgt_pct / 100.0,
                "remaining_s": remaining,
                "reset_clock": self._fmt_reset(acct.get("session_reset") or ""),
                # Engraved on the lane itself: window % + absolute burn in the
                # middle, bare countdown at the right end (the lane's badge
                # already names the account — no "A 1:42" repetition).
                "mid": f"{round(pct)}%  ·  {pct / 100.0 * limit / 1e6:.1f}M "
                       f"of {tgt_pct / 100.0 * limit / 1e6:.1f}M",
                "clock": self._fmt_remaining(remaining),
            })
        if not parts or target <= 0:
            return []
        frac = burned / target
        # Underline the account that is actually burning right now — the max
        # window-% growth over the hot window. Nothing moving → no underline.
        rates = {l["id"]: self._pct_rate(l["id"], now, l["pct"]) for l in lanes}
        hot = max(rates, key=rates.get) if rates and max(rates.values()) > 0 \
            else ""
        return [Signal(
            id="burn/session",
            label="burn",
            state=WORKING,
            progress=min(1.0, frac),
            flash=False,
            group=self.group,
            meta={
                "meter": True,
                "frac": frac,
                "left": f"{round(frac * 100)}%",
                "mid": " · ".join(parts),
                # Was the reset CLOCK TIME ("01:00") formatted so it read like a
                # duration — which is exactly how it was misread. Now it is a
                # real countdown per account, soonest one marked.
                "right": "  ".join(
                    f"{l['id']} {self._fmt_remaining(l['remaining_s'])}"
                    for l in lanes if l["remaining_s"] is not None
                ),
                "lanes": lanes,
                "hot": hot,
            },
        )]

    def _pct_rate(self, acct: str, now: float, pct: float) -> float:
        """Window-% growth per second over the hot window (≥ 0)."""
        hist = self._pct_hist.setdefault(acct, [])
        hist.append((now, pct))
        while hist and now - hist[0][0] > self.hot_window:
            hist.pop(0)
        if len(hist) < 2:
            return 0.0
        dt = hist[-1][0] - hist[0][0]
        return max(0.0, (hist[-1][1] - hist[0][1]) / max(dt, 1.0))

    @staticmethod
    def _remaining(iso: str | None, now: float) -> float | None:
        """Seconds until the window resets, or None if unknown/expired."""
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Clamp at zero: a negative countdown means the window already rolled
        # and the API has not caught up. Showing "-0:03" would be alarming and
        # meaningless; showing 0:00 is honest.
        return max(0.0, dt.timestamp() - now)

    @staticmethod
    def _fmt_remaining(secs: float | None) -> str:
        if secs is None:
            return ""
        m = int(secs // 60)
        return f"{m // 60}:{m % 60:02d}"

    def _fmt_reset(self, iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso)
            local = dt.astimezone(ZoneInfo(self.tz) if self.tz else None)
            return local.strftime("%H:%M")
        except (ValueError, KeyError):
            return ""
