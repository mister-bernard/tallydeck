# tallydeck — design notes

## Why these boundaries

The failure mode of deck software is fusion: state detection tangled with
drawing tangled with the HID library, so nothing is reusable and the deck
becomes a macro pad with one hardcoded app. tallydeck's contract is that
exactly one type crosses each boundary:

- sources → hub: `Signal`
- hub → client: JSON snapshots of signals (NDJSON protocol)
- view → surface: `Layout` (signals placed on a grid) + a `lit` map

A new integration is *either* a source (new state to show) *or* a surface
(new place to show it), never both, and never touches the middle.

## Why full snapshots, not diffs

A fleet is tens of signals; a snapshot is a couple of KB. Full-state makes
the protocol idempotent — a client can connect, crash, reconnect at any
moment and be correct after one line. Diff protocols earn their complexity
at thousands of entities; we are nowhere near.

## Why stdio + SSH instead of a network service

The deck sits on a desk; the agents live on servers. Options considered:

1. HTTP/WebSocket service on the server — new port, new auth, new TLS story,
   and operational state leaks onto a public surface.
2. MQTT/redis pub-sub — a broker dependency for 8 keys.
3. **stdio over SSH** — zero new attack surface, auth is SSH, transport is a
   pipe, works over any bastion/jump config, trivially testable
   (`tallyd | head`).

(3) is also what makes the hub composable: anything that can pipe can host
or consume it. This mirrors Bitfocus Companion's Satellite idea, minus the
custom TCP protocol.

## Why press-by-id only

The client never sends commands, argv, or paths — only `{"press", id}`.
All execution happens hub-side, from the hub's own config and sources. A
compromised or buggy client can, at worst, ack signals and focus tmux panes.

## Flash design

- `attention`: even ~1.25 Hz blink (55% duty). Calm but unmissable.
- `blocked`: double-pulse per 0.8 s period — reads as *alarm* against the
  even blink of attention keys, even in the same color at a distance.
- The lit frame floods the entire key with the state color and inverts the
  text. Border/glow effects tested poorly at 96 px viewed from arm's length;
  flooding is the only treatment that registers in peripheral vision.
- The renderer ticks at 10 fps only while something flashes; otherwise it
  redraws only on state change. LCD keys, no burn-in concern at these rates.

## Ranking

`state weight → priority → recency`, with pinned ids ahead of everything.
State weights: blocked 50 > attention 40 > working 30 > success 20 >
idle 10 > offline 0. Priority is a per-signal tiebreaker so a critical
mission outranks chatter *within* the same state, without letting a noisy
source shout over a blocked one.

## The Claude session heuristic

Ported from cc-fkeys: in a Claude Code session JSONL, if the last record is
a `user` message the model is processing (WORKING); if it's an `assistant`
message the model finished and the human owes a reply (ATTENTION); other
record types are IDLE; sessions untouched for 30 min drop off. One key per
project (most recent session wins) — the operator thinks in projects, not
session ids.

## Surfaces are pixel-identical

The PNG contact sheet is not a mockup: it runs the same `draw_key` as the
hardware. That gives (a) hardware-free development, (b) screenshots that are
ground truth, (c) golden-image regression tests if we ever want them.

## Neo specifics

Profile: 2×4 keys @ 96 px, info bar 248×58, two touch points mapped to page
prev/next. The info bar carries the fleet summary so the 8 keys never waste
space on chrome. Geometry lives in `DeviceProfile`; the hardware surface
asserts the real device matches at open time and adopts its geometry
otherwise, so other decks (Mini/MK.2/XL) work by profile swap.

## Field notes absorbed from prior art

Distilled from a survey of ~8 agent-deck projects, Bitfocus Companion,
and the Home Assistant deck integrations (2026-09):

- **Epoch-aligned flash.** Blink phase derives from wall-clock epoch, not
  per-key timers, so every flashing key everywhere blinks in unison.
  Companion allocates one shared timer per interval for the same reason;
  its in-source rationale: "to keep all intervals aligned amongst each
  other and across restarts."
- **Attention dwell.** Claude Code logs tool results as `user` records, so
  a session's tail flaps assistant/user/assistant… mid-turn. Two prior
  projects independently hit this as icon flicker. Our fix: an assistant
  tail only counts as ATTENTION once the log has been quiet for
  `dwell` seconds (default 15). Hysteresis is mandatory, not polish.
- **Write suppression.** The hardware surface fingerprints each key's
  content and skips unchanged HID writes; flashing 2 of 8 keys costs 2
  updates per frame, not 8. (Companion goes further with content-hash
  render caching; at our scale the fingerprint is enough.)
- **Fail open.** Nothing in tallydeck sits in any agent's execution path.
  If the hub or deck dies, agents proceed exactly as before — the deck is
  a mirror, never a gate. (Agent Pager's hook design makes the same
  promise; it's the right one.)
- **Neo hardware truth** (cross-verified in the Python, Rust and Node
  drivers): keys 96×96 JPEG flipped on both axes; info bar 248×58 via
  `set_screen_image`; the two touch points are programmable RGB LEDs
  (`set_key_color`) that report presses as key indices 8 and 9 — we use
  them as lit page indicators. macOS needs `hidapi` (the library dlopens
  `libhidapi.dylib`) and the Elgato app quit, including its menu-bar
  helper, which holds the device exclusively.
- **tmux gotcha:** a hub launched by systemd/launchd doesn't inherit the
  login shell's `TMUX_TMPDIR`; sessions on a different tmux server are
  invisible to it. Run the hub where your tmux lives, or export the var.

Ideas noted for later, deliberately not in v0:
- **Held-open permission requests** (agentsd): the hook holds the HTTP
  response open and the deck press *is* the answer — the difference
  between an attention router and a status light. Perfect fit for a
  future Claude Code PermissionRequest-hook source.
- **Alert semantics** (hwinfo-streamdeck): hysteresis / dwell / cooldown /
  snooze as four explicit knobs. We ship dwell only.
- **A "Waiting" key** (agent-vitals): one key dark until *any* session
  blocks on you, then names it; press to jump, press again to cycle.
- **Companion Satellite / AgentDeck Surface Protocol v1** as alternate
  transports, if this ever needs to render onto surfaces we don't own.

## Non-goals (v0)

- No plugin for the official Elgato app — we drive HID directly; the Elgato
  app must be quit. Revisit only if coexistence becomes a real need.
- No historical timeline/state store. The deck shows *now*; logs stay in
  the systems that own them.
- No key images/icons per project. Text + color is the design: icons on
  96 px keys cost legibility and require per-project art.
