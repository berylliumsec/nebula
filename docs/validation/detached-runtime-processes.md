# Detached runtime process boundary

Validated locally against synthetic SQLite databases and disposable PostgreSQL
schemas. No existing installation or operational service was contacted.

- 21 selected API/authentication/chat/memory cases passed after extracting the
  canonical execution factory and shared authentication boundary.
- 19 private HTTP/WebSocket transport cases passed, including cookie isolation,
  authenticated client metadata, bounded connections, independent control
  capacity, and cancellation-safe cleanup.
- 23 startup/process cases passed across focused runs. Real inert processes
  survive graceful supervisor exit and supervisor SIGKILL and are rediscovered
  through authenticated RPC. Bootstrap backpressure respects deadlines. Failed
  starts retain their version slot until verified cleanup; cancellation after
  readiness publication leaves an admitted worker alive.

The first process run passed 17 cases. Six cases failed because the fixture
omitted two synthetic Engagement entities. A fresh-context independent validator
confirmed that setup fault; seeding those entities made the exact six cases pass
in 14.99 seconds. The production project-existence check was preserved.

Ruff and targeted mypy checks passed for the new transport, process,
authentication and IPC modules. The last diff-bound selection is recorded in
`.github/test-selection.json`; local raw outputs are under `/tmp/nebula-*`.

These fixture executables import repository libraries. This evidence does not
certify packaged retained dependencies, desktop update/reopen, the complete role
wiring, or deployment continuity. Mobile WebKit remains blocked by the managed
environment's browser-download policy; packaged desktop evidence is absent.
Keep legacy overlap disabled until the remaining integration gates pass.
