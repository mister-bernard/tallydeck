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

The client never sends commands, argv, or paths — only `{"press", id}`
and `{"answer", id, text}`. All execution happens hub-side, from the
hub's own config and sources; a file dropped into the watch directory
cannot smuggle an `action` in (the source strips it and attaches its own).
An answer is accepted only for a question the hub currently has raised,
never for a live session's own prompt, and a bare digit must index the
question's option list. A compromised or buggy client can, at worst,
answer questions the operator was already being asked.

## Presses are hub-side popups

Client-side routing (read the Mac's window titles, ssh back, fall through
to a fresh terminal window) depended on the deck machine's configuration,
permissions and process version, and every one of those failed in turn.
The hub runs tmux, so it puts the popup up itself: a session key runs
`contrib/tally-popup-route`, a raised question `contrib/tally-popup-decide`,
each opening on every attached client active in the last 12 h (mosh ghosts
are weeks old; `_fl-*` floating sessions are never targets). The same popup
folds on the other terminals once the operator acts in one (a marker file
the popups poll). A press action still alive after 0.7 s means "the popup
is up": the key stops flashing until it closes. The Mac script's only
remaining job is to bring the terminal app forward.

Pane identity is durable: the PostToolUse hook records each session's
tmux pane from inside the pane (`~/.tallydeck/panes/`), because the live
process-tree scan only sees a session while a tool subprocess is alive —
keys kept "losing" their pane between tool calls. For a permission
prompt the pane a press lands in comes from that registry, never from the
dropped file, so a hostile drop cannot steer the operator into a pane of
its choosing.

## Decisions: one answer file, many surfaces

A raised question (`tally raise --options … --markdown …`) can be answered
from the deck popup, from Signal, from a bare option number on Telegram,
or from a native Mac notification. Every surface writes the same
`~/.tallydeck/answers/<id>.json`, created exclusively (`os.link`): the
first writer wins, the second learns it lost, and nothing is ever
overwritten. All JSON state is written atomically (temp + replace) so a
concurrent reader never sees a torn file; `tally wait` treats an unreadable
or empty file as "not yet" and ignores an answer older than its flag.
`tally raise` archives last round's answer for a reused slug. The flag is
cleared LAST, and never outlives its answer.

The answer is delivered to the session that asked — pasted into its pane
(recorded at raise time; the process-tree scan is the fallback) — not to
the operator's chat: he pressed the key, he knows.

## Deck first, phone when away

`contrib/tally-notify` sends a raised question to Signal only while no
deck is connected: the hub touches `hub.alive` every 5 s on its own thread
(a hung poll must not look like "unplugged"), and a live `tally serve`
process counts too. Unplug the deck and any question still raised goes to
the phone; plug it back in and the phone goes quiet. Options are numbered
in the message and the operator replies with the number or quotes the
message to answer in words. The chat router (Telegraph) runs the matcher
(`contrib/tally-answer`) before normal routing, for Signal DMs from the
operator only while something is pending. Its rules are deliberately
narrow, because an unrelated message eaten as an answer is the worst
outcome the system can produce: an implicit answer is an OPTION only
(`1`, `b`), within an hour of the send; words need the question named
(`[id] …`, `id: …`) or a quote of the tagged message; a quote of anything
else belongs to someone else's conversation; several pending → a nudge,
never a swallow. On Telegram the operator is replying to an agent's own
message, so the hook only observes: a bare digit records the answer and
clears the key, the message still routes. `hush` / `hush 2h` / `unhush`
pause the phone; `~/.tallydeck/answer-quiet` kills both directions with
no restart. Permission prompts and turns that ended with a question are
announced once each, notify-only — that answer belongs in the session.

## Dedicated sessions

Work is meant to run in its own tmux session with its own key, decided
about from the deck rather than watched. `tally spawn <slug>` starts a
task that way (claude receives the task as its first prompt; the session
source labels the key by tmux session). `tally offer <slug> "<summary>"`
is the "run this in a dedicated session?" prompt as one command: it
raises a two-option decision, returns at once, and a detached waiter
spawns on yes, pasting the outcome back into the offering pane. The
judgment "will this run for a while?" belongs to the agent in the
operator's main session, guided by the convention in that workspace's
AGENTS.md.

## Reload and reconnect

The hub checks its own source tree every 5 s and `exec`s itself when it
changed (never mid-popup); `exec` keeps fds 0/1 — the ssh pipe — so a
`git pull` on the host reaches the deck with no client action. The deck
client re-spawns its connect command with a 3 s backoff if the hub dies,
keeping the last snapshot on the keys. Together these ended the "quit and
relaunch the deck after every fix" loop.

## The quiet-deck mural

When nothing is blocked, asking, or working, the eight keys become one
canvas: a fedora-and-magnifier portrait, block lettering across the top
row, a blinking prompt bottom-right — characters on a 4×7 px cell grid,
sampled from drawn silhouettes onto a density ramp, indigo to amber.
Pictures may cross the bezels between keys; text must not (a line of
words across a bezel loses letters), so lettering stays within one key
row and caption lines each sit inside one key. Any press peeks at the
plain grid for 20 s.

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

In a Claude Code session JSONL, walk back to the last *conversational*
record, skipping bookkeeping types (`attachment`, `system`, `cost-state`,
`last-prompt`, `ai-title`, `mode`, …) — on current Claude Code most logs
end in one of those, and reading only the literal last record left most of
a live fleet showing IDLE. Then:

- `user` (a prompt or a tool result) → Claude's move → WORKING
- `assistant` with a `tool_use` pending → a tool is running → WORKING
- `assistant` pending `AskUserQuestion`/`ExitPlanMode` → ATTENTION
- `assistant`, turn ended, last message asks a question → ATTENTION
- `assistant`, turn ended, no ask → SUCCESS (green, quiet, `done · 4m`)

"Asks a question" = a `?` in the final paragraph (code spans stripped) or a
short list of decision phrases (*should I*, *your call*, *sign-off*,
*decision needed*…). Deliberately narrow — a closing "let me know if…" is a
courtesy, not an ask, and every false amber trains the operator to ignore
the deck. A `system/turn_duration` record after the assistant record is
Claude Code's own end-of-turn marker: when present the verdict is final and
skips the dwell below. Sessions untouched for 30 min drop off. One key per
pane; paneless sessions collapse per project (most recent wins), except
that a session asking for the human is never collapsed away.

Red is reserved for hooks and scripts: the Notification hook raises
BLOCKED for `permission_prompt` only, ATTENTION for elicitation /
`agent_needs_input`, and nothing for `idle_prompt` ("waiting for your
input" is merely the turn being over — v1 raised BLOCKED for every type,
so every finished session went red a minute after it stopped talking).

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
  tail only counts as ATTENTION/SUCCESS once the log has been quiet for
  `dwell` seconds (default 15), unless Claude Code's own end-of-turn
  marker is present. Hysteresis is mandatory, not polish.
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
