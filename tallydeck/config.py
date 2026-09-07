"""Config loading: ~/.config/tallydeck/config.toml (or --config PATH).

Example:

  [view]
  device = "neo"
  hide_idle = false
  pinned = ["sig/deploy"]

  [[sources]]
  kind = "claude-sessions"

  [[sources]]
  kind = "watchdir"

  [[sources]]
  kind = "exec"
  group = "svc"
  argv = ["~/bin/service-signals.sh"]
  every = 30

  [client]
  connect = ["ssh", "vps", "tallyd"]   # omit for local mode
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get(
    "TALLYDECK_CONFIG",
    Path.home() / ".config" / "tallydeck" / "config.toml"))

DEFAULTS: dict = {
    "view": {"device": "neo", "hide_idle": False, "pinned": []},
    "sources": [{"kind": "claude-sessions"}, {"kind": "watchdir"}],
    "client": {},
}


def load(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_PATH
    cfg = {k: (v.copy() if isinstance(v, dict) else list(v))
           for k, v in DEFAULTS.items()}
    if p.is_file():
        with open(p, "rb") as fh:
            user = tomllib.load(fh)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg
