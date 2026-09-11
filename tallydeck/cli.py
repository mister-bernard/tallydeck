"""tally — CLI entry points.

  tallyd / tally serve          run the hub on stdio (put this behind ssh)
  tally deck [--connect ...]    drive a real Stream Deck
  tally term                    live terminal preview
  tally png  [-o FILE]          render one frame (or --watch) to PNG
  tally ls                      print the current signal table
  tally raise ID [...]          claim a key from any shell script
  tally clear ID                release it
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import config as cfgmod
from .devices import PROFILES
from .hub import Hub
from .signal import Signal, STATES, IDLE, rank
from .sources import make as make_source
from .paths import (signals_dir, state_dir, hushed, set_hush, clear_hush,
                    parse_duration, safe_id, write_json_atomic, read_json,
                    private_dir)
from .view import View
from .client import LocalLink, PipeLink, run


def _build_hub(cfg: dict, demo: bool) -> Hub:
    specs = [{"kind": "demo"}] if demo else cfg["sources"]
    sources = []
    for spec in specs:
        spec = dict(spec)
        kind = spec.pop("kind")
        if "argv" in spec:
            spec["argv"] = [str(Path(a).expanduser()) for a in spec["argv"]]
        sources.append(make_source(kind, **spec))
    return Hub(sources)


def _view(cfg: dict, args) -> View:
    dev = getattr(args, "device", None) or cfg["view"]["device"]
    if dev not in PROFILES:
        sys.exit(f"unknown device {dev!r} (have: {', '.join(PROFILES)})")
    return View(profile=PROFILES[dev],
                pinned=list(cfg["view"].get("pinned", [])),
                hide_idle=bool(cfg["view"].get("hide_idle", False)),
                fill=str(cfg["view"].get("fill", "columns")),
                mural=bool(cfg["view"].get("mural", True)))


def _link(cfg: dict, args):
    connect = getattr(args, "connect", None)
    if connect:
        return PipeLink(shlex.split(connect),
                        log=lambda m: print(m, file=sys.stderr))
    if cfg.get("client", {}).get("connect") and not args.demo:
        return PipeLink([str(a) for a in cfg["client"]["connect"]],
                        log=lambda m: print(m, file=sys.stderr))
    return LocalLink(_build_hub(cfg, args.demo))


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_serve(cfg, args):
    _build_hub(cfg, args.demo).serve_stdio()


def cmd_ls(cfg, args):
    link = _link(cfg, args)
    try:
        for s in rank(link.poll()):
            prog = f" {s.progress:>4.0%}" if s.progress is not None else "     "
            print(f"{s.state:<10}{prog}  {s.id:<28} {s.label:<16} {s.sublabel}")
    finally:
        link.close()


_SSH_OPTS_WITH_ARG = {"-o", "-p", "-i", "-l", "-F", "-J", "-b", "-c", "-D",
                      "-e", "-E", "-I", "-L", "-m", "-O", "-Q", "-R", "-S",
                      "-w", "-W", "-B"}


def ssh_host(connect) -> str:
    """The `user@host` (or alias) the deck client itself ssh's to.

    The press script needs to reach the same hub, and it used to guess
    ("claw") — on a machine where that alias resolved to the wrong user the
    guess died with "g@claw: Permission denied (publickey)" while the deck
    was happily connected the whole time. The one address known to work is
    the one in [client] connect; hand it over as TALLY_SSH_HOST (an explicit
    env value still wins)."""
    argv = [str(a) for a in (connect or [])]
    if not argv or os.path.basename(argv[0]) != "ssh":
        return ""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _SSH_OPTS_WITH_ARG:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return ""


def _on_press_cmd(cfg) -> list[str] | None:
    cmd = cfg.get("client", {}).get("on_press")
    if not cmd:
        return None
    host = ssh_host(cfg.get("client", {}).get("connect"))
    if host:
        os.environ.setdefault("TALLY_SSH_HOST", host)   # inherited by the press
    # Expand ~ here: this argv is executed LOCALLY, so an unexpanded tilde is a
    # path that does not exist rather than a shell that will resolve it. The
    # tracked config uses ~ deliberately, since a Mac's /Users/<you> and a
    # server's /home/<you> differ on the two machines reading the same file.
    return [str(Path(a).expanduser()) for a in cmd]


def cmd_term(cfg, args):
    from .render.surfaces import TermSurface
    view = _view(cfg, args)
    run(_link(cfg, args), TermSurface(view.profile), view,
        poll_every=float(cfg.get("client", {}).get("poll_every", 2.0)),
        on_press_cmd=_on_press_cmd(cfg))


def cmd_png(cfg, args):
    from .render.surfaces import PngSurface
    view = _view(cfg, args)
    surface = PngSurface(view.profile, args.out, scale=args.scale)
    run(_link(cfg, args), surface, view, once=not args.watch)
    if not args.watch:
        print(args.out)


def cmd_deck(cfg, args):
    from .render.deckdev import DeckSurface   # imports `streamdeck` lazily
    view = _view(cfg, args)
    surface = DeckSurface(preferred=view.profile.name,
                          brightness=args.brightness)
    view.profile = surface.profile            # trust the hardware's geometry
    run(_link(cfg, args), surface, view,
        poll_every=float(cfg.get("client", {}).get("poll_every", 2.0)),
        on_press_cmd=_on_press_cmd(cfg))


def cmd_raise(cfg, args):
    if not safe_id(args.id):
        sys.exit(f"bad signal id {args.id!r}: letters, digits, . _ - only")
    sdir = private_dir(signals_dir())
    fp = sdir / f"{args.id}.json"
    # A fresh raise of a reused slug must not inherit last round's answer:
    # `tally wait` would return it before the key even rendered. Archive.
    old = state_dir() / "answers" / f"{args.id}.json"
    if old.is_file() and not fp.is_file():
        arch = private_dir(state_dir() / "answers" / ".archive")
        try:
            old.rename(arch / f"{args.id}.{int(time.time())}.json")
        except OSError:
            old.unlink(missing_ok=True)
    d = read_json(fp) if fp.is_file() else None
    d = d or {}
    d["label"] = args.label or d.get("label", args.id)
    d["state"] = args.state or d.get("state", "attention")
    for k in ("sublabel", "detail", "color"):
        v = getattr(args, k)
        if v is not None:
            d[k] = v
    for k in ("progress", "priority", "ttl"):
        v = getattr(args, k)
        if v is not None:
            d[k] = v
    d["updated"] = time.time()
    # A raised ask is answerable hub-side only when it carries a `detail`
    # (that is what the decide popup shows). Most raise sites put the whole
    # question in --sublabel, so promote it rather than leave the key inert.
    if not d.get("detail") and d.get("sublabel") \
            and d["state"] in ("attention", "blocked"):
        d["detail"] = d["sublabel"]
    # A --markdown/--options raise is a question by definition; the hub keys
    # its press action off `detail`, so never leave it empty (a research
    # flag sat unanswerable for exactly this reason).
    if not d.get("detail") and (args.markdown or args.options):
        d["detail"] = d.get("sublabel") or d.get("label") or args.id
    # Raised from inside a Claude Code session (or a tmux pane)? Stamp the
    # identity so a press on this key lands the operator IN that session
    # with the ask on screen, instead of a bare "acked". Explicit flags win
    # over the environment.
    meta = dict(d.get("meta") or {})
    sid = args.session or os.environ.get("CLAUDE_SESSION_ID", "")
    if sid:
        meta["session"] = sid
    if args.project:
        meta["project"] = args.project
    elif "project" not in meta and sid:
        meta["project"] = os.getcwd()
    # The raiser's tmux pane is deliberately NOT stamped unless asked for.
    # A raised flag is a question; a pane target on it made a press attach
    # the operator to the agent's transcript instead of asking the question
    # (G, 2026-09-08: "opened a new window loading the tmux window, not
    # even that session or a question about it"). --tmux opts back in.
    if args.tmux:
        meta["tmux"] = args.tmux
    else:
        meta.pop("tmux", None)
    # Where to DELIVER the answer (never where to route a press): the pane
    # `tally raise` ran in. The session-id scan can only see a session while
    # a tool subprocess is alive, so this is the reliable address.
    # `-t $TMUX_PANE` is not optional: a bare `display -p` resolves the CURRENT
    # pane of the session/attached client, so a raise from a background window
    # (one-shot worker, spawned session, anything the operator is not looking
    # at) stamps the pane he IS looking at — and his answer is pasted into an
    # unrelated agent's transcript while the asker never hears it. Verified on
    # tmux 3.4; cost G a real answer on 2026-09-08 (c64-dxm-source landed in
    # mainA:1.4). No TMUX_PANE → stamp nothing; a wrong address is worse than
    # none, because the answers file still reaches the raiser via `tally wait`.
    if os.environ.get("TMUX") and os.environ.get("TMUX_PANE") and "raiser_pane" not in meta:
        try:
            meta["raiser_pane"] = subprocess.run(
                ["tmux", "display", "-p", "-t", os.environ["TMUX_PANE"], "#S:#I.#P"],
                capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if args.markdown:
        src = sys.stdin.read() if args.markdown == "-" else \
            Path(args.markdown).expanduser().read_text()
        meta["markdown"] = src.strip()
    if args.options:
        meta["options"] = [o.strip() for o in args.options.split("|") if o.strip()]
    if not d.get("detail") and (meta.get("markdown") or meta.get("options")):
        d["detail"] = d.get("sublabel") or d.get("label") or args.id
    # Which harness raised this? Codex keys wear their tally bar down the left
    # edge, so a mixed deck is legible at a glance — but only if the marker
    # gets stamped, and a Codex agent has no CLAUDE_SESSION_ID to give it away.
    # Telegraph stamps its panes; a bare `codex exec` still carries CODEX_HOME.
    harness = (args.harness or os.environ.get("TALLY_HARNESS")
               or os.environ.get("TELEGRAPH_PANE_HARNESS") or "").strip().lower()
    if not harness and not sid and (os.environ.get("CODEX_HOME")
                                    or os.environ.get("CODEX_BIN")):
        harness = "codex"
    if not harness and not sid and (os.environ.get("GROK_AGENT")
                                    or os.environ.get("GROK_SESSION_ID")):
        harness = "grok"
    if harness:
        meta["harness"] = harness
    if harness == "grok" and "account" not in meta and not args.account:
        meta["account"] = "X"
    if args.account:
        meta["account"] = args.account
    elif "account" not in meta and sid:
        marker = os.environ.get("TALLY_ACCT_B_MARKER", ".claude-b")
        cfgdir = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("HOME", "")
        meta["account"] = "B" if marker in cfgdir else "A"
    if meta:
        d["meta"] = meta
    Signal.from_dict({**d, "id": args.id})     # validate before writing
    write_json_atomic(fp, d)
    print(fp)


def cmd_brief(cfg, args):
    """Print the landing brief for a session (hub-side; shown in a popup)."""
    from .brief import build_cached
    roots = []
    codex_roots = []
    grok_roots = []
    for spec in cfg["sources"]:
        if spec.get("kind") == "claude-sessions":
            for r in spec.get("roots", []) or []:
                roots.append(Path(str(r.get("path", ""))).expanduser())
            if spec.get("root"):
                roots.append(Path(str(spec["root"])).expanduser())
        elif spec.get("kind") == "codex-sessions":
            if spec.get("root"):
                codex_roots.append(Path(str(spec["root"])).expanduser())
        elif spec.get("kind") == "grok-sessions":
            grok_roots.append(Path(str(spec.get("root") or Path.home() / ".grok" / "sessions")).expanduser())
    tasks_cmd = cfg.get("brief", {}).get("tasks_cmd") or None
    kwargs = dict(session=args.session or "", project=args.project or "",
                  label=args.label or "", roots=roots or None, state=args.state or "",
                  tasks_cmd=tasks_cmd, ask=args.ask or "",
                  codex_roots=codex_roots or None, grok_roots=grok_roots or None)
    if getattr(args, "interactive", False):
        from .brief import document, render
        from .popup import choose
        # Hook signals carry the authoritative permission prompt, which can be
        # absent from the assistant transcript. Load it whole, never the sublabel.
        sid = args.signal or ""
        if sid.startswith("sig/"):
            from .paths import signals_dir
            name = sid[4:]
            if name and "/" not in name and name not in (".", ".."):
                try:
                    flag = json.loads((signals_dir() / (name + ".json")).read_text())
                    kwargs["ask"] = (flag.get("meta") or {}).get("markdown") or flag.get("detail") or flag.get("sublabel") or kwargs["ask"]
                except (OSError, ValueError):
                    pass
        doc = document(**kwargs)
        key = choose(lambda width: render(doc, width), args.actions or "Enter open · space mute · q dismiss",
                     gone=lambda: bool(args.taken and Path(args.taken).exists()))
        if args.action_file:
            Path(args.action_file).write_text(key)
    else:
        print(build_cached(**kwargs))


def cmd_wait(cfg, args):
    """Block until a raised flag is answered (deck popup, Signal, Mac…).

    The raising agent's side of the round trip: it raised `id`, now it
    waits here instead of depending on the operator's Telegram session to
    relay the outcome. Prints the answer; exit 0. Exit 1 on timeout, 2 if
    the flag vanished without an answer (dismissed / expired)."""
    if not safe_id(args.id):
        sys.exit(f"bad signal id {args.id!r}")
    adir = state_dir() / "answers"
    fp = adir / f"{args.id}.json"
    flag = signals_dir() / f"{args.id}.json"
    deadline = time.time() + args.timeout if args.timeout else None
    while True:
        d = read_json(fp) if fp.is_file() else None
        # A torn or half-written file is "not yet", never "empty answer";
        # an answer older than the flag it sits beside is last round's.
        fresh = bool(d and d.get("answer"))
        if fresh and flag.is_file():
            try:
                fresh = fp.stat().st_mtime >= flag.stat().st_mtime - 1
            except OSError:
                fresh = False
        if fresh:
            if args.json:
                print(json.dumps(d))
            else:
                print(d.get("answer", ""))
            if args.consume:
                fp.unlink(missing_ok=True)
            return
        if not flag.is_file() and not fp.is_file():
            sys.exit(2)
        if deadline and time.time() > deadline:
            sys.exit(1)
        time.sleep(args.every)


def _fmt_until(until: float) -> str:
    if until == float("inf"):
        return "until you say unhush"
    return "until " + time.strftime("%H:%M", time.localtime(until))


def cmd_hush(cfg, args):
    """Do-not-disturb for decision notifications (phone). `tally hush`
    = until unhush; `tally hush 2h` = timed. Prints the state."""
    if args.status:
        u = hushed()
        print(f"hushed {_fmt_until(u)}" if u else "not hushed")
        return
    try:
        secs = parse_duration(args.duration or "")
    except ValueError as e:
        sys.exit(str(e))
    print(f"hushed {_fmt_until(set_hush(secs))}")


def cmd_unhush(cfg, args):
    was = clear_hush()
    n = len([p for p in signals_dir().glob("*.json") if not p.stem.startswith("ask-")])
    print(("unhushed" if was else "was not hushed") +
          (f" — {n} question(s) raised" if n else ""))


def cmd_passthrough(name: str):
    """`tally spawn …` / `tally offer …` exec the contrib helper of that name."""
    def run(cfg, args):
        from .paths import contrib_bin
        bin_ = contrib_bin(name)
        if not bin_:
            sys.exit(f"{name} helper not installed (contrib/{name})")
        os.execv(bin_, [bin_] + list(args.rest))
    return run


def cmd_clear(cfg, args):
    if not safe_id(args.id):
        sys.exit(f"bad signal id {args.id!r}")
    fp = signals_dir() / f"{args.id}.json"
    if fp.is_file():
        fp.unlink()
        print(f"cleared {args.id}")
    else:
        sys.exit(f"no such signal file: {fp}")


# ── parser ───────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tally", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--demo", action="store_true",
                   help="use the built-in demo fleet instead of real sources")
    sub = p.add_subparsers(dest="cmd", required=True)

    # --demo is accepted both before and after the subcommand; argparse only
    # applies a subparser default when the attribute is missing, so the
    # main-parser value survives via default=SUPPRESS on the sub flag.
    sp = sub.add_parser("serve", help="run the hub on stdio (for ssh)")
    sp.add_argument("--demo", action="store_true", default=argparse.SUPPRESS)
    sp = sub.add_parser("ls", help="print the signal table")
    sp.add_argument("--demo", action="store_true", default=argparse.SUPPRESS)

    for name in ("term", "png", "deck"):
        sp = sub.add_parser(name)
        sp.add_argument("--demo", action="store_true",
                        default=argparse.SUPPRESS,
                        help="use the built-in demo fleet")
        sp.add_argument("--connect", metavar="CMD",
                        help="hub command, e.g. 'ssh vps tallyd'")
        sp.add_argument("--device", choices=list(PROFILES),
                        help="device profile (default from config: neo)")
        if name == "png":
            sp.add_argument("-o", "--out", default="tallydeck.png")
            sp.add_argument("--watch", action="store_true",
                            help="keep re-rendering on change")
            sp.add_argument("--scale", type=int, default=2)
        if name == "deck":
            sp.add_argument("--brightness", type=int, default=80)

    sp = sub.add_parser("raise", help="raise/update a signal from the shell")
    sp.add_argument("id")
    sp.add_argument("--label")
    sp.add_argument("--state", choices=list(STATES))
    sp.add_argument("--sublabel")
    sp.add_argument("--detail")
    sp.add_argument("--color")
    sp.add_argument("--progress", type=float)
    sp.add_argument("--priority", type=int)
    sp.add_argument("--ttl", type=float)
    sp.add_argument("--session", help="claude session uuid (default: "
                    "$CLAUDE_SESSION_ID)")
    sp.add_argument("--project", help="project path (default: cwd)")
    sp.add_argument("--tmux", help="tmux pane target (default: this pane)")
    sp.add_argument("--account", help="account badge, e.g. A or B")
    sp.add_argument("--harness", help="which agent harness raised this: "
                    "'codex' draws the tally bar down the left edge, "
                    "'grok' along the bottom "
                    "(default: detected from the environment)")
    sp.add_argument("--markdown", metavar="FILE|-",
                    help="full ask as Markdown (headings, tables, lists…); "
                         "the decide popup renders it. '-' reads stdin")
    sp.add_argument("--options", metavar="A|B|C",
                    help="answer options, pipe-separated; the popup shows "
                         "them as numbered cards")

    sp = sub.add_parser("clear", help="remove a raised signal")
    sp.add_argument("id")

    sp = sub.add_parser("hush", help="pause phone notifications (2h, 45m, or open-ended)")
    sp.add_argument("duration", nargs="?", default="")
    sp.add_argument("--status", action="store_true")
    sub.add_parser("unhush", help="resume phone notifications")

    for name, help_, usage in (
            ("spawn", "start a task as its own tmux session (own deck key)",
             "tally spawn <slug> [-c cwd] [-a A|B|O|X] [--harness claude|codex|grok] [-p file|-] [\"task text\"]"),
            ("offer", "compatibility name for spawn — starts it, asks nothing",
             "tally offer <slug> \"<one-line summary>\" [-c cwd] [-a A|B|O|X] [--harness claude|codex|grok] [-p file|-] [\"task text\"]"),
            ("reap", "report (and on request reap) idle spawned sessions",
             "tally reap [--reap] [--idle-hours N] [--include-unspawned] [--json]"),
            ("close", "close a --worktree lane: commit, merge back, remove the tree",
             "tally close <slug> [--abandon] [--force] [--no-commit]")):
        # add_help=False: --help reaches the helper, which prints its real usage
        sp = sub.add_parser(name, help=help_, usage=usage, add_help=False)
        sp.add_argument("rest", nargs=argparse.REMAINDER)

    sp = sub.add_parser("wait", help="block until a raised flag is answered")
    sp.add_argument("id")
    sp.add_argument("--timeout", type=float, default=0,
                    help="seconds; 0 = forever")
    sp.add_argument("--every", type=float, default=2.0)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--consume", action="store_true",
                    help="delete the answer file after printing it")

    sp = sub.add_parser("brief", help="print a session's landing brief")
    sp.add_argument("--session", help="full Claude or Codex session uuid")
    sp.add_argument("--project", help="project path")
    sp.add_argument("--label")
    sp.add_argument("--state")
    sp.add_argument("--ask", help="the ask text, for signals without a log")
    sp.add_argument("--interactive", action="store_true", help="scrollable popup with session actions")
    sp.add_argument("--signal", default="")
    sp.add_argument("--actions", default="")
    sp.add_argument("--taken", default="")
    sp.add_argument("--action-file", default="")
    return p


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # spawn/offer are thin passthroughs: hand EVERYTHING (including --help)
    # to the helper before argparse can claim the flags for itself.
    if argv and argv[0] in ("spawn", "offer", "reap"):
        from .paths import contrib_bin
        bin_ = contrib_bin(f"tally-{argv[0]}")
        if not bin_:
            sys.exit(f"{argv[0]} helper not installed (contrib/tally-{argv[0]})")
        os.execv(bin_, [bin_] + argv[1:])
    args = _parser().parse_args(argv)
    cfg = cfgmod.load(args.config)
    {
        "serve": cmd_serve, "ls": cmd_ls, "term": cmd_term,
        "png": cmd_png, "deck": cmd_deck,
        "raise": cmd_raise, "clear": cmd_clear, "brief": cmd_brief,
        "wait": cmd_wait, "hush": cmd_hush, "unhush": cmd_unhush,
        "spawn": cmd_passthrough("tally-spawn"), "offer": cmd_passthrough("tally-offer"),
        "reap": cmd_passthrough("tally-reap"), "close": cmd_passthrough("tally-close"),
    }[args.cmd](cfg, args)


def main_serve() -> None:
    """`tallyd` — shorthand for `tally serve`."""
    main(["serve"] + sys.argv[1:])


if __name__ == "__main__":
    main()
