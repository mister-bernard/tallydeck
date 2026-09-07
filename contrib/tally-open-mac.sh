#!/bin/bash
# tally-open-mac.sh — route a deck press to the operator, on the Mac.
#
# Wire it up in the CLIENT's config.toml (the machine with the deck):
#
#   [client]
#   on_press = ["~/bin/tally-open-mac.sh"]
#
# tallydeck runs this on every short press and passes the signal as
# environment: TALLY_ID, TALLY_LABEL, TALLY_GROUP, TALLY_STATE, TALLY_PROJECT,
# TALLY_SESSION, TALLY_TMUX, TALLY_LONG.
#
# THE MODEL (the operator's design)
# ----------------------
# A press never hijacks your view. It finds the tmux client you are actually
# looking at and drops a FULL-SCREEN popup over it — the attention router:
#
#   Enter → switch this view to the pressed session
#   b     → bring the pressed session's window INTO your current session
#           (link-window: it appears in your window list, tiled your way;
#           the source session keeps it — fully reversible)
#   r     → (paneless sessions) resume the Claude session in a new window
#   other → dismiss; nothing anywhere has changed
#
# WHY THE TITLE TOKENS
# --------------------
# tmux cannot tell a real attachment from a mosh ghost — clients whose Mac
# window closed weeks ago still report "attached". The server stamps each
# client's unique token into its terminal title (TALLY[/dev/pts/NN], via
# set-titles; verified to survive the mosh hop). A client is REAL iff its
# token is visible in a window here; the frontmost window's token tells us
# which client you're looking at right now.
#
# First run triggers macOS Automation permission prompts — grant them once.
set -u

HOST="${TALLY_SSH_HOST:-claw}"
SOCKET="${TALLY_TMUX_SOCKET:-/tmp/tmux-1000/cc}"

# Nothing to route for the burn meter or any other non-session key.
[ "${TALLY_GROUP:-}" = "cc" ] || exit 0

TARGET="${TALLY_TMUX:-}"
SESS="${TARGET%%:*}"

# Single-quote for the remote shell. Paths like "Client Docs/Quarterly Design
# Review 2025" are real here; unquoted they became `cd: too many arguments`.
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# ── local terminal app ───────────────────────────────────────────────────────

APP=""
for app in Ghostty iTerm2 iTerm WezTerm kitty Alacritty Terminal; do
  if [ "$(/usr/bin/osascript -e "application \"$app\" is running" 2>/dev/null)" = "true" ]; then
    APP="$app"
    break
  fi
done

window_titles() {  # newline-separated titles of every window of $APP
  [ -n "$APP" ] || return 1
  /usr/bin/osascript -e "tell application \"System Events\" to get name of every window of process \"$APP\"" 2>/dev/null \
    | /usr/bin/sed 's/, /\n/g'
}

front_title() {
  [ -n "$APP" ] || return 1
  /usr/bin/osascript -e "tell application \"System Events\" to get name of front window of process \"$APP\"" 2>/dev/null
}

focus_by_token() {  # $1 = token; bring the window whose title contains it forward
  local tok="$1"
  case "$APP" in
    Ghostty)
      /usr/bin/osascript 2>/dev/null <<EOS
tell application "Ghostty"
  set hits to (every terminal whose name contains "$tok")
  if hits is {} then error "miss"
  focus (item 1 of hits)
  activate
end tell
EOS
      ;;
    iTerm2|iTerm)
      /usr/bin/osascript 2>/dev/null <<EOS
tell application "$APP"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if name of s contains "$tok" then
          select s
          select t
          select w
          activate
          return
        end if
      end repeat
    end repeat
  end repeat
  error "miss"
end tell
EOS
      ;;
    Terminal)
      /usr/bin/osascript 2>/dev/null <<EOS
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if name of t contains "$tok" then
        set selected tab of w to t
        set index of w to 1
        set frontmost of w to true
        activate
        return
      end if
    end repeat
  end repeat
  error "miss"
end tell
EOS
      ;;
    *)  # universal AX fallback: window-level only, needs Accessibility
      /usr/bin/osascript 2>/dev/null <<EOS
tell application "System Events"
  tell process "$APP"
    set ws to (every window whose name contains "$tok")
    if ws is {} then error "miss"
    perform action "AXRaise" of (item 1 of ws)
    set frontmost to true
  end tell
end tell
EOS
      ;;
  esac
}

# ── pick the client you're looking at, drop the router over it ──────────────

if [ -n "$APP" ]; then
  CLIENTS=$(ssh -o BatchMode=yes "$HOST" \
    "tmux -S '$SOCKET' list-clients -F '#{client_tty}|#{client_session}|#{client_activity}'" \
    2>/dev/null || true)
  TITLES=$(window_titles || true)

  if [ -n "$CLIENTS" ] && [ -n "$TITLES" ]; then
    FRONT=$(front_title || true)
    PICK_TTY=""; PICK_SESS=""
    BEST_TTY=""; BEST_SESS=""; BEST_ACT=""
    while IFS='|' read -r tty sess act; do
      [ -n "$tty" ] || continue
      case "$TITLES" in *"TALLY[$tty]"*) ;; *) continue ;; esac   # ghost → skip
      case "$FRONT" in *"TALLY[$tty]"*) PICK_TTY="$tty"; PICK_SESS="$sess" ;; esac
      # otherwise: the most recently active real client is "current enough"
      if [ -z "$BEST_ACT" ] || [ "$act" -gt "$BEST_ACT" ]; then
        BEST_TTY="$tty"; BEST_SESS="$sess"; BEST_ACT="$act"
      fi
    done <<EOF
$CLIENTS
EOF
    if [ -z "$PICK_TTY" ]; then
      PICK_TTY="$BEST_TTY"; PICK_SESS="$BEST_SESS"
    fi
    if [ -n "$PICK_TTY" ]; then
      focus_by_token "TALLY[$PICK_TTY]"
      ROUTE="~/.local/bin/tally-route $(q "$PICK_TTY") $(q "$PICK_SESS") $(q "$TARGET") $(q "${TALLY_SESSION:-}") $(q "${TALLY_PROJECT:-}") $(q "${TALLY_LABEL:-}") $(q "${TALLY_STATE:-}")"
      ssh -o BatchMode=yes "$HOST" \
        "tmux -S '$SOCKET' display-popup -c $(q "$PICK_TTY") -w 100% -h 100% -E $(q "$ROUTE")" \
        >/dev/null 2>&1 &
      exit 0
    fi
  fi
fi

# ── fallback: no real attached window anywhere → open a fresh one ────────────

if [ -n "$TARGET" ]; then
  REMOTE="tmux -S ${SOCKET} attach -t $(q "$SESS") \\; select-pane -t $(q "$TARGET")"
elif [ -n "${TALLY_PROJECT:-}" ]; then
  # Brief + [r]esume choice, then shell — never a silent bare prompt.
  REMOTE="~/.local/bin/tally-land $(q "${TALLY_PROJECT}") $(q "${TALLY_SESSION:-}") $(q "${TALLY_LABEL:-}") $(q "${TALLY_STATE:-}")"
else
  REMOTE="exec bash -l"
fi

CMD="ssh -t ${HOST} \"${REMOTE}\""
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

if [ "$APP" = "iTerm2" ] || [ "$APP" = "iTerm" ]; then
  /usr/bin/osascript <<APPLESCRIPT
tell application "$APP"
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
