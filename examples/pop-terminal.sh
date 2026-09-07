#!/usr/bin/env bash
# pop-terminal.sh — bring your terminal to the front when a deck key is
# pressed. Wire it up on the deck machine (macOS):
#
#   [client]
#   on_press = ["/Users/you/Code/tallydeck/examples/pop-terminal.sh"]
#
# Combined with the hub-side action (which selects the right tmux pane on
# the server), a key press = window pops forward + pane highlighted.
# The signal arrives as TALLY_* env vars if you want to get fancier
# (e.g. jump to a specific iTerm window by title using TALLY_PROJECT).
for app in Ghostty iTerm2 iTerm WezTerm kitty Alacritty Terminal; do
  if [ "$(osascript -e "application \"$app\" is running" 2>/dev/null)" = "true" ]; then
    osascript -e "tell application \"$app\" to activate"
    exit 0
  fi
done
