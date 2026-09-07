"""Watch-directory source: the universal escape hatch.

Any script, agent, cron job, or one-liner can claim a key by dropping a
JSON file into the watch dir (default ~/.tallydeck/signals/):

  $ tally raise deploy --state blocked --label "prod deploy" --sublabel "gate 3"

or equivalently:

  $ cat > ~/.tallydeck/signals/deploy.json <<'EOF'
  {"label": "prod deploy", "state": "blocked", "sublabel": "gate 3",
   "progress": 0.4, "ttl": 3600}
  EOF

The filename (sans .json) becomes the signal id within this source's
group. Delete the file — or let its ttl lapse — and the key frees up.
A short press on one of these keys acknowledges it: attention/blocked
signals are downgraded in place; a long press deletes the file.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..signal import Signal, ATTENTION, BLOCKED, IDLE
from .base import Source

DEFAULT_DIR = Path.home() / ".tallydeck" / "signals"


class WatchDirSource(Source):
    """opts: path (str), group (default 'sig')."""

    group = "sig"

    def __init__(self, **opts):
        super().__init__(**opts)
        self.path = Path(opts.get("path", DEFAULT_DIR)).expanduser()

    def poll(self) -> list[Signal]:
        signals: list[Signal] = []
        if not self.path.is_dir():
            return signals
        for fp in sorted(self.path.glob("*.json")):
            try:
                d = json.loads(fp.read_text())
                d.setdefault("id", f"{self.group}/{fp.stem}")
                d.setdefault("label", fp.stem)
                d.setdefault("updated", fp.stat().st_mtime)
                sig = Signal.from_dict(d)
            except (OSError, ValueError, json.JSONDecodeError):
                # A malformed drop becomes a visible complaint, not silence.
                sig = Signal(id=f"{self.group}/{fp.stem}", label=fp.stem,
                             sublabel="bad json", state=BLOCKED)
            sig.meta["file"] = str(fp)
            if sig.expired():
                fp.unlink(missing_ok=True)
                continue
            signals.append(sig)
        return signals

    def on_press(self, sig: Signal, long: bool = False) -> bool:
        fp = Path(sig.meta.get("file", ""))
        if not fp.is_file():
            return False
        if long:
            fp.unlink(missing_ok=True)
            return True
        if sig.meta.get("session"):
            # Backed by a live session: the press routes the operator there.
            # Auto-acking here cleared flashes the operator never actually saw.
            return False
        if sig.state in (ATTENTION, BLOCKED):
            try:
                d = json.loads(fp.read_text())
                d["state"] = IDLE
                d["sublabel"] = "acked"
                fp.write_text(json.dumps(d, indent=2))
                return True
            except (OSError, json.JSONDecodeError):
                return False
        return False
