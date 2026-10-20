# Simulation Core deployer

`nebula_deployer.py` and `nebula_deploy_gate.py` are the source for the
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

The rollback path preserves failed post-deployment state for investigation.
Do not delete that state during an unverified recovery just to meet the normal
one-snapshot count. Record the exception and resolve it after recovery is
verified.

Install both scripts together from a reviewed commit with atomic renames
while the deploy lock is free. Verify their hashes against the commit. This
updates deployment automation without restarting Core.
