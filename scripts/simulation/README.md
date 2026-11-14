# Simulation Core deployer

Built releases retain the complete canonical runtime bundle at
`dist/nebula-runtime`, including its paired manifest and digest. Configure the
foreground production unit with
`Environment=NEBULA_RUNTIME_BUNDLE=/srv/apple-sim/nebula/current/dist/nebula-runtime`.
The outer release's deployment manifest is not a standalone runtime bundle.
Source placeholders under `ui/src-tauri/resources` are not deployable payloads.

Use `KillMode=process` and `ExitType=main` when replacing the foreground
supervisor so independently retained API and execution workers survive. A unit
stop then detaches the supervisor; use authenticated Core lifecycle controls for
full worker shutdown. Verify fresh work state and retained process identities at
the cutover boundary.

`nebula_deployer.py`, `nebula_staged_deployer.py`, and `nebula_deploy_gate.py` are the source for the
`simulation` user's installed tools in `/home/simulation/.local/bin`. The
`nebula-deploy.timer` runs `python3 -m nebula_deployer cycle` every 15 minutes.
The service token stays in the host's private environment file; these source
files contain no token value.

After a successful promotion, the deployer retains the newly verified
`rollback_backup` and removes older completed directories directly under
`/srv/apple-sim/nebula/rollback-backups`. It checks every target's name, type,
and mount boundary before deletion, and verifies that the retained directory is
the only completed snapshot left. Retention runs after the new Core passes its
authenticated health and accepted web-build checks. A cleanup failure returns
`promoted_cleanup_incomplete` while leaving the healthy Core and new rollback
snapshot in place; a timer cycle with no pending candidate retries cleanup.

The legacy offline rollback path preserves failed post-deployment state for investigation.
Do not delete that state during an unverified recovery just to meet the normal
one-snapshot count. Record the exception and resolve it after recovery is
verified.

`nebula_deployer.py prepare` runs its reviewed local test selection by default.
For a deliberate CI-backed preparation, pass `--ci-run-id <run-id>` once for a
one-commit candidate. For a candidate containing multiple first-parent commits
after the live release, repeat the option once per commit, oldest to newest,
such as `prepare --ci-run-id <first-run> --ci-run-id <head-run>`. The deployer
requires exactly one distinct successful `main` push run from the active
`.github/workflows/ci.yml` for every commit in the complete
live-baseline-to-head chain. Each run must match that exact commit and its first
parent. The deployer verifies each run's selection artifact SHA-256, validates
the committed selection against that commit's diff, and requires all selected
jobs to have completed successfully. The immutable acceptance record retains
the ordered per-commit receipts and selected-job conclusions; the reviewed
selection union must cover the entire first-parent range. Missing, repeated,
reordered, or mismatched runs fail closed. This mode skips local test collection
and test execution only; dependency setup, lint/diagnostic checks, builds,
release hashes, and existing staging/promotion guards still run. It fails closed
when a per-commit CI selection contains Playwright entries because the push CI
workflow does not run those entries. Runs from pull requests or workflow
dispatches are not accepted.

CI-only acceptance also requires the workflow files, local action definitions,
and `scripts/test_selection.py` to match the original live baseline for every
commit in the chain. This prevents a candidate from changing the checks or the
validator used to accept itself. Readback compares the stored ordered receipt
commits with the release's actual Git first-parent chain.

After the one-time coordinated transition to the supervisor architecture, run
`python3 -m nebula_deployer adopt-supervisor`. Adoption requires an authenticated
supervisor deployment status; it does not replace the running architecture.
Later `promote` operations submit durable stage and migration operations, activate
the accepted foreground supervisor, then prepare/promote the API while keeping
ingress available. Execution migration is serial: prepare one target, hand off
one project, verify its new owner and fencing token from fresh status, retire the
now-ownerless source, and confirm that retirement before preparing another
target. The foreground supervisor may retire a verified target for a project
with no open chat. Pinned projects remain on their source runtime; uncertain
handoffs remain journaled and stop further target preparation until reconciled.
The accepted runtime manifest hashes every executable/UI asset and uses the
build's reviewed compatibility metadata, including its migration-tree digest.

Foreground host activation is separately guarded from API and execution worker
promotion. `activate-host` targets only the exact accepted pending candidate
after its durable stage and migration receipts are verified. It records a
stable operation ID before stopping the foreground unit, atomically switches
`current`, and restarts only `nebula-core.service`. It requires
`KillMode=process` and `ExitType=main`, then checks the exact host commit and
recovery asset version while requiring every preexisting project owner ID,
runtime version, and fencing token and every prior resident execution version
to remain unchanged. Newly admitted owners are allowed only on the exact
activated candidate version; unexpected worker versions fail readback. Normal
`promote` uses this same guarded activation after migration and
before API preparation. Interrupted retries read the durable host journal and
verify live identity without repeating a completed restart. A failed exact
readback triggers a code-only switch back to the recorded immutable host
release; no database snapshot or file restore is part of activation or
`rollback-host`. An unverified rollback remains in `needs_attention` for
operator reconciliation.

Supervisor-mode `rollback` selects compatible retained code and preserves the
current database and files. It never restores a snapshot or stops pinned work.
Projects with unsupported resources keep their current runtime; a third execution
version stays staged until a resident version retires. Interrupted operation IDs
and their original arguments remain in `deployer-state.json`; retry reads their
durable receipts. If a promotion is blocked specifically by an uncertain
`prepare_api` operation whose exact Core receipt records `MemoryBudgetError`, use
`reconcile-runtime-deployment --deployment-id <journal-id>`. This guarded path
re-reads the Core stage, migration, and API receipts; verifies the active host,
authenticated health, old API version, target, and resident versions; then archives
the journal as a proven pre-spawn rejection while preserving the accepted pending
candidate. It does not retry the operation or edit Core's operation ledger. Other
uncertain outcomes remain blocked for separate reconciliation. An incomplete
cleanup is reported separately from a healthy API promotion.
`offline-rollback` refuses to run in supervisor mode. Coherent legacy snapshots
remain a separate disaster-recovery artifact, outside the code rollback path.
Legacy mutations also verify the live protocol before stopping a service. A
missing adoption marker or unavailable discovery never authorizes data restore.
An available supervisor response always blocks the offline path. Automatic
legacy recovery after failed candidate health uses only the positive legacy
classification captured for that same cutover attempt.

Install all three scripts together from a reviewed commit with atomic renames
while the deploy lock is free. Verify their hashes against the commit. This
updates deployment automation without restarting Core.
