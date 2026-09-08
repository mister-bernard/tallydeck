# tallydeck

**Tally lights for your agent fleet.** A Stream Deck becomes the attention
surface for every AI coding session you run: keys flash when something
genuinely needs you, stay calm while agents work, and one press drops you
into the exact session that's asking — briefed, focused, ready to type.

![demo deck](docs/preview.png)

---

## Working through the deck

The deck is meant to replace watching panes scroll. Work runs in its own
tmux session with its own key; you decide when a key asks, from the deck,
your phone, or a bare option number in chat.

- **`tally spawn <slug> -c <dir> "<task>"`** starts the task as its own tmux
  session (`<slug>`), with the calling harness fed the task as its first prompt. It appears
  as its own key as soon as it speaks; a second `<slug>` becomes `<slug>-2`.
- **`tally offer <slug> "<one-line summary>" -c <dir> -p - <<'EOF' … EOF`** is
  the "run this in a dedicated session?" prompt as one command: it raises a
  two-option decision (deck popup; phone when the deck is unplugged; a bare
  `1`/`2` on Telegram or Signal answers it), returns at once, and a detached
  waiter spawns on *yes* — the outcome is pasted back into the offering pane
  as a `[tally] …` line. The convention for agents lives in AGENTS.md.
- **Harness inheritance:** Codex callers launch Codex (account O); Claude
  callers launch Claude and preserve account A/B. Ordinary shell calls retain
  Claude A as the fallback. Both commands accept `--harness codex|claude` or
  `-a A|B|O` to override this; conflicting overrides are rejected. Offers show
  the selected harness/account and save that choice before waiting for an
  answer. A missing executable fails explicitly instead of switching harnesses.
- **Phone fallback** covers session prompts too: with no deck connected, a
  permission prompt or a turn that ended with a question is announced once
  on Signal (notify-only — the answer belongs in that session). Raised
  questions stay fully answerable from the phone.
- **`hush` / `hush 2h` / `unhush`** (as words on Signal or Telegram, or
  `tally hush`) pause phone notifications; questions wait on the deck.

## Reading the deck

### The lights: orange vs red

| Light | State | Meaning | What to do |
|---|---|---|---|
| 🟠 **Orange, flashing** | `attention` | The session **asked you something** — a question in its last message, a plan waiting for approval, an `AskUserQuestion` — and is waiting on the answer. Flashes for 5 minutes, then holds steady orange (still your move, done shouting). | Press it |
| 🔴 **Red, double-pulse** | `blocked` | A hard stop: a permission prompt, a failed gate, a flag a script raised as blocked. Cannot proceed without you. Raised the instant it happens. | Press it now |
| 🔵 **Blue** | `working` | Agent busy — running tools, thinking, writing. Needs nothing. | Enjoy |
| 🟢 **Green** | `success` | **Finished its turn and asked for nothing.** Reads `done · 4m`. Stays until the session goes stale (30 min) or you mute it. | Nothing |
| ⚫ **Gray / dark** | `idle` / `offline` | Alive but quiet, or gone stale. | Nothing |

Hot attention keys show **the actual question**, wrapped across the key —
long asks alternate between two pages of text (watch the little dots), so
you often know your answer before you press.

**When nothing is live** — nobody blocked, nobody asking, nothing working —
the eight keys stop being eight dark tiles and become one picture: Mr. B in
ASCII, fedora and magnifier on the left, the name across the top, a
blinking `> all quiet_` bottom-right. Any press peeks at the plain grid
(your done and idle sessions) for twenty seconds. Turn it off with
`[view] mural = false`.

Finished is not the same as waiting. A session that ends its turn with a
report goes **green**; only one that ends with a question goes **orange**.
Red is never earned by finishing — a normally completed turn is not an
emergency, and a deck that says otherwise trains you to ignore it.

**Pressing never acknowledges.** A short press on any flashing key shows
you what it wants (the router popup, with the ask at the top) and leaves
the alarm up. Only the popup's `␣ done`, `tally clear`, or a long press
retires it — an alarm must never disappear on an action whose result you
did not see.

