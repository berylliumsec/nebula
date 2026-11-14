#!/bin/bash
# Start the scratch supervisor by binding the port itself (no listener holder)
# from the source tree named in $ROOT/core/core.env, and wait for API readiness.
set -euo pipefail
ROOT=${REHEARSAL_ROOT:-$HOME/nebula-scratch/deploy-continuity}
PORT=${REHEARSAL_PORT:-18440}
PY=${REHEARSAL_PYTHON:-$HOME/nebula-staging/venv/bin/python}
UI=${REHEARSAL_UI_DIR:-$HOME/nebula/ui/dist}
cd "$ROOT/core"
set -a; . ./core.env; set +a
mkdir -p logs
setsid nohup "$PY" -m nebula.v3.cli serve --host 127.0.0.1 --port "$PORT" --data-dir "$PWD/data" --static-dir "$UI" >> logs/core.out.log 2>&1 < /dev/null &
for i in $(seq 1 "${START_WAIT:-120}"); do
  sleep 1
  if curl -s -m 5 -H "Authorization: Bearer $(cat data/runtime/operator-token 2>/dev/null)" "http://127.0.0.1:$PORT/api/v1/health" 2>/dev/null | grep -q '"api_ready":true'; then echo "ready after ${i}s"; exit 0; fi
done
echo "not ready"; exit 1
