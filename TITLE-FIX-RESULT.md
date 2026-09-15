# Title correction candidate — t1bc3f2

## Outcome

Candidate committed as `07efa25079d135010f5bf6e696c132cf34586e0b` on
`queue/title-fix-59783` in the assigned isolated checkout. **Full acceptance is
blocked**, not green: 86 title/phone tests pass; the full suite has the same
10 launch/ownership failures as the untouched baseline. Parent `tc147d0`
retains review, release and physical-device acceptance. Nothing was deployed.

## Intention and acceptance

Keep the displayed topic and phone reply attached to the correct session,
even when panes move, while preserving genuine human title overrides.
Ordinary local candidate; no new dependencies. Required checks were the title
and phone test modules, the full existing pytest suite, and diff validation.
The pipeline claim for this checkout belongs to the executing task's runner
(`pipeline-8a819f4bbcff4e70820168eb450d3d5b`); it was preserved.

## Before / after

The exact historical screenshot cases already pass in baseline `43e0e82`:
ledger-authored titles are correctable and registry lookups use pane IDs.
The historic bulk-rename ordering hypothesis cannot be proven from this
checkout alone. New behavioral reproductions found remaining holes:

| Trigger | Before | After |
|---|---|---|
| Two rename batches in the same second | Second receipt erased the first; `Storage expansion` lost provenance and could become sticky | Unique, atomically published receipts preserve both batches |
| `%276` moves from `main:1.4` to `main:1.3` between resolution and sync refresh | Its title is written to neighbor `%277` at `main:1.4` | Sync binds identities before refresh; both harness sources submit stable IDs; writes target `%276` |
| Sync receives a stable ID directly | Title silently skipped | Stable ID resolves to the live pane |
| Phone question's pane dies and its old position is reused | Reply falls back to the replacement pane | Known dead IDs fail closed; positional fallback exists only for legacy records without IDs |
| Pane visually swaps after phone route resolution | Positional target can deliver to neighbor | Known IDs remain stable through paste and Enter |

The real tmux tests create private sockets and disposable `cat` panes. They
swap panes and verify the actual title options and received bytes: the intended
pane gets `Current subject` and `2`, the neighbor gets neither. Another test
kills the original pane and verifies that a live replacement receives no route.
Phone transport is mocked; tests do not send messages to anyone.

## Validation

Existing system Python lacks pytest; existing Homebrew Python has pytest but
lacks Rich. The existing Python 3.12 package directory below supplies pytest and
its dependencies, with no install or filesystem changes to that environment:

```sh
PYTHONPATH=/home/openclaw/projects/lofoten-sweeper/.venv/lib/python3.12/site-packages python3 -m pytest -q tests/test_titles.py tests/test_phone_ask.py
PYTHONPATH=/home/openclaw/projects/lofoten-sweeper/.venv/lib/python3.12/site-packages python3 -m pytest -q
git diff --check
```

| Check | Result | Exit |
|---|---|---|
| Untouched baseline full suite, extracted with `git archive` into a temporary directory inside this checkout | 338 passed, 10 failed, 8 subtests passed | 1 |
| Four new deterministic regressions against original implementation | 80 passed, 4 failed | 1 |
| Candidate title/phone modules, including two additional real tmux regressions | 86 passed | 0 |
| Final candidate full suite | 344 passed, 10 failed, 8 subtests passed | 1 |
| Diff whitespace validation | Clean | 0 |

Full-suite failures match the baseline by test name: one offer test in
`test_core.py`, five parametrized real-tmux launch cases, the offer route test,
and three admission tests in `test_launch_routing.py`. Each spawn was refused
by the installed ownership helper while its follow-up exact-path lookup printed
`FREE`. The wrapper suppresses the acquire command's stderr; the specific
registry condition is **not established** by that output. Shared ownership
records and admission controls were not cleared, bypassed or modified.

Logs and SHA-256 bindings are in [docs/title-fix-evidence](docs/title-fix-evidence).
The manifest binds source files, project metadata, external claim helper and
logs. Builder and inline diff reviewer: OpenAI Codex, GPT-6 Astra. No independent
reviewer was launched because this execution unit explicitly forbids delegation;
parent review is still required. These are local test receipts, not hermetic
build or independent review attestations.

## Limitations and resume

- Full-suite green remains mandatory. Resolve the launch/ownership test
  environment through its owner, then rerun both commands above against the
  candidate. Do not weaken admission or reuse the failed receipt as acceptance.
- The dispatcher's configured bare `python3 -m pytest` also needs an existing
  pytest-capable environment; system Python currently cannot import pytest.
- No live Stream Deck/Mac, real phone delivery, production tmux, cron, installed
  scripts, gateway or service changes were exercised. Parent owns device checks
  and promotion. No SSH, credentials, installs or external sends were used.
- Ledger recovery requires an intact receipt; this change cannot reconstruct
  already-overwritten history or distinguish a human deliberately reusing an
  identical historical tool-authored string. Historic receipts are still keyed
  by pane ID, without a tmux-server lifetime identifier.
- Legacy positional phone records cannot prove original session identity.
  Stable-ID source pairs prevent the reproduced sync race; other legacy callers
  must have a current mapping. A concurrent manual retitle during the separate
  tmux write commands is not made transactional by this candidate.
- Codex still derives its topic from the initial thread message. A semantic
  live-thread summarizer is outside this candidate; latest broadcast text is
  intentionally not substituted.

Rollback is to leave the candidate unpromoted; the parent/live checkout remains
at its existing revision. The subsequent evidence commit contains this report
and logs only. Dispatcher owns completion bookkeeping; this report does not
close the claimed task or parent.
