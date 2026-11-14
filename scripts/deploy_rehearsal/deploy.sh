#!/bin/bash
# Full non-disruptive deploy of release $1 on the scratch Core behind the
# listener holder: stage+migrate, host activation (supervisor generation swap),
# API blue/green, target, per-project handoff + source retirement.
set -euo pipefail
ROOT=${REHEARSAL_ROOT:-$HOME/nebula-scratch/deploy-continuity}
PORT=${REHEARSAL_PORT:-18440}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${REHEARSAL_PYTHON:-$HOME/nebula-staging/venv/bin/python}
REL=$1; OUT=${2:-/dev/null}
CORE=$ROOT/core
TOOLS=$HERE
rm -f $CORE/probe.stop
PYTHONPATH= $PY $TOOLS/prober.py $CORE/probe.stop $OUT.probe.json > $CORE/probe.out 2>&1 &
PROBER=$!
PYTHONPATH= $PY $TOOLS/roll.py $REL --phase prepare
t0=$(date +%s.%N)
echo "swap $REL/python" > $CORE/holder.fifo
# The old generation answers until it drains; wait for the new generation's identity.
want=$(python3 -c "import json;print(json.load(open('$REL/nebula-runtime.json'))['version'])")
until curl -s -m 5 -H "Authorization: Bearer $(cat $CORE/data/runtime/operator-token)" http://127.0.0.1:$PORT/api/v1/health | grep -q "\"recovery_version\":\"$want\""; do sleep 0.2; done
t1=$(date +%s.%N)
python3 -c "print(f'host activation: new supervisor generation serving after {float($t1)-float($t0):.1f}s')"
ROLL_OUT=$OUT.roll.json PYTHONPATH= $PY $TOOLS/roll.py $REL --phase roll
touch $CORE/probe.stop; wait $PROBER; cat $CORE/probe.out