### The line on each tile (the tally bar)

That strip is the session's **state in miniature** — same color code as
above. **Where** it sits tells you which agent the key belongs to: across
the **top** for Claude Code, down the **left edge** for Codex. Position
carries the harness so colour can keep meaning state.

- **Orange bar** → this session is waiting on you (the whole key will also
  be flashing if it's fresh, steady orange if you've let it sit).
- **Gray bar** → idle session: alive, nothing happening, listed so you know
  it exists.
- **Blue bar** → working.
- **Flashing** always means *fresh* — the state just became yours to act on.

### Everything else on a key

- **Background warmth** — the marbled background's heat (near-black → indigo
  → violet → ember) is that session's **token burn relative to the hottest
  session visible**. Ember = the current #1 spender. Cold black = coasting.
- **`2m · 45k/m`** — quiet-time and burn rate (log-bytes/minute).
- **`A` / `B` / `O` badge** — which account's quota the session is draining
  (`O` is Codex).
- **Placement** — urgent first, then the busiest, flowing top-left ↓ then
  next column. Keys are **sticky**: a session keeps its key while visible,
  so nothing moves between your glance and your press.

### The bottom strip (Neo info bar)

Two bars: your Claude accounts share the top one (one lane each), Codex
gets the bottom one. Fill = how much of the window is spent (matches the
engraved %), the **notch** = your burn target, molten past the notch. The
countdown at the right is when the window resets — underlined marks the
account **burning right now**. The Codex bar reads the plan's real window,
so it says `weekly` under the number and counts down in days; if its
snapshot stops refreshing it says `stale` instead of a confident figure.

### The two touch points

- **Left = urgency beacon.** Pulses red/amber when anything (any page)
  needs you; faint blue when all is calm work. **Press it to service the
  most urgent key** without hunting.
- **Right = burn gauge** (cool → hot → molten), or the **page cycler** when
  there are more sessions than keys.

## Pressing a key

Your terminal comes forward and a floating popup appears **over** your tmux
view (nothing underneath moves), showing the brief: where the session left
off, repo state, related queue tasks. Then one key routes it:

