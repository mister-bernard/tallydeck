#!/bin/zsh
# tally-open-mac.sh — jump to the pressed session, on the Mac.
#
# Wire it up in the CLIENT's config.toml (the machine with the deck):
#
#   [client]
#   on_press = ["/Users/you/bin/tally-open-mac.sh"]
#
# tallydeck runs this on every short press and passes the signal as
# environment: TALLY_ID, TALLY_LABEL, TALLY_GROUP, TALLY_STATE, TALLY_PROJECT,
# TALLY_SESSION, TALLY_TMUX, TALLY_LONG.
#
# WHY A CLIENT-SIDE SCRIPT AT ALL
# ------------------------------
# The hub also handles presses, and its handler focuses the tmux pane — on the
# VPS. That succeeds and is completely invisible to the person who pressed the
# key, which is why the buttons appeared to do nothing. Anything the user is
# meant to SEE has to run on the machine holding the deck.
#
# The pane target is resolved hub-side and arrives as TALLY_TMUX, because only
# the hub can see tmux. Re-deriving it here would mean an ssh round trip on
# every press.
set -u

HOST="${TALLY_SSH_HOST:-claw}"
SOCKET="${TALLY_TMUX_SOCKET:-/tmp/tmux-1000/cc}"

# Nothing to open for the burn meter or any other non-session key.
[[ "${TALLY_GROUP:-}" == "cc" ]] || exit 0

if [[ -n "${TALLY_TMUX:-}" ]]; then
  # "session:window.pane" — attach to the session, then select the exact pane.
  SESS="${TALLY_TMUX%%:*}"
  REMOTE="tmux -S ${SOCKET} attach -t ${SESS} \\; select-pane -t ${TALLY_TMUX}"
elif [[ -n "${TALLY_PROJECT:-}" ]]; then
  # No live pane: drop into the project directory instead of failing silently.
  REMOTE="cd ${TALLY_PROJECT} && exec \$SHELL -l"
else
  REMOTE="exec \$SHELL -l"
fi

CMD="ssh -t ${HOST} \"${REMOTE}\""

# osascript is the only reliable way to get a NEW Terminal window running a
# command; `open -a Terminal` can only open a file. Escaping matters: the
# command is embedded in an AppleScript string literal, so backslashes and
# double quotes have to survive two levels of quoting.
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

if [[ -d "/Applications/iTerm.app" ]]; then
  /usr/bin/osascript <<APPLESCRIPT
tell application "iTerm"
  activate
  create window with default profile command "$(esc "$CMD")"
end tell
APPLESCRIPT
else
  /usr/bin/osascript <<APPLESCRIPT
tell application "Terminal"
  activate
  do script "$(esc "$CMD")"
end tell
APPLESCRIPT
fi
