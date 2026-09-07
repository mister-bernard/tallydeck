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
import shlex
import sys
import time
from pathlib import Path

from . import config as cfgmod
from .devices import PROFILES
from .hub import Hub
from .signal import Signal, STATES, IDLE, rank
from .sources import make as make_source
from .sources.watchdir import DEFAULT_DIR
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
                fill=str(cfg["view"].get("fill", "columns")))


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


def _on_press_cmd(cfg) -> list[str] | None:
    cmd = cfg.get("client", {}).get("on_press")
    if not cmd:
        return None
    # Expand ~ here: this argv is executed LOCALLY, so an unexpanded tilde is a
    # path that does not exist rather than a shell that will resolve it. The
    # tracked config uses ~ deliberately, since /Users/you and /home/me are
    # different on the two machines that read the same file.
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
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
    fp = DEFAULT_DIR / f"{args.id}.json"
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
    print(build_cached(args.session or "", args.project or "",
                       args.label or "", roots or None, args.state or ""))


def cmd_clear(cfg, args):
    fp = DEFAULT_DIR / f"{args.id}.json"
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

    sp = sub.add_parser("clear", help="remove a raised signal")
    sp.add_argument("id")

    sp = sub.add_parser("brief", help="print a session's landing brief")
    sp.add_argument("--session", help="claude session uuid")
    sp.add_argument("--project", help="project path")
    sp.add_argument("--label")
    sp.add_argument("--state")
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
