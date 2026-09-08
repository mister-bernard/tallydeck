"""Where tallydeck keeps per-user state: signals, acks, the brief cache.

Per USER, not per $HOME. A second Claude account runs its sessions under a
different HOME (the CLAUDE_CONFIG_DIR pattern), so a hook or `tally raise`
fired from inside one of those sessions used to write into a directory the
hub never watched: permission prompts on that account never reached the
deck, and every attention flag raised there silently vanished. The passwd
home is the one thing every process of one user agrees on.

Override with $TALLYDECK_STATE (tests, unusual layouts).
"""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    env = os.environ.get("TALLYDECK_STATE")
    if env:
        return Path(env).expanduser()
    home = ""
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        pass
    return Path(home or Path.home()) / ".tallydeck"


def signals_dir() -> Path:
    return state_dir() / "signals"


def acked_dir() -> Path:
    return state_dir() / "acked"


def cache_dir() -> Path:
    return state_dir() / "cache"
