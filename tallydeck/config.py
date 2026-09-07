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

# Shipped config, versioned with the code. Everything here is machine-neutral
# and carries no credentials, so it lives in the repo and arrives with a pull —
# no hand-editing a config file on every machine after every change.
REPO_PATH = Path(__file__).resolve().parent.parent / "config" / "tallydeck.toml"

DEFAULTS: dict = {
    "view": {"device": "neo", "hide_idle": False, "pinned": []},
    "sources": [{"kind": "claude-sessions"}, {"kind": "watchdir"},
                {"kind": "tokenburn"}],   # meter vanishes if API absent
    "client": {},
}


def _merge(cfg: dict, user: dict) -> None:
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v


def load(path: str | Path | None = None) -> dict:
    """Built-in defaults, then the repo config, then the user's, last wins.

    Three layers rather than one, because the two ends of this system need
    genuinely different settings — a Mac's `on_press` path is meaningless on
    the hub — and because config that has to be hand-edited on each machine
    drifts out of step with the code that reads it.

    The repo layer is tracked. It must therefore stay free of anything secret:
    hosts and paths yes, tokens and keys never. Anything private belongs in the
    user layer, which is not in the repo and overrides the repo layer anyway.

    An explicit --config or $TALLYDECK_CONFIG replaces only the USER layer; the
    repo layer still loads underneath it.
    """
    cfg = {k: (v.copy() if isinstance(v, dict) else list(v))
           for k, v in DEFAULTS.items()}
    for p in (REPO_PATH, Path(path) if path else DEFAULT_PATH):
        if p.is_file():
            with open(p, "rb") as fh:
                _merge(cfg, tomllib.load(fh))
    return cfg
