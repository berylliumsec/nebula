#!/bin/bash
# Swap the holder's supervisor generation $SWAPS times while
# host_services_probe.py calls host services as a retained worker, and check
# that the desktop browser-bridge registration survives every swap.
# Usage: host_swap.sh <PYTHONPATH of the generation to start> <label> [probe args]
set -uo pipefail
ROOT=${REHEARSAL_ROOT:-$HOME/nebula-scratch/deploy-continuity}
PORT=${REHEARSAL_PORT:-18440}
SWAPS=${SWAPS:-3}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${REHEARSAL_PYTHON:-$HOME/nebula-staging/venv/bin/python}
SOURCE=$1 LABEL=$2
shift 2
cd "$ROOT/core"
set -a; . ./core.env; set +a
mkdir -p "$ROOT/evidence"
OUT="$ROOT/evidence/$LABEL"
TOKEN=$(cat data/runtime/operator-token)
API="http://127.0.0.1:$PORT/api/v1"
bridge() { # bridge [registration_id] -> "<status> <registration_id>"
  local body='{"endpoint":"http://127.0.0.1:17999","token":"rehearsal-bridge-token-000000000000","device_id":"rehearsal-desktop"'
  [ -n "${1:-}" ] && body="$body,\"registration_id\":\"$1\""
  curl -s -m 10 -o "$OUT.bridge.json" -w '%{http_code}' -X POST "$API/desktop/browser-bridge" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "$body}"
  echo " $(python3 -c "import json;print(json.load(open('$OUT.bridge.json')).get('registration_id',''))" 2>/dev/null)"
}
ready() {
  for i in $(seq 1 240); do
    curl -s -m 5 -H "Authorization: Bearer $TOKEN" "$API/health" 2>/dev/null | grep -q '"api_ready":true' && return 0
    sleep 0.5
  done
  return 1
}
read -r status registration < <(bridge)
echo "bridge registered: $status $registration" | tee "$OUT.log"
rm -f "$OUT.stop"
"$PY" "$HERE/host_services_probe.py" --out "$OUT.probe.json" --stop-file "$OUT.stop" "$@" > "$OUT.probe.out" 2>&1 &
probe=$!
sleep 8
for n in $(seq 1 "$SWAPS"); do
  t0=$(date +%s.%N)
  echo "swap $SOURCE" > holder.fifo
  sleep 2
  ready || { echo "swap $n: API not ready" | tee -a "$OUT.log"; break; }
  t1=$(date +%s.%N)
  read -r status refreshed < <(bridge "$registration")
  echo "swap $n: API ready after $(python3 -c "print(round($t1-$t0,1))")s; bridge refresh $status $refreshed" | tee -a "$OUT.log"
  sleep 6
done
touch "$OUT.stop"
wait $probe
cat "$OUT.probe.out" | tee -a "$OUT.log"
