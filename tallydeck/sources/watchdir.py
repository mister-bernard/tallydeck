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
import shutil
from pathlib import Path

from ..signal import Signal, BLOCKED
from ..paths import signals_dir
from .base import Source

DEFAULT_DIR = signals_dir()


class WatchDirSource(Source):
    """opts: path (str), group (default 'sig')."""

    group = "sig"

    @staticmethod
    def _default_decide_cmd() -> str:
        """Where the press action lives, resolved rather than hardcoded.

        This shipped briefly as an absolute /home/openclaw path, which is a host
        detail with no business in a public repo and is wrong on any other machine.
        Prefer the copy that travels with the checkout, then anything installed on
        PATH, and only then the original location so existing deployments keep
        working.
        """
        here = Path(__file__).resolve().parents[2] / "contrib" / "tally-popup-decide"
        if here.is_file():
            return str(here)
        found = shutil.which("tally-popup-decide")
        if found:
            return found
        return "/home/openclaw/scripts/tally-popup-decide.sh"

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
            # A raised flag that carries a `detail` is a QUESTION, so make pressing it
            # answer the question by default. Without this the hub logs
            # "press <id> (no action)" and whether anything happens depends entirely on
            # the operator's client-side on_press being configured on another machine —
            # which is exactly how two rounds were lost on 2026-09-08. The hub runs
            # tmux, so it can put the popup up itself. An explicit action still wins.
            # `ask-<sid>.json` is a LIVE Claude session parked at its own prompt, not a
            # question this deck can answer. Its answer has to be typed into that pane;
            # capturing it in a popup would write to a decisions log the waiting session
            # never reads, and the session would sit there blocked regardless. Those keys
            # keep the default routing, which takes you to the pane. Only agent-raised
            # flags get the answer-here treatment.
            is_session_ask = fp.stem.startswith("ask-")
            if sig.action is None and not is_session_ask and (sig.detail or "").strip():
                sig.action = {"type": "cmd",
                              "argv": [self._decide_cmd(), fp.stem]}
            if sig.expired():
                fp.unlink(missing_ok=True)
                continue
            signals.append(sig)
        return signals

    def _decide_cmd(self) -> str:
        cmd = getattr(self, "_decide_cached", None)
        if cmd is None:
            cmd = self._decide_cached = str(
                getattr(self, "decide_cmd", "") or self._default_decide_cmd())
        return cmd

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
