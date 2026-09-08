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
# TALLY_SESSION, TALLY_TMUX, TALLY_ACCOUNT, TALLY_SUBLABEL, TALLY_DETAIL,
# TALLY_LONG.
#
# THE MODEL
# ---------
# A press never hijacks your view. It finds the tmux client you are actually
# looking at and drops a floating popup (80% x 70%) over it — the router:
#
#   Enter → switch this view to the pressed session
#   b     → bring the pressed session's window INTO your current session
#           (link-window: it appears in your window list, tiled your way;
#           the source session keeps it — fully reversible)
#   r     → (paneless sessions) resume the Claude session in a new window
#   ␣     → done: retire the alert (the ONLY way a press clears one)
#   other → dismiss; nothing anywhere has changed
#
# A raised signal with no session behind it (a script's `tally raise`) still
# gets the popup — showing its ask — so pressing a flashing key always shows
# you WHAT wanted you before anything is acknowledged.
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

# Where the hub is. The deck client exports TALLY_SSH_HOST from its own
# [client] connect line, so this is whatever address the deck itself is
# using — the one address proven to work. Set it yourself to override; the
# bare alias is only a last resort. Never a hostname or address in here.
HOST="${TALLY_SSH_HOST:-claw}"
SOCKET="${TALLY_TMUX_SOCKET:-/tmp/tmux-1000/cc}"

# Which branch a press took is otherwise invisible from the hub, and diagnosing it
# by asking "what did you see?" costs a round trip per guess. One line per press,
# locally, so a wrong landing is answered by reading a file instead of speculating.
TRACE="${TALLY_TRACE:-$HOME/.tally-press.log}"
trace() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" >> "$TRACE" 2>/dev/null || true; }
trace "PRESS id=${TALLY_ID:-} group=${TALLY_GROUP:-} state=${TALLY_STATE:-} tmux=${TALLY_TMUX:-} detail=${#TALLY_DETAIL} long=${TALLY_LONG:-0}"

# ── the hub owns every popup ─────────────────────────────────────────────────
# Session keys and raised questions alike are handled HUB-SIDE on press: the
# hub runs tmux and puts the router / decide popup up on your attached
# terminal(s) itself. This script's only job left is to bring the terminal
# app forward so the popup is not born behind something. The old client-side
# routing below stays reachable with TALLY_LEGACY_ROUTING=1 (no attached tmux
# client anywhere is the one case it still covers).
if [ "${TALLY_LEGACY_ROUTING:-0}" != "1" ]; then
  trace "  -> hub owns the popup; bringing the terminal forward"
  for app in Ghostty iTerm2 iTerm WezTerm kitty Alacritty Terminal; do
    if [ "$(/usr/bin/osascript -e "application \"$app\" is running" 2>/dev/null)" = "true" ]; then
      /usr/bin/osascript -e "tell application \"$app\" to activate" 2>/dev/null
      break
    fi
  done
  exit 0
fi

# Route session keys, and every raised/hook signal — with a session behind
# it or not (the popup then shows the ask and offers "done"). Anything else
# (demo keys, the burn meter) has nothing to open.
if [ "${TALLY_GROUP:-}" != "cc" ] && [ "${TALLY_GROUP:-}" != "sig" ] \
   && [ -z "${TALLY_TMUX:-}" ] && [ -z "${TALLY_SESSION:-}" ]; then
  exit 0
fi
# What the key was asking, for the popup. Sublabel is the short ask; detail
# the long form; a session key's sublabel is just an age, so leave it out.
ASK=""
if [ "${TALLY_GROUP:-}" = "sig" ]; then
  ASK="${TALLY_DETAIL:-}"
  [ -n "$ASK" ] || ASK="${TALLY_SUBLABEL:-}"
fi

# A raised flag that carries a question is answered HUB-SIDE: the hub runs
# tmux, so on press it puts tally-decide up on the most recently active
# attached client itself (watchdir's default action). Handling it here as
# well produced two answers to one press — the hub's popup, plus a fresh
# Terminal window from the fallback below whose ssh then failed. One owner.
if [ "${TALLY_GROUP:-}" = "sig" ] && [ -n "${TALLY_DETAIL:-}" ]; then
  trace "  -> hub owns this ask (decide popup); nothing to do here"
  exit 0
