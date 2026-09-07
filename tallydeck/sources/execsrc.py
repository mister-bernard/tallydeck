"""Exec source: poll any program that prints signal JSON.

The command is run on each hub tick (rate-limited by `every`) and must
print either a JSON array of signal objects or NDJSON, one per line.
This is how you graft in anything with a CLI — CI status, systemd
sweeps, task queues — without writing a Python source.

Config example:

  [[sources]]
  kind = "exec"
  group = "svc"
  argv = ["/home/me/bin/service-signals.sh"]
  every = 30
"""

from __future__ import annotations

import json
import subprocess
import time

from ..signal import Signal
from .base import Source


class ExecSource(Source):
    """opts: argv (list[str]), every (sec, default 15), timeout (default 10)."""

    group = "exec"

    def __init__(self, **opts):
        super().__init__(**opts)
        self.argv = [str(a) for a in opts["argv"]]
        self.every = float(opts.get("every", 15))
        self.timeout = float(opts.get("timeout", 10))
        self._last_run = 0.0
        self._cache: list[Signal] = []

    def poll(self) -> list[Signal]:
        now = time.time()
        if now - self._last_run < self.every:
            return self._cache
        self._last_run = now
        out = subprocess.run(self.argv, capture_output=True, text=True,
                             timeout=self.timeout)
        if out.returncode != 0:
            raise RuntimeError(
                f"{self.argv[0]} exited {out.returncode}: {out.stderr.strip()[:200]}")
        text = out.stdout.strip()
        if not text:
            self._cache = []
            return self._cache
        try:
            payload = json.loads(text)
            items = payload if isinstance(payload, list) else [payload]
        except json.JSONDecodeError:  # NDJSON fallback
            items = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        signals = []
        for d in items:
            d.setdefault("id", f"{self.group}/{d.get('label', '?')}")
            signals.append(Signal.from_dict(d))
        self._cache = signals
        return signals
