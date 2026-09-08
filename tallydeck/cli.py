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
from .paths import signals_dir
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
    sdir = signals_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    fp = sdir / f"{args.id}.json"
    d = {}
    if fp.is_file():
        try:
            d = json.loads(fp.read_text())
        except json.JSONDecodeError:
            d = {}
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
    if args.markdown:
        src = sys.stdin.read() if args.markdown == "-" else \
            Path(args.markdown).expanduser().read_text()
        meta["markdown"] = src.strip()
    if args.options:
        meta["options"] = [o.strip() for o in args.options.split("|") if o.strip()]
    if not d.get("detail") and (meta.get("markdown") or meta.get("options")):
        d["detail"] = d.get("sublabel") or d.get("label") or args.id
    if args.account:
        meta["account"] = args.account
    elif "account" not in meta and sid:
        marker = os.environ.get("TALLY_ACCT_B_MARKER", ".claude-b")
        cfgdir = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("HOME", "")
        meta["account"] = "B" if marker in cfgdir else "A"
    if meta:
        d["meta"] = meta
    Signal.from_dict({**d, "id": args.id})     # validate before writing
    fp.write_text(json.dumps(d, indent=2))
    print(fp)


def cmd_brief(cfg, args):
    """Print the landing brief for a session (hub-side; shown in a popup)."""
    from .brief import build_cached
    roots = []
    for spec in cfg["sources"]:
        if spec.get("kind") == "claude-sessions":
            for r in spec.get("roots", []) or []:
                roots.append(Path(str(r.get("path", ""))).expanduser())
            if spec.get("root"):
                roots.append(Path(str(spec["root"])).expanduser())
    tasks_cmd = cfg.get("brief", {}).get("tasks_cmd") or None
    print(build_cached(args.session or "", args.project or "",
                       args.label or "", roots or None, args.state or "",
                       tasks_cmd, ask=args.ask or ""))


def cmd_clear(cfg, args):
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
    sp.add_argument("--markdown", metavar="FILE|-",
                    help="full ask as Markdown (headings, tables, lists…); "
                         "the decide popup renders it. '-' reads stdin")
    sp.add_argument("--options", metavar="A|B|C",
                    help="answer options, pipe-separated; the popup shows "
                         "them as numbered cards")

    sp = sub.add_parser("clear", help="remove a raised signal")
    sp.add_argument("id")

    sp = sub.add_parser("brief", help="print a session's landing brief")
    sp.add_argument("--session", help="claude session uuid")
    sp.add_argument("--project", help="project path")
    sp.add_argument("--label")
    sp.add_argument("--state")
    sp.add_argument("--ask", help="the ask text, for signals without a log")
    return p


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    cfg = cfgmod.load(args.config)
    {
        "serve": cmd_serve, "ls": cmd_ls, "term": cmd_term,
        "png": cmd_png, "deck": cmd_deck,
        "raise": cmd_raise, "clear": cmd_clear, "brief": cmd_brief,
    }[args.cmd](cfg, args)


def main_serve() -> None:
    """`tallyd` — shorthand for `tally serve`."""
    main(["serve"] + sys.argv[1:])


if __name__ == "__main__":
    main()
