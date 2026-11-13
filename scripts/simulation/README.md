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
For a deliberate CI-backed preparation, pass `--ci-run-id <run-id>`. The
deployer accepts only a successful push run from the active `.github/workflows/ci.yml`
for the exact `main` candidate whose first parent is the live release baseline.
It verifies the run's selection artifact SHA-256, independently validates the
committed selection against that exact diff, and requires the selected CI jobs
to have completed successfully. This mode skips local test collection and test
execution only; dependency setup, lint/diagnostic checks, builds, release hashes,
and existing staging/promotion guards still run. It fails closed when the CI
selection contains Playwright entries because the push CI workflow does not run
those entries. A run from a pull request, workflow dispatch, a different
baseline, or a multi-commit first-parent range is not accepted.

After the one-time coordinated transition to the supervisor architecture, run
`python3 -m nebula_deployer adopt-supervisor`. Adoption requires an authenticated
supervisor deployment status; it does not replace the running architecture.
Later `promote` operations submit durable stage, migration, API promotion and
project handoff operations while keeping ingress available. The accepted runtime
manifest hashes every executable/UI asset and uses the build's reviewed
compatibility metadata, including its migration-tree digest.

Supervisor-mode `rollback` selects compatible retained code and preserves the
current database and files. It never restores a snapshot or stops pinned work.
Projects with unsupported resources keep their current runtime; a third execution
version stays staged until a resident version retires. Interrupted operation IDs
and their original arguments remain in `deployer-state.json`; retry reads their
durable receipts. An uncertain operation needs explicit reconciliation, and an
incomplete cleanup is reported separately from a healthy API promotion.
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
