#!/usr/bin/env bash
# Restart only hapi-hub (needed after hub patches) once no HAPI session is mid-turn
# or waiting for approval. Workers keep running and reconnect; the runner is untouched.
# Usage: deploy/restart-hub-when-idle.sh [max_wait_minutes, default 120]
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
deadline=$(( $(date +%s) + ${1:-120} * 60 ))
idle_checks=0
while (( $(date +%s) < deadline )); do
    busy=$(python3 "$ROOT/managerctl.py" '{"op":"sessions"}' | python3 -c '
import json, sys
sessions = json.load(sys.stdin)["sessions"]
print(sum(1 for s in sessions if s.get("active") and (s.get("thinking") or s.get("pendingRequestsCount"))))')
    # Require two consecutive idle observations to skip brief gaps between turns.
    if [[ "$busy" == 0 ]]; then idle_checks=$((idle_checks + 1)); else idle_checks=0; fi
    if (( idle_checks >= 2 )); then
        systemctl --user restart hapi-hub
        echo "hapi-hub restarted at $(date -Is)"
        exit 0
    fi
    sleep 30
done
echo "Sessions stayed busy; hapi-hub was not restarted." >&2
exit 1
