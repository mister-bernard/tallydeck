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

Pressing one of these keys never clears it by itself. An alarm must not
vanish on an action whose result the operator could not see — a short
press used to downgrade the signal in place, so pressing a flashing key
"acknowledged" an ask nobody had read. Now a short press is the client's
to route (it shows the ask); only the popup's explicit "done", `tally
clear`, or a long press retires the file.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..signal import Signal, BLOCKED
from ..paths import signals_dir
from .base import Source

DEFAULT_DIR = signals_dir()


class WatchDirSource(Source):
    """opts: path (str), group (default 'sig')."""

    group = "sig"

    def __init__(self, **opts):
        super().__init__(**opts)
        self.path = Path(opts.get("path") or signals_dir()).expanduser()

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
            fp.unlink(missing_ok=True)   # the one explicit hub-side dismiss
            return True
        # Short press: hands-off. The client routes it (popup with the ask,
        # or the session that raised it); nothing here may change state.
        return False
