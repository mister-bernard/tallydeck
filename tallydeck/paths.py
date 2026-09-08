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


SAFE_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def safe_id(sid: str) -> bool:
    """A signal id is a filename component and nothing else."""
    return bool(SAFE_ID.match(sid or "")) and ".." not in sid


def write_json_atomic(path: Path, obj, exclusive: bool = False) -> bool:
    """Write JSON so a concurrent reader never sees a torn file: temp file
    in the same dir, then os.replace. With exclusive=True the write only
    succeeds if `path` does not exist yet (os.link is atomic on POSIX) —
    the first surface to answer wins, the second learns it lost. Returns
    False when exclusive and the file already exists."""
    import json, os, tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(obj))
        if exclusive:
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False
            return True
        os.replace(tmp, path)
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_json(path: Path):
    """None on missing, torn, or invalid — callers retry rather than act."""
    import json
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def answer_quiet() -> Path:
    """Kill switch for the phone round-trip (both directions). Present =
    tally-notify sends nothing and the chat hook matches nothing, with no
    restart of anything. Mirrors crypto-quiet / devcycle-quiet."""
    return state_dir() / "answer-quiet"


def private_dir(p: Path) -> Path:
    """mkdir -p with 0700: decisions are the operator's business."""
    import os
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def signals_dir() -> Path:
    return state_dir() / "signals"


def acked_dir() -> Path:
    return state_dir() / "acked"


def cache_dir() -> Path:
    return state_dir() / "cache"


def hush_file() -> Path:
    """Do-not-disturb for decision notifications. JSON {"until": epoch|null,
    "since": epoch}. Present = hushed (until the timestamp, or until
    removed). The deck is unaffected — keys are not a distraction — and
    nothing is lost: questions still raised go out the moment it lifts."""
    return state_dir() / "hush"


def hushed(now: float | None = None) -> float | None:
    """None if not hushed; else the epoch it lifts (inf = until unhush)."""
    import json, time as _t
    p = hush_file()
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    until = d.get("until")
    now = now or _t.time()
    if until is None:
        return float("inf")
    if float(until) <= now:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return float(until)


def set_hush(seconds: float | None) -> float | None:
    """Hush for `seconds` (None = until unhush). Returns the lift epoch."""
    import json, time as _t
    p = hush_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    until = None if seconds is None else _t.time() + float(seconds)
    p.write_text(json.dumps({"until": until, "since": _t.time()}))
    return float("inf") if until is None else until


def clear_hush() -> bool:
    p = hush_file()
    was = p.exists()
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    return was


def parse_duration(s: str) -> float | None:
    """'2h' '45m' '1d' '90' (minutes) → seconds; '' → None (open-ended)."""
    import re
    s = (s or "").strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|d|day|days)?", s)
    if not m:
        raise ValueError(f"not a duration: {s!r}")
    n = float(m.group(1)); u = (m.group(2) or "m")[0]
    return n * {"h": 3600, "m": 60, "d": 86400}[u]


def hub_alive() -> Path:
    """Heartbeat the hub touches while a deck client is connected. Other
    surfaces (the Signal notifier) read its mtime to know whether the
    operator is at the deck — questions go there first, and to the phone
    only when nobody is plugged in."""
    return state_dir() / "hub.alive"
