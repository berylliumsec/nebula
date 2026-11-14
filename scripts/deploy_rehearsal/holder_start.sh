#!/bin/bash
# Start the listener holder (if needed) and a supervisor generation from $1.
set -uo pipefail
ROOT=${REHEARSAL_ROOT:-$HOME/nebula-scratch/deploy-continuity}
PORT=${REHEARSAL_PORT:-18440}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${REHEARSAL_PYTHON:-$HOME/nebula-staging/venv/bin/python}
cd "$ROOT/core"
if ! { [ -f holder.pid ] && kill -0 "$(cat holder.pid)" 2>/dev/null; }; then
  rm -f holder.fifo
  setsid nohup "$PY" -I "$HERE/listener_holder.py" >> holder.out 2>&1 < /dev/null &
  echo $! > holder.pid
  for i in $(seq 1 40); do [ -p holder.fifo ] && break; sleep 0.25; done
fi
echo "start $1" > holder.fifo
for i in $(seq 1 90); do
  sleep 1
  curl -s -m 5 -H "Authorization: Bearer $(cat data/runtime/operator-token 2>/dev/null)" http://127.0.0.1:$PORT/api/v1/health 2>/dev/null | grep -q '"api_ready":true' && { echo "ready after ${i}s"; exit 0; }
done
echo "not ready"; exit 1
