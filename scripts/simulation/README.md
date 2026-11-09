# Simulation Core deployer

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

Install all three scripts together from a reviewed commit with atomic renames
while the deploy lock is free. Verify their hashes against the commit. This
updates deployment automation without restarting Core.
