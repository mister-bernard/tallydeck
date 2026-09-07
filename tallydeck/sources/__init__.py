"""Source registry: config `kind` → class."""

from .base import Source
from .claude_sessions import ClaudeSessionsSource
from .watchdir import WatchDirSource
from .execsrc import ExecSource
from .demo import DemoSource
from .burn import TokenBurnSource

REGISTRY: dict[str, type[Source]] = {
    "claude-sessions": ClaudeSessionsSource,
    "watchdir": WatchDirSource,
    "exec": ExecSource,
    "demo": DemoSource,
    "tokenburn": TokenBurnSource,
}


def make(kind: str, **opts) -> Source:
    try:
        cls = REGISTRY[kind]
    except KeyError:
        raise ValueError(
            f"unknown source kind {kind!r} (have: {', '.join(REGISTRY)})")
    return cls(**opts)
