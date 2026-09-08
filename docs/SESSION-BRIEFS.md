# Session briefs

Session keys and raised decisions share Rich panels, Markdown, and a scrollable
viewport. The popup uses the current terminal width and redraws on resize. Use
Up/Down or j/k, Page Up/Down, and Home/End (g/G). The line counter shows the full
document's extent; the action bar remains visible. Narrow tables become stacked
label/value lists, and code lines wrap. No assistant message or explicit ask is
shortened to a character budget.

A session brief quotes the actual ask first, followed by supplied choices or an
explicit recommendation when present, short verbatim summary excerpts, and the
complete latest message and previous supporting message. It does not generate
choices, infer a recommendation, or turn a status report into a decision.

The classifier suppresses optional closing offers, negated approvals, quoted
questions, and question marks in status tables/code. An explicit approval request
remains actionable even when supporting material follows it. Blocking tools and
permission-hook flags retain their own meaning; deliberately raised flags are not
reclassified by the prose heuristic. Detection is conservative and rule based.

Session actions remain Enter (floating), t (tab), s (split), l (text links), and
space (mute until new output). Session tool questions must be answered in the
waiting agent pane. Raised decisions open the answer UI directly and retain the
exclusive first-answer record and delivery to the originating pane.

Codex briefs, resume commands, and mute records use the full UUID. Rendering is
rebuilt on each press instead of caching an ANSI screen under a UUID prefix;
label, repository status, and terminal size can all change independently of the
log. Complete JSONL records are read even when one exceeds the old tail window.

The popup uses the existing Rich installation used by `tally-decide`; no new
service is involved. The noninteractive core CLI still has a plain-text fallback
on hosts without Rich. Tests of the Rich popup skip there; the deployment host
must have the existing popup stack available.

Regression coverage is in `tests/test_session_briefs.py`: large records, genuine
closing questions, optional status offers, structured permissions, explicit
recommendations, narrow/wide Markdown, UUIDv7 siblings, mute/rearm, route actions,
exclusive answers/delivery, and real pseudo-terminal scroll/resize/input handling.
