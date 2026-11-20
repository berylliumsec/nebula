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

## Build ahead, and see where the time goes

`python3 -m nebula_deployer prebuild` does what `prepare` does, so that a later
`promote` or `cycle` finds the immutable release already built and only
verifies it. It is meant for a timer, so waiting states are results rather than
failures: it reports `waiting_for_ci` while main CI evidence is still running,
missing or failed, `skipped` while a deployment journal is unfinished or another
deployer run holds the lock, and `current` when main is already live. It never
promotes, never runs local tests and leaves Core untouched. A `promote` or
`cycle` that starts while a prebuild holds the deploy lock waits for it, up to
`PREBUILD_LOCK_WAIT_SECONDS` (30 min); any other busy holder is still refused
at once. A prebuild sets `pending`, so run it only on a host where promotion is
a manual step. An example user timer:

```ini
# ~/.config/systemd/user/nebula-prebuild.service
[Service]
Type=oneshot
WorkingDirectory=/home/simulation/.local/bin
ExecStart=/usr/bin/python3 -m nebula_deployer prebuild

# ~/.config/systemd/user/nebula-prebuild.timer
[Timer]
OnCalendar=*:0/5
Persistent=true
[Install]
WantedBy=timers.target
```

Every `prepare` reports each step's duration on stderr as it ends (`prepare:
core_build took 109.0s`), including the step a failed run stopped in, and keeps
them under `timings` in the release's `simulation-acceptance.json`. A
supervisor `promote` returns `timings` for staging, migration, host and API
promotion, execution targeting and project handoffs. Host activation logs how
long chat turns took to go quiet and records `stop_and_switch_seconds` and
`start_to_ready_seconds` in its journal.

Backups are operator opt-in: `promote --backup` (or `cycle --backup`) takes a
coherent rollback snapshot before a legacy release switch, or an online
PostgreSQL dump (`rollback-backups/operator-pre-<commit>-<time>`) before a
supervisor-mode promotion. Without `--backup` no backup is taken. A failed
legacy promotion without a snapshot then restores the previous release's code
and keeps the current data, and an older snapshot is dropped from the deployer
state so it can never be restored over newer data.

After a successful promotion that took a snapshot, the deployer retains the newly verified
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

`nebula_deployer.py prepare` accepts CI evidence by default: for every
first-parent commit from the live release to `origin/main` it finds the newest
successful `main` push run of `.github/workflows/ci.yml` and verifies it as
below, without re-running tests on this host. A commit with no successful run
(none yet, failed, or still running) or with Playwright entries (which push CI
does not run) stops preparation with the commit and reason; the deployer never
falls back to local tests on its own. `prepare --local-tests` re-runs the
reviewed selection on this host instead. To name runs explicitly, pass
`--ci-run-id <run-id>` once per commit, oldest to newest, such as
`prepare --ci-run-id <first-run> --ci-run-id <head-run>`. The deployer
requires exactly one distinct successful `main` push run from the active
`.github/workflows/ci.yml` for every commit in the complete
live-baseline-to-head chain. Each run must match that exact commit and its first
parent. The deployer verifies each run's selection artifact SHA-256, validates
the committed selection against that commit's diff, and requires all selected
jobs to have completed successfully. Each required job is a job family: the
exact job name, or GitHub's matrix instances named `<job> (<matrix values>)`.
A family needs at least one job, and every job in it must have completed with
`success`; a failed, cancelled, or unfinished instance fails closed. A bare
prefix never matches, so `python (3.12)` stays separate from `python (3.11)`.
A selected terminal client test therefore requires every `terminal-client`
matrix job, whatever runners and Python versions the matrix lists. Acceptance
records key the family as `terminal-client`; records written earlier with the
six exact Ubuntu/macOS and Python 3.11–3.13 job names still validate, and no
other substitute does. The immutable acceptance record retains
the ordered per-commit receipts and selected-job conclusions; the reviewed
selection union must cover the entire first-parent range. Missing, repeated,
reordered, or mismatched runs fail closed. This mode skips local test collection
and test execution only; dependency setup, lint/diagnostic checks, builds,
release hashes, and existing staging/promotion guards still run. Push CI does
not run Playwright entries, so a commit whose selection names them is accepted
only with its merged pull request's successful `ci.yml` and
`playwright-impact.yml` runs, which `prepare` discovers on the PR head. For a
merge commit the head is its second parent. For a squash merge it is the head of
the one PR GitHub records as merged into that commit, fetched from
`refs/pull/<n>/head` if needed; that head must contain the squash commit's
parent and have the identical tree, so it tested exactly what main received.
Matrix jobs are matched to selected entries by GitHub's displayed job name, which
is cut at 100 characters; entries that truncate to the same name each still need
their own successful job. A Playwright-selected commit without such PR evidence
stops preparation. Other pull request or workflow dispatch runs are not accepted.

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
with no open chat. A source that has not handed off within
`HANDOFF_GRACE_SECONDS` (180 s), including a pinned project or a source shared by
several projects, is stopped with the journaled `evict_execution` operation.
Core refuses to stop the API worker, a worker already on the deployment target,
or an owner whose grant changed since the deployer journaled it. Expired-owner
recovery settles the stopped worker's leftovers without replay and restores its
projects on the target version. The deployer accepts that recovery only when
the project's `recovery` status names a verified recovery that started at or
after the exact source grant it journaled, with no blockers and a ready target.
It then retires the unused target it prepared. A recovery onto an older
version is handed off again under a new journal generation. One run polls until
no project is left waiting, or until a wait has made no progress for the grace
period plus `HANDOFF_RECOVERY_WAIT_SECONDS`. An interrupted command record no
longer pins a project, because no process in the old worker still runs it.
Uncertain handoffs remain journaled and stop further target preparation until
reconciled.
The accepted runtime manifest hashes every executable/UI asset and uses the
build's reviewed compatibility metadata, including its migration-tree digest.

Before stopping the foreground supervisor, `promote` checks every API and
execution version in deployment status. When each is an exact commit that
includes #1023 (workers wait out a supervisor replacement for host reads),
running chat turns cross the restart and the deployer does not wait for
projects to go idle. If any version is unknown or older, it still waits up to
`HOST_QUIET_WAIT_SECONDS` for chat work to settle and refuses otherwise.

When a version predates #1023 and the Core supports project holds (#1232),
`promote` holds each busy project instead of waiting for it to settle, waits
until no held project has chat work (up to the hold grace plus
`PROJECT_HOLD_WAIT_SECONDS`), restarts the supervisor and then releases those
holds, also when activation fails. `hold --reason <why> [--project <id>]...`
and `release-holds` place and release holds for a stop or wind-down; every
placed hold is saved in `project-holds.json` until Core confirms its release,
and a host restart releases only the holds it placed.

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
Normal promotion and rollback retries refresh the saved operation receipt first.
A definite no-effect refusal receives a new numbered attempt and current revision;
unknown effects are not replayed. The deployer reconciles confirmed API promotion,
project ownership and retirement from current deployment status.
An unknown migration is settled only when a read-only query against the configured
Core database confirms that release's exact accepted migration head as the current
database head. This works before activation and never runs a migration. Missing
process inventory on an older host is unknown, not an empty inventory. A worker
start remains held until its original receipt confirms completion or no effect;
absence alone cannot prove an in-flight request has stopped. Preserve the journal and
use the recorded operation ID to inspect it; never edit it to force progress.

When a later manual installation superseded a managed promotion, use
`reconcile-superseded-manual-deployment --deployment-id <journal-id>
--manual-receipt-prefix <YYYYMMDD-commit8>`. The command verifies the exact later
stage, migration, host switch, API promotion, and final readback receipts, then
checks the authenticated current host/API identity and confirms the old release
has no process, resident version, or project owner. It archives the complete old
journal and its Core receipts, including every uncertain receipt; it never
replays those operations or labels them no-effect. A currently degraded
deployment aggregate requires the explicit
`--allow-degraded-baseline` flag. This records the degraded status while still
requiring healthy API/database identity and exact host process readback. Other
legacy attempt journals are preserved unchanged; this command does not settle
their uncertain effects.

Run the same checks without changing deployer state by adding `--dry-run`. It
returns the proposed classification, verified receipts, live readback, uncertain
effects to preserve, and state alignment. Rerun without `--dry-run` only after
reviewing that summary.

If a later verified manual host release superseded a promotion and an attempted
code rollback stopped before host activation, use
`reconcile-unactivated-supersession --deployment-id <rollback-journal-id> --dry-run`.
This command accepts only a promotion with confirmed stage and migration receipts
nested inside a rollback with a confirmed stage receipt. Both host activations
must be merely requested. It checks the current release's accepted or reconciled
identity, live service process, authenticated Core health and runtime inventory,
and the exact database migration head. It preserves both journals and all three
Core receipts in deployment history, then clears only the stale pending candidate
and aligns host state. Review the dry run before repeating without `--dry-run`.
Any extra operation or live candidate runtime blocks reconciliation; never edit
the journal to make it match.

New hosts expose authenticated operation reconciliation at
`POST /api/v1/deployment/operations/{operation_id}/reconcile`. It never repeats
an effect. New operations retain private origin and child identities before
bootstrap admission; reconciliation rejects active invocations and live migration
children, then records either exact completion or a verified no-effect refusal.
The original outcome remains in the deployment receipt history. Older operations
without this linkage remain held, and an older host without this endpoint reports
that limitation explicitly. Do not substitute process-list absence for this proof.

A rollback can replace an unfinished promotion once its original operations have
terminal receipts, while preserving its complete journal. It restores compatible code, API and project targets without downgrading
or restoring the database. Completed host activation is read back against its own
journaled later API promotion, so rerunning promotion does not reject its own API.

Deployment inventory reads for goals, missions, notebook runs and execution history
are served from SQL without starting a project worker. The workspace gate still
fails closed if a live owner does not answer. Idle retirement is requested only
from releases advertising `project.idle-retirement/v1`; older workers remain
available until the explicit handoff.

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
