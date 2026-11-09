# Automatic admission after a verified shutdown

A quiesced project retains its fencing counter while releasing its execution
owner. Automatic admission now selects ownerless projects regardless of their
previous counter. Claiming the next owner still increments the existing counter;
it never resets history. Expired or uncertain owners remain non-null and are not
automatically reclaimed by this selector.

The change removes only the zero-fence condition. Existing shutdown admission,
archived-project, unknown/unassigned-process, memory and exact-claim guards remain
authoritative. A partial shutdown abort keeps its unknown, unassigned old process
barrier. Explicit retirement still requires positive absence of that exact
process.

| Journey | Observable invariant | Evidence |
| --- | --- | --- |
| Reopen a used project | Automatic claim uses the next fence and rejects the old lease | SQLite/PostgreSQL SQL contracts and actual Core |
| Keep uncertain work held | Non-null expired/uncertain owners do not move automatically | Durable command and ownership cases |
| Abort part of a shutdown | A remaining process prevents replacement until positive retirement | Actual inert process with refusal while alive |
| Preserve pauses | Prior paused goals and disabled schedules stay unchanged | Actual completed-chat Core stop/reopen |

Twelve cases are selected in `tests/v3/test_shutdown_readmission.py`: ten SQL
contract cases and two real CLI Core journeys across SQLite and PostgreSQL.
The Core fixture starts authenticated `serve --allow-remote` on a non-loopback
LAN address with isolated data, checksummed retained candidate source, the existing
inert-provider acceptance adapter and synthetic verified recovery HTML. It
completes a chat, shuts down through the public deployment operation, verifies
all prior workers absent, reopens the same data, waits for automatic ownership,
completes another chat, and stops its owned Core again.

The local diff-bound test selection is recorded outside the repository in
`/tmp/nebula-shutdown-readmission-selection.json`, as requested by the parent
integration owner; the shared tracked selection is untouched.

All twelve cases passed on the first run in 97.21 seconds. Actual authenticated
Core origins were `http://172.26.91.238:44711` (SQLite) and
`http://172.26.91.238:36747` (PostgreSQL). Each completed two turns across the
shutdown/reopen boundary, read back fence 1 then 2, rejected the old lease,
preserved the full prior paused goal and disabled schedule, and verified the
previous workers absent before restart. Both reopened Cores then shut down
through the ordinary management operation.

Ruff and focused mypy on `runtime_auto_admission.py` pass. Raw receipts are
`/tmp/nebula-shutdown-readmission-tests.txt` and
`/tmp/nebula-shutdown-readmission-mypy.txt`. No ownership counters, release proofs,
claim rules or other product modules changed. Final combined production UI/browser
acceptance remains with the parent owner. This fixture is a source-runtime
backend proof, not an installed distribution or live-provider test.

## CI shutdown fixture follow-up

The original CI run on `eee508bc` failed the two actual-Core variants: SQLite
reported `CoordinationConflict` during the second shutdown, and PostgreSQL
reported `RuntimeIpcError` during the first. A scoped diagnostic retry on
`c4fb0ff5` reproduced only SQLite; PostgreSQL passed. None of the later recovery
reconciliation changes caused these original failures.

A separate fresh-context causal review confirmed the SQLite fixture raced
optional conversation naming. The second turn was complete at
`18:15:24.106480`, its shutdown operation was refused by `18:15:24.282`, and its
title's authoritative `initial_title_state=generated` write finished only at
`18:15:24.362612`. The signed worker refusal was “Conversation maintenance is
still active.” Each test chat creates a fresh session, so completing the reply
does not prove its separate naming task has finished. Reaching worker quiescence
also excludes a stale deployment revision as the cause of that refusal.

The readmission-only retained inert adapter now exposes an authenticated,
read-only observation of its actual naming and precompaction tasks. The fixture
waits for those tasks before each public shutdown operation, replacing the first
stop's fixed delay and adding the missing boundary before the second stop. It
also reads back the durable host shutdown state and includes fresh deployment
status if an operation fails. Product runtime code, shutdown guards, fencing
counters, paused inventory and exact process-absence assertions are unchanged.

Only the still-failing SQLite node was rerun after the repair:

```text
tests/v3/test_shutdown_readmission.py::test_actual_core_shutdown_reopen_readmits_existing_project_and_preserves_pauses[sqlite]
1 passed in 39.51s
```

Its authenticated source-Core LAN origins were `http://172.26.91.238:38771` and
`http://172.26.91.238:45763`. Both turns completed, the ownership fence advanced
from 1 to 2, the old lease was rejected, prior workers were absent, both paused
records were preserved, and both stops were read back from durable host state.
The original two-node diagnostic run recorded one failure and one PostgreSQL
pass in 87.58 seconds; that passing local case was not repeated.

External receipts and traces are
`/tmp/nebula-ci-shutdown-diagnostic-selection-receipt.json`,
`/tmp/nebula-ci-shutdown-diagnostic.txt`,
`/tmp/nebula-ci-shutdown-sqlite-selection-receipt.json`, and
`/tmp/nebula-ci-shutdown-sqlite-final.txt`. Focused Ruff check and formatting pass.
The PostgreSQL CI exception's inner cause remains unknown; its diagnostic pass
does not prove that original exception is repaired. No other tests, browser
matrix, installed distribution, existing installation or live provider was
exercised in this follow-up. This is fixture evidence for the stated backend
journey only.
