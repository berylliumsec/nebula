# Recovery before the memory launch boundary

The independently reproduced failure on `e38aade3` committed a 32 MiB reservation
at revision 2, then awaited the second runner identity probe with no durable
owner. SIGKILL bypassed normal cancellation. Fresh host and project reconcilers
ignored the ownerless reservation, so three recovery passes left new work queued.
Original evidence: `/tmp/nebula-ownerless-validation-unhmxy5j/receipt.json` and
`/tmp/nebula_ownerless_validate_20261109.py`. The independent validator also
confirmed that ownerless `starting` cannot safely be reclaimed.

The controller now supplies its process ID, exact kernel birth, PID namespace
origin and runner fingerprint in the same transaction that charges capacity.
The second runner probe stays after this commit. `reserved` is durable proof
that the controller has not crossed the runner launch boundary; the separate
`starting` transition still precedes every `run_stream` invocation and now checks
the reservation owner as well as its revision. That transition may enrich the
same owner's previously unknown daemon identity.

`release_prelaunch` checks the locked row's revision, exact owner, `reserved`
state, absent groups and absent enforcement evidence. Recovery additionally
requires comparable process origin and positively observed death. Process
inspection failures retain the charge. Normal cancellation requires the actual
current process incarnation. The release audit records `never_launched: true`
and `groups_cleaned: false`; it creates no fictitious group-empty evidence.
The existing project transaction fence applies to this mutation unchanged.

Live owners, foreign origins and legacy ownerless reservations remain held.
`starting` without acknowledged groups still becomes uncertain and stays charged;
known-group recovery continues requiring the original runner and immutable-group
cleanup proof. No time-based reaper or implicit retry was added.

The bounded local selection is exactly nine cases in
`tests/v3/test_memory_prelaunch.py`: two actual controller child processes killed
at the second info probe (host and `ExecutionMemoryBudget`), five conservative
hold boundaries, duplicate-controller cancellation versus stale launch, and two
independent SQL connections racing release against launch. The project death case
also checks replacement-lease recovery and rejection of the original stale lease.
Per-case JSON receipts record the committed owner, process death and truthful
release audit. The external diff-bound plan/receipt is
`/tmp/nebula-memory-prelaunch-selection.json`; the execution receipt directory is
`/tmp/nebula-memory-prelaunch-tests`.

These are isolated Linux process and SQLite tests with a nonexecuting runner.
No Docker socket, daemon workload, provider, build, installation or live Core is
used. PostgreSQL and real runner cleanup are outside this selection; existing
post-launch cleanup behavior is preserved and is not re-certified here.
