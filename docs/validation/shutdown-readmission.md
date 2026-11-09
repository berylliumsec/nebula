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
