#!/bin/bash
# Legacy-style restart (systemctl stop with KillMode=control-group): SIGTERM the
# supervisor and every worker at once, wait for all to exit, start again.
set -uo pipefail
ROOT=${REHEARSAL_ROOT:-$HOME/nebula-scratch/deploy-continuity}
PORT=${REHEARSAL_PORT:-18440}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${REHEARSAL_PYTHON:-$HOME/nebula-staging/venv/bin/python}
cd "$ROOT/core"
A="$ROOT/core/da""ta"
pids=$(pgrep -f "$A" | tr '\n' ' ')
echo "stopping: $pids"
t0=$(date +%s.%N)
kill -TERM $pids
for i in $(seq 1 900); do alive=0; for p in $pids; do kill -0 $p 2>/dev/null && alive=1; done; [ $alive = 0 ] && break; sleep 0.1; done
if [ $alive = 1 ]; then echo "SIGKILL after 90s"; kill -9 $pids 2>/dev/null; fi
t1=$(date +%s.%N)
attempts=0
until START_WAIT=8 "$HERE/start.sh"; do attempts=$((attempts+1)); [ $attempts -ge 20 ] && exit 1; sleep 1; done
t2=$(date +%s.%N)
python3 -c "print(f'all processes exited {float($t1)-float($t0):.1f}s; API ready after {float($t2)-float($t0):.1f}s; failed starts {$attempts}')"
