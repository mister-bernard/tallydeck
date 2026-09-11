"""Source registry: config `kind` → class."""

from .base import Source
from .claude_sessions import ClaudeSessionsSource
from .codex_sessions import CodexSessionsSource
from .watchdir import WatchDirSource
from .execsrc import ExecSource
from .demo import DemoSource
from .burn import TokenBurnSource
from .fleet import BackgroundFleetSource
from .grok_sessions import GrokSessionsSource

REGISTRY: dict[str, type[Source]] = {
    "claude-sessions": ClaudeSessionsSource,
    "codex-sessions": CodexSessionsSource,
    "grok-sessions": GrokSessionsSource,
    "watchdir": WatchDirSource,
    "exec": ExecSource,
    "demo": DemoSource,
    "tokenburn": TokenBurnSource,
    "background-fleet": BackgroundFleetSource,
}


def make(kind: str, **opts) -> Source:
    try:
        cls = REGISTRY[kind]
    except KeyError:
        raise ValueError(
            f"unknown source kind {kind!r} (have: {', '.join(REGISTRY)})")
    return cls(**opts)