| Key | Action |
|---|---|
| **⏎ Enter** | Open the session **floating in the popup** (resumes it first if it isn't running). Work in it; `Ctrl-b d` puts it back. |
| **t** | Open it as a **tab** (window) of your current tmux session |
| **s** | **Split** it into your current window, side by side |
| **␣ space** | **Done** — mute the alert like archiving mail; it revives only if the session asks anew |
| anything else | Dismiss — nothing anywhere changed |
| **long-press the deck key** | Snooze that alarm 15 min (long-press again to wake) |

An alarm **never clears from a press you couldn't see** — it clears when
the session actually receives your answer, or when you explicitly dismiss.

## Raising your own flags

Any script or agent can claim a key — one line, no dependencies:

```bash
tally raise deploy --state blocked --label "prod deploy" --sublabel "gate 3 needs sign-off"
tally clear deploy
```

Agents in this fleet are instructed to raise decision-grade flags
themselves, with the question on the key — and to keep FYIs off the deck.

## Install

**Server (where your agents run):** clone, then expose the hub:

```bash
printf '#!/usr/bin/env bash\nexport PYTHONPATH="$HOME/projects/tallydeck"\nexec /usr/bin/python3 -m tallydeck.cli "${1:-serve}" "${@:2}"\n' > ~/.local/bin/tallyd && chmod +x ~/.local/bin/tallyd
```

**Mac (where the deck is plugged in):** Python 3.11+ required —
`brew install python@3.13 hidapi`, then:

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[deck]"
ln -sf "$PWD/contrib/tally-open-mac.sh" ~/bin/tally-open-mac.sh
tally deck --demo          # first light (quit the Elgato app first)
tally deck                 # the real fleet, via [client] connect in config
```

### Config, in two layers

`config/tallydeck.toml` is tracked, so shared settings arrive with a `git
pull`. Anything machine-specific — real paths, extra account roots, a task
hook, your timezone — goes in `~/.config/tallydeck/config.toml`, which is
not in the repo and wins over the tracked layer. Keep hosts and secrets out
of the tracked file: see `examples/config.example.toml` for the shape.

The hub is reached by an **ssh alias**, never an address, so nothing
host-specific lives in the repo. Name it whatever you like in the Mac's
`~/.ssh/config` (the default the scripts expect is `claw`), then point
`[client] connect` at that alias. `ControlMaster` makes presses instant:

```
Host claw
  HostName your-server.example.com
  User you
  ControlMaster auto
  ControlPath ~/.ssh/cm-%r@%h:%p
  ControlPersist 10m
```

Grant the macOS **Automation** prompt on first press: the Mac script's only
job is to bring your terminal app forward. The popup itself is put up by
the hub, on whichever tmux client(s) you have attached — nothing on the Mac
routes, reads window titles, or opens fresh windows any more. (The old
client-side routing survives behind `TALLY_LEGACY_ROUTING=1` for the one
case with no tmux client attached anywhere.)

### What a press does

Every key press travels to the hub as an id, and the hub answers it with a
popup on your attached terminal(s):

| Key | Popup | Keys inside it |
|:--|:--|:--|
| a session (blue/green/orange) | the **router**: where it left off, repo state, matching tasks | ⏎ open floating · `t` tab here · `s` split here · `l` text me links · ␣ done (mute) |
| a permission prompt (red) | the router, aimed at the waiting pane | ⏎ lands you at the prompt |
| a raised question (orange/red) | the **decide** TUI: the full Markdown brief and numbered option cards | `1`–`9` pick · `a` answer in words · ⏎ leave it raised · Esc dismiss |

The same popup appears on every attached terminal and folds on the others
the moment you act in one. A pressed key throws fireworks and stops
blinking; it stays steady while its popup is open. Answers to a raised
question go to the session that asked (pasted into its pane) and to
`~/.tallydeck/answers/<id>.json`, which `tally wait <id>` reads — never to
your chat.

### Phone

`contrib/tally-notify` (a user service) sends a raised question to Signal
**only while no deck is connected** (the hub leaves a heartbeat while one
is), with its options numbered; reply with the number, or quote the message
to answer in words. A chat-router hook (see `docs/DESIGN.md`) records the
answer and clears the key; a bare option number on Telegram does the same
without hijacking the message. Permission prompts and turns that ended with
a question are announced once, notify-only. `hush` / `hush 2h` / `unhush`
pause it; `~/.tallydeck/answer-quiet` is the kill switch for both directions.

### Operations

The hub re-executes itself when its source tree changes (a `git pull` on
the host reaches the deck without touching the client) and the deck client
reconnects if the hub dies, keeping the last frame on the keys meanwhile.

## How it works (the short version)

`Signal` (one unit of attention) → **sources** (Claude and Codex session
scanners, watch-directory, token-burn API, Claude Code hooks) → **hub** (merges,
streams NDJSON over stdio — canonically behind `ssh`; runs press actions
and validates answers) → **view** (rank, sticky slots, pages, the mural)
→ **surfaces** (Stream Deck, terminal, PNG). Presses travel by signal-id
only; an answer is accepted only for a question the hub itself raised;
nothing arriving over the wire is executed. Details:
[docs/DESIGN.md](docs/DESIGN.md).

## Development

```bash
pip install -e ".[dev]" && pytest
tally --demo png -o /tmp/deck.png    # pixel-true preview, no hardware
```

## Prior art & credits

Bitfocus Companion (feedback/action split, epoch-aligned blink),
[AgentDeck](https://github.com/puritysb/AgentDeck), agentsd, muxplex-deck,
streamdeck-cmux, home-assistant-streamdeck-yaml, and
[python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck)
under the hardware surface. Marbling math: Lu et al., *Mathematical
Marbling*, IEEE CG&A 2012. Type: [Inter](https://rsms.me/inter/) (OFL).
Named for broadcast tally lights: one lamp, one glance, no reading.

[MIT](LICENSE).
