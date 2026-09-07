#!/bin/bash
# tally-open-mac.sh — jump to the pressed session, on the Mac.
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
# HOW THIS WORKS (and why it is shaped this way)
# ----------------------------------------------
# tmux on the server cannot tell a real attachment from a mosh ghost — clients
# whose Mac window closed weeks ago still report "attached", and switching one
# succeeds invisibly. Only this machine knows which windows actually exist. So:
#
#   1. Ask the hub (read-only) for every client: tty, session, activity.
#   2. Enumerate local terminal windows. The server stamps each client's
#      unique token into its title: TALLY[/dev/pts/NN] (tmux set-titles —
#      see the hub's tmux.conf). A client is REAL iff its token is visible
#      in some window here. Ghosts vanish at this step.
#   3. A real window already on the target session → just focus it.
#   4. Else retarget the least-recently-active real client (never the
#      busiest window) with switch-client, then focus its window.
#   5. No real windows at all → open a fresh one (the original behavior).
#
# First run will trigger macOS Automation permission prompts (and, for the
# System Events fallback, Accessibility) — grant them once, interactively.
set -u

HOST="${TALLY_SSH_HOST:-claw}"
SOCKET="${TALLY_TMUX_SOCKET:-/tmp/tmux-1000/cc}"

# Nothing to open for the burn meter or any other non-session key.
[ "${TALLY_GROUP:-}" = "cc" ] || exit 0

TARGET="${TALLY_TMUX:-}"
SESS="${TARGET%%:*}"

# Single-quote for the remote shell. Paths like "Client Docs/Quarterly Design
# Review 2025" are real here; unquoted they became `cd: too many arguments`.
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# The landing brief: where the session left off, repo state, matching open
# tasks. Shown as a tmux popup over the session you land in, so a press
# arrives with context instead of a bare prompt. Any key dismisses it.
BRIEF_CMD="~/.local/bin/tally-brief $(q "${TALLY_SESSION:-}") $(q "${TALLY_PROJECT:-}") $(q "${TALLY_LABEL:-}") $(q "${TALLY_STATE:-}")"
popup() {  # $1 = tmux target session
  ssh -o BatchMode=yes "$HOST" \
    "tmux -S '$SOCKET' display-popup -t $(q "$1") -w 80% -h 70% -E $(q "$BRIEF_CMD")" \
    >/dev/null 2>&1 &
}

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

# ── hub truth + intersection ─────────────────────────────────────────────────

if [ -n "$TARGET" ] && [ -n "$APP" ]; then
  CLIENTS=$(ssh -o BatchMode=yes "$HOST" \
    "tmux -S '$SOCKET' list-clients -F '#{client_tty}|#{client_session}|#{client_activity}'" \
    2>/dev/null || true)
  TITLES=$(window_titles || true)

  if [ -n "$CLIENTS" ] && [ -n "$TITLES" ]; then
    BEST_TTY=""; BEST_ACT=""
    while IFS='|' read -r tty sess act; do
      [ -n "$tty" ] || continue
      case "$TITLES" in *"TALLY[$tty]"*) ;; *) continue ;; esac   # ghost → skip
      if [ "$sess" = "$SESS" ]; then
        # A real window is already on the target session: focus it, and only
        # nudge window/pane (this moves every viewer of that session — tmux
        # windows are session-scoped; that is the data model, not a bug).
        ssh -o BatchMode=yes "$HOST" \
          "tmux -S '$SOCKET' switch-client -c '$tty' -t '$TARGET'" 2>/dev/null
        if focus_by_token "TALLY[$tty]"; then popup "$SESS"; exit 0; fi
      fi
      if [ -z "$BEST_ACT" ] || [ "$act" -lt "$BEST_ACT" ]; then
        BEST_TTY="$tty"; BEST_ACT="$act"
      fi
    done <<EOF
$CLIENTS
EOF
    if [ -n "$BEST_TTY" ]; then
      # Least-recently-active real window: the one whose current view you
      # will miss least. switch-client takes the full pane target directly.
      if ssh -o BatchMode=yes "$HOST" \
           "tmux -S '$SOCKET' switch-client -c '$BEST_TTY' -t '$TARGET'"; then
        if focus_by_token "TALLY[$BEST_TTY]"; then popup "$SESS"; exit 0; fi
      fi
    fi
  fi
fi

# ── fallback: fresh window ───────────────────────────────────────────────────

if [ -n "$TARGET" ]; then
  # Attach to the live pane; once the attachment settles, the same overlay
  # brief appears over it (and any key dismisses it) — one look everywhere.
  REMOTE="tmux -S ${SOCKET} attach -t $(q "$SESS") \\; select-pane -t $(q "$TARGET")"
  ( sleep 2; popup "$SESS" ) &
elif [ -n "${TALLY_PROJECT:-}" ]; then
  # No live pane: drop into the project directory instead of failing silently.
  # Literal bash — the hub has no zsh, and a literal cannot be eaten by an
  # intermediate shell the way \$SHELL was.
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
