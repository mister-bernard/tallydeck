"""Source — anything that can emit Signals.

A source owns a `group` namespace and returns its full current truth on
every poll(); the hub treats missing signals as gone. Implement poll(),
optionally on_press() to handle key presses for your signals hub-side.
"""

from __future__ import annotations

from ..signal import Signal


class Source:
    group = "src"

    def __init__(self, **opts):
        self.opts = opts
        self.group = opts.get("group", self.group)

    def poll(self) -> list[Signal]:
        raise NotImplementedError

    def on_press(self, sig: Signal, long: bool = False) -> bool:
        """Return True if the press was handled here."""
        return False
