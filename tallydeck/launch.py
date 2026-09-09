"""Resolve dedicated-session harness/account in the caller's context."""
from __future__ import annotations

import json
import os
import pwd
import shutil
from pathlib import Path


def operator_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def ancestor_harness() -> str:
    pid = os.getppid()
    for _ in range(32):
        try:
            name = Path(f"/proc/{pid}/comm").read_text().strip()
            if name in ("codex", "claude", "claude-b"):
                return "codex" if name == "codex" else "claude"
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rfind(")") + 2:].split()[1])
            if pid <= 1:
                break
        except (OSError, ValueError, IndexError):
            break
    return ""


def caller_harness(env: dict) -> str:
    codex = bool(env.get("CODEX_THREAD_ID"))
    claude = any(env.get(k) for k in
                 ("CLAUDECODE", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"))
    if codex != claude:
        return "codex" if codex else "claude"
    # Nearest ancestor settles conflicting inherited markers. Detached offer
    # waiters receive the already resolved route, never re-detect the caller.
    return ancestor_harness() or env.get("TALLY_HARNESS", "") or "claude"


def _codex_alias(account: str, accounts: dict) -> str:
    """Map a spoken Codex account name to its tokenburn id: "1" → O, "2" → O2.

    G refers to the Codex accounts as 1 and 2 (Claude's are A and B), so
    `tally spawn -a 2` has to mean something. Exact ids always win, and an
    account that declares `aliases` in ~/.tokenburn.json is matched on those,
    so this never invents a mapping the config disagrees with.
    """
    want = str(account or "").strip().lower()
    if not want or want in {str(k).lower() for k in accounts}:
        return ""
    for aid, a in accounts.items():
        if any(str(x).strip().lower() == want for x in (a.get("aliases") or [])):
            return aid
    return ""


def resolve_launch(account: str = "", harness: str = "", *, env: dict | None = None) -> dict:
    env = dict(os.environ) if env is None else env
    home = operator_home()
    config = Path(env.get("TALLY_ACCOUNTS_FILE", home / ".tokenburn.json"))
    try:
        accounts = {a["id"]: a for a in json.loads(config.read_text()).get("accounts", [])}
    except FileNotFoundError:
        accounts = {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"cannot read account configuration: {config}") from e
    # Fallbacks for when ~/.tokenburn.json can't be read; the file is the real
    # source. O and O2 are the two Codex accounts (G calls them 1 and 2).
    defaults = {"A": "claude", "B": "claude", "O": "codex", "O2": "codex"}
    account = _codex_alias(account, accounts) or account
    if harness not in ("", "claude", "codex"):
        raise ValueError("harness must be claude or codex")
    if account:
        provider = accounts.get(account, {}).get("provider", defaults.get(account))
        selected = {"anthropic": "claude", "claude": "claude", "codex": "codex"}.get(provider)
        if not selected:
            raise ValueError(f"unsupported account: {account}")
        if harness and harness != selected:
            raise ValueError(f"account {account} does not use {harness}")
        harness = selected
    else:
        harness = harness or caller_harness(env)
        inherited = env.get("TALLY_ACCOUNT", "")
        provider = accounts.get(inherited, {}).get("provider", defaults.get(inherited))
        if provider in ({"claude", "anthropic"} if harness == "claude" else {"codex"}):
            account = inherited
        elif harness == "codex":
            # First ENABLED codex account, so parking account 1 moves the
            # default rather than spawning into a disabled lane. Falls back to
            # O when the config is unreadable.
            account = next((aid for aid, a in accounts.items()
                            if a.get("provider") == "codex"
                            and a.get("enabled", True)), "O")
        elif harness == "claude":
            account = "B" if any(".claude-b" in env.get(k, "") for k in ("HOME", "CLAUDE_CONFIG_DIR")) else "A"
        else:
            raise ValueError(f"unsupported calling harness: {harness}")
    settings = accounts.get(account, {})
    if harness == "codex":
        binary = env.get("CODEX_BIN") or settings.get("codex_bin") or shutil.which("codex")
        codex_home = settings.get("codex_home") or str(home / ".codex")
    else:
        override = "CLAUDE_BIN_B" if account == "B" else "CLAUDE_BIN"
        binary = env.get(override) or settings.get("claude_bin") or str(home / ".local/bin" / ("claude-b" if account == "B" else "claude"))
        codex_home = ""
    if not binary or not os.access(binary, os.X_OK) or not Path(binary).is_file():
        raise ValueError(f"{harness} binary not executable: {binary or '(not found)'}")
    return {"harness": harness, "account": account, "binary": str(Path(binary).absolute()),
            "home": str(home), "codex_home": str(codex_home)}


if __name__ == "__main__":
    import sys
    try:
        route = resolve_launch(*sys.argv[1:3])
        values = [route[k] for k in ("harness", "account", "binary", "home", "codex_home")]
        if any("\n" in v or "\r" in v for v in values):
            raise ValueError("launch settings cannot contain line breaks")
        print("\n".join(values))
    except ValueError as e:
        sys.exit(f"tally-launch: {e}")
