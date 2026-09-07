#!/usr/bin/env bash
# Example exec-source: systemd units → signals. Wire it up with:
#   [[sources]]
#   kind = "exec"
#   group = "svc"
#   argv = ["/path/to/service-signals.sh"]
#   every = 30
set -euo pipefail
failed=$(systemctl --user --failed --no-legend --plain 2>/dev/null | awk '{print $1}')
if [ -z "$failed" ]; then
  printf '{"id":"svc/units","label":"services","sublabel":"all green","state":"success","ttl":120}\n'
else
  n=$(echo "$failed" | wc -l)
  first=$(echo "$failed" | head -1)
  printf '{"id":"svc/units","label":"services","sublabel":"%s failed: %s","state":"blocked"}\n' "$n" "$first"
fi
