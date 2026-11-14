#!/bin/bash
# Replace only the foreground supervisor (what activate-host does): SIGTERM it,
# wait for exit, start a new one from the worktree in core.env, retrying the
# start the way systemd Restart=on-failure would. Workers are not signalled.
set -uo pipefail
ROOT=${REHEARSAL_ROOT:-$HOME/nebula-scratch/deploy-continuity}
PORT=${REHEARSAL_PORT:-18440}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${REHEARSAL_PYTHON:-$HOME/nebula-staging/venv/bin/python}
cd "$ROOT/core"
pid=$(python3 -c "import json;print(json.load(open('data/runtime/connection.json'))['pid'])")
t0=$(date +%s.%N)
kill -TERM $pid
while kill -0 $pid 2>/dev/null; do sleep 0.1; done
t1=$(date +%s.%N)
echo "time-wait sockets on $PORT at restart: $(ss -tan state time-wait '( sport = :$PORT )' | tail -n +2 | wc -l)"
attempts=0
until START_WAIT=8 "$HERE/start.sh"; do attempts=$((attempts+1)); [ $attempts -ge 20 ] && exit 1; sleep 1; done
t2=$(date +%s.%N)
python3 -c "print(f'supervisor exit {float($t1)-float($t0):.1f}s; listener+API ready after {float($t2)-float($t0):.1f}s; failed starts {$attempts}')"
