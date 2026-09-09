# Codex deck coordinator — reboot checkpoint

Recorded 2026-09-09 UTC before operator maintenance.

## State

This conversation coordinated the Stream Deck fixes and directly implemented
harness inheritance. No uncommitted implementation work belongs to this
conversation. Tallydeck was clean at inspection; HEAD was `7fc267dba3a7aafd539d1e59c1cc9ad980b49443`.
The requested work below is complete. Do not restart the delegated jobs.

- `589c983`: dedicated-session launchers inherit the caller's harness and
  account, with explicit overrides. Installed helper copies were refreshed.
  Validation at that iteration: 167 tests passed, including 24 new routing tests.
- `2b8178d` and `4314d59`: shared session titles, exact pane identity and concise
  topic labels. Internal Codex IDs use the full UUID so simultaneously started
  threads cannot overwrite one another in the hub. The coordinator added
  `tests/test_codex_signal_identity.py`; the title session committed it.
- `ac07d98`: modern scrollable session/decision briefs, complete asks and
  context, corrected optional-follow-up attention detection, and Codex brief
  identity/routing fixes. Owner `deck-briefs` recorded 213 passing tests at
  delivery, refreshed installed helpers, pushed, closed its task and notified G.
  See [deck-briefs.md](deck-briefs.md) for its detailed checkpoint.
- Shared workspace instructions recorded concise topic titles and harness
  inheritance in local commit `d209e8a7`; bridge sync verified the generated
  Codex instructions and both Claude Code accounts' instructions. The workspace
  plaintext GitHub push remote is intentionally disabled. Do not bypass it.
- At G's request 19 current pane titles were shortened through the shared tmux
  title option. Rollback snapshots are in the existing Tallydeck state directory
  under `title-renames-*.json`. Later title-system changes supersede the original
  implementation; preserve their current ownership/correction logic.

## User expectations and later changes

Titles should be recognizable topic names, usually 2–4 words, ideally <=20
characters and at most 24, consistent across deck, tmux and CC manager. Full
sentences or chopped sentence openings are inadequate. A decision popup should
show a concise summary and the actual decision first, with full readable context.
An optional conversational follow-up should not create a false attention alarm.

Codex work defaults to Codex, Claude work to Claude, unless explicitly overridden.
The repository has evolved since this conversation's launch change: automatic
launch instead of an offer (`a8445dc`), exact originating-pane targeting,
additional Codex accounts, trust seeding and memory-aware admission are newer
work. Read current code and workspace instructions rather than reinstating the
older offer-first instructions in this transcript.

## Resume

1. Read this note and the current working-tree status. Check recent commits
   before touching files; other sessions share the checkout.
2. The named dedicated sessions were `session-titles` (Claude) and `deck-briefs`
   (Codex). Both had completed their requested feature work before this notice;
   their startup metadata and task briefs remain in the normal Tallydeck state.
3. Check the post-boot service-health sweep and title-sync/deck behavior if needed.
   Do not restart gateways or alter unrelated infrastructure as part of resume.
4. No task remains blocked on an answer from G in this coordinator conversation.
   Do not replay prior approvals, spawn duplicate work, or resend old completion
   messages. Respond to a new request or report a concrete post-boot regression.

Unrelated bridge checkout changes observed: `presets/full-stack.md` and an
untracked Python cache. These belong to other work and were not staged or stashed.
No tests were rerun solely to create this documentation checkpoint; the test
counts above are explicitly the feature owners' delivery-time results.
