# PR 270 whole-branch merge review

Base: `450ef96bc3e6e782190f3635a978ce7f4974f3d8` (`main`). This review covers
the accumulated branch, not just the working-knowledge commit. It authorizes
bounded regression selection and merge, not deployment or live-session activity.

| Journey | Invariant / authority | Selected layers |
| --- | --- | --- |
| Assistant approve, reject, stop, restart, reconnect | Core decisions and revisioned session state remain authoritative; no command replay | Approval/session/adapter unit tests; deterministic real-Core browser journeys |
| Browser and internal knowledge | Assistant-only page control, scoped stored evidence, retained pause/approval/privacy boundaries | Model/browser/security tests; read-only viewer and context/approval browser journeys |
| Project/workspace/resources | URL and durable project own selection; linked folders survive removal; drafts remain record-bound | API/workspace/storage tests; lifecycle/resource browser journeys |
| Layout/navigation/recovery | Composer and actions reachable at short/mobile sizes; recovery follows actual connection state | Changed component tests; production desktop and mobile Chromium/WebKit matrices |
| Migration/build identity | Upgrades retain records; build identities and selection receipts describe actual artifacts | SQLite/PostgreSQL migration gates; build/selection tests; production build |

Collect and review exact selections before execution. Include all changed Python
test files, migration integration, and all changed component test files; this is
not the full repository suite. Browser batches use explicit files, projects and
feature filters. Preserve earlier acceptance evidence without claiming it is a
new run. No external target, configured live runtime, existing session, or live
data clearing is part of these tests. Native source is unchanged; updated native
acceptance helpers still require a future package run. Physical devices remain
unverified. A merge does not constitute desktop/LAN release acceptance.

Collection: 454 Python cases and 247 component cases. PostgreSQL's integration
case requires its dedicated CI service; a local skip does not replace that gate.
Browser collection: 238 cases, split into 147 responsive UI cases, 10
short-landscape viewer cases, and 81 real-Core recovery/resource cases. Exact
files/projects/filters live in the `branch-review` impact catalog and reviewed
selection. Collection logs: `/tmp/nebula-pr270-browser-*-selection.log`.

Local component run: **247 passed**, `/tmp/nebula-pr270-ui.log`. Core subsystem
batches are logged separately under `/tmp/nebula-pr270-python-*.log`. The PR's
required CI jobs will run all selected cases against this reviewed branch; their
check URLs retain final results without regenerating commits merely to record
each passing job. Do not infer success from collection or a skipped check.

Expanded checks found two stale contracts: graph-only chat already includes the
read-only relationship-options tool, and migration 0014 deliberately refuses
downgrade because deleted experimental projection records cannot be reconstructed.
The chat assertion now includes that tool. Migration coverage still exercises the
complete reversible chain through 0013, then upgrades to head, asserts that the
0014 downgrade guard fails explicitly, checks retained tables, and restores head.
The guard itself is not weakened. The targeted repeat passed 3 cases locally;
PostgreSQL remains a separate required service-backed CI check.

The first expanded CI run passed all 453 non-PostgreSQL Python cases on Python
3.13, plus the dedicated PostgreSQL migration job; its later formatting gate
failed. Local full-Chromium launch timed out before the DOM fixture, while that
same test passed on CI. It is not omitted from the selection. Follow-up gate
repairs normalize the 13 reported files, document expected recovery paths already
surfaced through receipts/UI, add precise internal typing and a missing-receipt
guard, and use the selector's required `entry:` prefix. No diagnostic auditor,
type checker, branch protection or workflow gate is disabled.
