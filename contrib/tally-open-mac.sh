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

# Activate whichever terminal app is running (shared by both paths below).
activate_terminal() {
  local app
  for app in Ghostty iTerm2 iTerm WezTerm kitty Alacritty Terminal; do
    if [[ "$(/usr/bin/osascript -e "application \"$app\" is running" 2>/dev/null)" == "true" ]]; then
      /usr/bin/osascript -e "tell application \"$app\" to activate"
      return 0
    fi
  done
  return 1
}

if [[ -n "${TALLY_TMUX:-}" ]]; then
  SESS="${TALLY_TMUX%%:*}"

  # FIRST CHOICE: retarget an EXISTING attachment. If any tmux client is
  # already attached to the socket (the usual case — a terminal window you
  # keep open), flip the most recently active one to the pressed session and
  # just bring the terminal app forward. No new windows, no new ssh logins.
  if ssh -o BatchMode=yes "${HOST}" "
        set -e
        C=\$(tmux -S '${SOCKET}' list-clients -F '#{client_activity} #{client_name}' 2>/dev/null \
            | sort -rn | awk 'NR==1{print \$2}')
        [ -n \"\$C\" ] || exit 1
        tmux -S '${SOCKET}' switch-client -c \"\$C\" -t '${SESS}'
        tmux -S '${SOCKET}' select-window -t '${TALLY_TMUX%.*}'
        tmux -S '${SOCKET}' select-pane -t '${TALLY_TMUX}'
      " 2>/dev/null; then
    activate_terminal
    exit 0
  fi

  # No attached client anywhere: fall through and open a fresh window
  # attached to the session, exact pane selected.
  REMOTE="tmux -S ${SOCKET} attach -t ${SESS} \\; select-pane -t ${TALLY_TMUX}"
elif [[ -n "${TALLY_PROJECT:-}" ]]; then
  # No live pane: drop into the project directory instead of failing silently.
  # No variable at all — bash is guaranteed present on the hub, and a
  # literal cannot be eaten by an intermediate shell.
  REMOTE="cd ${TALLY_PROJECT} && exec bash -l"
else
  REMOTE="exec bash -l"
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
