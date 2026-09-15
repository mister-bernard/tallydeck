# Title correction candidate — t1bc3f2

## Outcome

Title behavior candidate `07efa25079d135010f5bf6e696c132cf34586e0b` now has
full local acceptance at `f98e489a35c47f299bfb727fa7b3f9c9f610ed5b` on
`queue/title-fix-59783`: **357 passed, 8 subtests passed**, plus **86 passed**
in the required title/phone modules. The continuation changes test fixtures only.
Parent `tc147d0` retains independent review, release and physical-device
acceptance. Nothing was deployed.

## Intention and acceptance

Keep the displayed topic and phone reply attached to the correct session,
even when panes move, while preserving genuine human title overrides.
Ordinary local candidate; no new dependencies. Required checks were the title
and phone test modules, the full existing pytest suite, and diff validation.
The original runner claim was reconciled by the parent before this continuation.
This worker did not change host ownership records or release any host claim.

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
| Initial candidate full suite (historical) | 344 passed, 10 failed, 8 subtests passed | 1 |
| Continuation title/phone modules | 86 passed | 0 |
| Continuation full suite, exact configured acceptance command | 357 passed, 8 subtests passed, 12 deprecation warnings | 0 |
| Diff whitespace validation | Clean | 0 |

### Test-isolation continuation

The ten historical launch failures used the host registry. Parent diagnosis
identified an old broad `/tmp` claim overlapping disposable test directories.
This worker did not remove it or rerun the failing tests against host state.

`tests/conftest.py` now loads the installed `work-claim.py` implementation and
binds its `REG` global to a per-test disposable directory before calling `main`.
A disposable copy of `contrib/tally-spawn` changes only its literal helper path;
the assertion requires exactly one such binding. `TALLYDECK_HOME` points the
copy back to this candidate. Production code, helper and registry remain
unchanged. Missing helper is a test failure, not a skip or success stub.

Launch and offer fixtures use this path, including offer cleanup. Real atomic
acquire/adopt/release behavior is exercised with real private tmux sockets and
fake agents. Added tests verify one winner across competing helper processes,
cross-launcher refusal before creating a session, and ancestor/child overlap
refusal. Existing memory admission assertions remain intact; their force/no-gate
cases were pre-existing test scenarios using disposable processes, not overrides
of this worker's admission. The standalone non-pytest claim script was not run.

Configured full acceptance (exit 0, 26.01 seconds):

```sh
/usr/bin/env PYTHONPATH=/home/openclaw/projects/lofoten-sweeper/.venv/lib/python3.12/site-packages /usr/bin/python3 -m pytest -q
```

The 12 warnings are existing Pillow `getdata` deprecations in core tests.
No failed or skipped tests were reported. The preliminary core/launch run
passed 144 tests before adding the three ownership regressions; full acceptance
includes those regressions. Title/phone acceptance passed in 0.93 seconds.

Historical logs remain in [docs/title-fix-evidence](docs/title-fix-evidence).
The continuation manifest and logs in its `continuation` directory bind the
clean tested source commit/tree, tracked source hashes, external helper hash,
installed package metadata and actual commands/exit statuses. Evidence logs
normalize trailing whitespace only. This is not a hermetic build receipt.
Builder and inline diff reviewer: OpenAI Codex, GPT-6 Astra. No independent
reviewer was launched because this execution unit explicitly forbids delegation;
parent review is still required.

## Limitations and resume

- Local acceptance is green. The configured command supplies the installed
  pytest path explicitly; bare system Python still lacks pytest. The external
  ownership helper remains a declared local dependency; changes require fresh
  acceptance evidence.
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
