#!/bin/bash
# Set the scratch Core's host pressure thresholds: pressure.sh <cpu> <load>
ROOT=${REHEARSAL_ROOT:-$HOME/nebula-scratch/deploy-continuity}
PORT=${REHEARSAL_PORT:-18440}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${REHEARSAL_PYTHON:-$HOME/nebula-staging/venv/bin/python}
cd "$ROOT/core"
T=$(cat data/runtime/operator-token)
H=$(curl -s -H "Authorization: Bearer $T" http://127.0.0.1:$PORT/api/v1/host-pressure | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['host_id'], d['revision'])")
set -- $H "$1" "$2"
curl -s -X PUT -H "Authorization: Bearer $T" -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" http://127.0.0.1:$PORT/api/v1/host-pressure -d "{\"host_id\":\"$1\",\"expected_revision\":$2,\"thresholds\":{\"cpu\":$3,\"memory\":0.8,\"load\":$4,\"io\":10.0}}" | python3 -c "import json,sys; d=json.load(sys.stdin); print('waiting', d.get('waiting'), d.get('effective_thresholds'))"
