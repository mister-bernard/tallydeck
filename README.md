# tallydeck

**Tally lights for your agent fleet.**

In a broadcast studio, the tally light is the red lamp on the camera that's
live — one glance tells everyone where attention belongs. tallydeck does the
same for a fleet of running LLM agents, build pipelines, and long jobs: it
projects their attention state onto an Elgato Stream Deck. Keys **flash when
something needs you**, **colors track state**, and a **progress bar** creeps
across each key as missions advance. Press a key to service that agent.

![demo deck](docs/preview.png)

## The idea

Three small concepts, kept strictly apart so anything can be projected onto
anything:

```
  sources ──▶ hub ──▶ view ──▶ surface
  (emit       (merge, (rank,   (Stream Deck,
   Signals)    serve)  page)    terminal, PNG)
```

- **Signal** — the unit of attention: `{id, label, state, progress, …}`.
  States: `blocked · attention · working · success · idle · offline`.
- **Source** — anything that emits signals. Ships with: a **Claude Code
  session scanner** (infers who's waiting on whom from session logs), a
  **watch-directory** (any script claims a key by dropping a JSON file), and
  an **exec poller** (any CLI that prints signal JSON becomes a source).
- **Hub** (`tallyd`) — merges sources into one live table and speaks a tiny
  NDJSON protocol over stdio. Put it behind `ssh` and the deck machine needs
  no state, no ports, no credentials beyond SSH itself.
- **View** — arranges ranked signals onto the key grid: urgent first, pinned
  keys fixed, extra signals paginate.
- **Surface** — the renderer. Real deck, live terminal grid, or PNG file.
  One drawing routine serves all three, so what you screenshot is what the
  hardware shows.

## Quick start (no hardware needed)

```bash
pip install -e .
tally --demo term        # live demo fleet in your terminal
tally --demo png -o deck.png
tally --demo ls
```

## With a Stream Deck

```bash
brew install hidapi      # macOS; Linux: libhidapi + udev rules
pip install -e ".[deck]"
tally deck --demo        # quit the Elgato Stream Deck app first
```

Supported: Stream Deck Neo (2×4 keys + info bar), Mini, MK.2, XL.

## The real thing: agents on a server, deck on your desk

On the server, `tallyd` watches your agents (config below). On the machine
with the deck plugged in:

```bash
tally deck --connect "ssh yourserver tallyd"
```

That's the whole remote story — the protocol rides SSH's stdio.

`~/.config/tallydeck/config.toml`:

```toml
[view]
device = "neo"
hide_idle = false
pinned = []                  # signal ids to fix to the first keys

[[sources]]
kind = "claude-sessions"     # ~/.claude/projects session scanner

[[sources]]
kind = "watchdir"            # ~/.tallydeck/signals/*.json

[[sources]]
kind = "exec"                # any CLI that prints signal JSON
group = "svc"
argv = ["~/bin/service-signals.sh"]
every = 30

[client]
connect = ["ssh", "yourserver", "tallyd"]
```

## Raising a signal from anywhere

The watch-directory is the universal escape hatch — one shell line makes any
script, cron job, or agent hook part of the deck:

```bash
tally raise deploy --label "prod deploy" --state blocked --sublabel "gate 3"
tally raise build  --state working --progress 0.4 --ttl 3600
tally clear deploy
```

Or with no tallydeck install at all, from any language:

```bash
echo '{"label":"backup","state":"success","ttl":600}' \
  > ~/.tallydeck/signals/backup.json
```

## Key language

| state | color | motion | meaning |
|---|---|---|---|
| `blocked` | red | double-pulse flash | hard stop — error, failed gate, approval needed |
| `attention` | amber | steady blink | finished or asking — wants you now |
| `working` | blue | steady | busy; progress bar if known |
| `success` | green | steady | landed; auto-expires via `ttl` |
| `idle` | gray | steady | alive, nothing happening |
| `offline` | near-black | steady | stale or unreachable |

Flashing keys alternate with a **flood frame** — the whole key fills with its
state color — which is what makes them readable in peripheral vision.

**Press** a key: the hub asks the signal's source to handle it (the Claude
source focuses that project's tmux pane; the watchdir source acks the
signal), falling back to the signal's own declared `action`. **Long-press**
(≥ 0.5 s): dismiss. The Neo's touch points page left/right when there are
more signals than keys; its info bar shows the fleet summary
(`1 blocked · 2 need you · 3 working`).

Security posture: presses travel *by signal id only*. The hub executes
nothing that arrives over the wire — actions run server-side and only if the
server's own config/sources defined them.

## Writing a source

```python
from tallydeck.sources.base import Source
from tallydeck.signal import Signal, WORKING

class MySource(Source):
    group = "mine"
    def poll(self) -> list[Signal]:
        return [Signal(id="mine/thing", label="thing", state=WORKING,
                       progress=0.7)]
```

Register it in `tallydeck/sources/__init__.py` (or just shell out from an
`exec` source — that's what it's for).

## Development

```bash
pip install -e ".[dev]"
pytest
tally --demo png -o /tmp/deck.png   # visual check without hardware
```

## Prior art & credits

This corner of the design space is busy; tallydeck exists because none of
these had the exact shape we wanted (multi-source attention model, SSH-pipe
transport, zero server ports), but several taught it things — see
[docs/DESIGN.md](docs/DESIGN.md) for what was borrowed from whom.

- [Bitfocus Companion](https://bitfocus.io/companion) — the deck-as-control-
  surface heavyweight; its *feedbacks* (state → key look) vs *actions*
  (press → command) split inspired the source/surface separation here, and
  its epoch-aligned blink timers inspired ours.
- [AgentDeck](https://github.com/puritysb/AgentDeck) — the most complete
  agent-status deck (many ingests, many surfaces, a published Surface
  Protocol). If you want a big ecosystem rather than a small pipe, start
  there.
- [agentsd](https://github.com/paultyng/agentsd),
  [muxplex-deck](https://github.com/bkrabach/muxplex-deck),
  [streamdeck-cmux](https://github.com/gonzaloserrano/streamdeck-cmux),
  [agent-vitals](https://github.com/tapparello/agent-vitals) — adjacent
  takes on agents/tmux → deck, each with an idea worth reading.
- [home-assistant-streamdeck-yaml](https://github.com/basnijholt/home-assistant-streamdeck-yaml)
  — the cleanest state-subscription model on a deck anywhere.
- [python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck)
  — the HID library under the hardware surface.
- Broadcast tally systems, for the name and the philosophy: one lamp, one
  glance, no reading.

Type: [Inter](https://rsms.me/inter/) (SIL OFL 1.1), bundled.

MIT.