fi

# Long press already acted hub-side (snooze) — opening the router on top
# of that would be two conflicting answers to one gesture.
[ "${TALLY_LONG:-0}" = "1" ] && exit 0

TARGET="${TALLY_TMUX:-}"
SESS="${TARGET%%:*}"

# Single-quote for the remote shell. Project paths with spaces in them are
# real; unquoted they became `cd: too many arguments`.
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

  if [ -n "$CLIENTS" ]; then
    FRONT=$(front_title || true)
    # If window titles are unreadable (Accessibility permission not granted
    # yet), fall back to a liveness window: a client active in the last 12h
    # is real; mosh ghosts are weeks old. Popup-over-existing always beats
    # opening yet another terminal window.
    NOW=$(date +%s)
    PICK_TTY=""; PICK_SESS=""
    BEST_TTY=""; BEST_SESS=""; BEST_ACT=""
    while IFS='|' read -r tty sess act; do
      [ -n "$tty" ] || continue
      case "$sess" in _fl-*) continue ;; esac   # never popup on a popup
      if [ -n "$TITLES" ]; then
        case "$TITLES" in *"TALLY[$tty]"*) ;; *) continue ;; esac # ghost → skip
      else
        [ $((NOW - act)) -lt 43200 ] || continue                  # stale → skip
      fi
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
      # Title-less fallback can't find the specific window; at least bring
      # the app forward so the popup isn't born behind something.
      focus_by_token "TALLY[$PICK_TTY]" \
        || /usr/bin/osascript -e "tell application \"$APP\" to activate" 2>/dev/null
      ROUTE="~/.local/bin/tally-route $(q "$PICK_TTY") $(q "$PICK_SESS") $(q "$TARGET") $(q "${TALLY_SESSION:-}") $(q "${TALLY_PROJECT:-}") $(q "${TALLY_LABEL:-}") $(q "${TALLY_STATE:-}") $(q "${TALLY_ACCOUNT:-}") $(q "${TALLY_ID:-}") $(q "$ASK")"
      # Foreground: a failed dispatch (dead client, wrong socket) must fall
      # through to the fresh-window path instead of vanishing silently. The
      # ssh stays open while the popup is up; that is fine — we are a
      # fire-and-forget child of the deck client.
      trace "  -> popup on $PICK_TTY (session $PICK_SESS)"
      if ssh -o BatchMode=yes "$HOST" \
           "tmux -S '$SOCKET' display-popup -c $(q "$PICK_TTY") -w 95% -h 90% -E $(q "$ROUTE")" \
           >/dev/null 2>&1; then
        exit 0
      fi
      trace "  -> popup FAILED, falling through to a fresh window"
    fi
  fi
fi

# ── fallback: no real attached window anywhere → open a fresh one ────────────

if [ -n "$TARGET" ]; then
  # attach -t with the FULL session:window.pane sets the current window too
  # (verified on tmux 3.4) — attaching to just the session landed on
  # whatever window that session happened to show.
  REMOTE="tmux -S ${SOCKET} attach -t $(q "$TARGET") \\; select-pane -t $(q "$TARGET")"
elif [ -n "${TALLY_PROJECT:-}" ] || [ -n "${TALLY_SESSION:-}" ]; then
  # Brief + [r]esume choice, then shell — never a silent bare prompt.
  REMOTE="~/.local/bin/tally-land $(q "${TALLY_PROJECT:-}") $(q "${TALLY_SESSION:-}") $(q "${TALLY_LABEL:-}") $(q "${TALLY_STATE:-}") $(q "${TALLY_ACCOUNT:-}")"
elif [ -n "$ASK" ]; then
  # A raised flag with nothing to land in: show the ask, wait for a key.
  REMOTE="TALLY_POPUP=1 ~/.local/bin/tally-brief '' '' $(q "${TALLY_LABEL:-}") $(q "${TALLY_STATE:-}") $(q "$ASK")"
else
  REMOTE="exec bash -l"
fi

trace "  -> fresh window: ${REMOTE%% *}"
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
