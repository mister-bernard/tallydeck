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

  [brief]
  tasks_cmd = ["python3", "~/bin/my-task-queue.py", "list"]  # optional

  [client]
  connect = ["ssh", "myhub", "tallyd"]   # an ssh alias; omit for local mode
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
    """Overlay `user` onto `cfg`. Dicts update keywise; most other values replace.

    `sources` is the exception: kinds the overlay names replace the lower
    layer's entries of that kind, but kinds it does not name stay. A user
    file written for Claude+Codex must not silently drop grok-sessions the
    next time the tracked config grows a harness. Matching is by `kind`
    only — two Codex accounts in the user file still replace the repo's
    single Codex source, which is the point of the user file.
    """
    for k, v in user.items():
        if k == "sources" and isinstance(v, list) and isinstance(cfg.get(k), list):
            named = {s.get("kind") for s in v if isinstance(s, dict)}
            extras = [s for s in cfg[k]
                      if isinstance(s, dict) and s.get("kind") not in named]
            cfg[k] = list(v) + extras
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
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
