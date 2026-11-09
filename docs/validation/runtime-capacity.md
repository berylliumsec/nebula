# Runtime overlap capacity

The SQL host-capacity ledger shares provider, background, tool and weighted CPU
admission across execution generations. It is separate from compute memory:
these units bound concurrency; the memory guard enforces each job's kernel limit.
An additive migration retains reservations across API, worker and supervisor
restart. Application execution pools cannot mutate this host authority directly.

The supervisor configures one revisioned policy. A second initializer cannot
silently replace a different policy. An explicit revisioned reduction retains
current reservations and drains new admission. Every requested dimension reserves
atomically under a common lock; unsupported/unconfigured dimensions fail closed.
Acquisition requires the exact current project fence. Immutable receipts make
lost-response lookup safe, and old receipts cannot release a new incarnation's
capacity. Cancellation after SQL commit joins acquisition and releases known-idle
capacity before returning. Lease expiry alone never releases an unknown vendor
or tool effect; such reservations also prevent worker retirement.

Provider admission and all four chat broker paths use the ledger when the new
execution kernel receives its host capacity dependency. The four paths are
ordinary tools, parallel reads, planning discovery and approval resume. Legacy
compatibility launchers do not yet supply this dependency; this evidence does
not enable overlapping legacy Cores or certify the complete architecture. CPU
callers and host-owned capacity resolution still require lifecycle integration.

The initial 37-case selection passed 36 cases in 28.73 seconds, including all 26
SQLite/PostgreSQL ledger cases, four cancellation-cleanup cases, four real inert
worker retirement cases and two provider/approval regressions. The new real tool
loop exposed a bypass: only multi-call parallel waves acquired the tool slot.
Fresh independent diagnosis reproduced an inert sequential turn completing
while the tool reservation was occupied, with no `tool:` acquisition. This ruled
out earlier admission failure, missing context and deadlock. Broker invocation is
now centralized. The seven affected cases passed in 6.07 seconds: three new real
serial/parallel/approval loops and four existing exact planning, parallel and
approval regressions. No operational provider or compute workload was used.

Ruff passed; targeted mypy passed for the ledger, scheduler and chat service.
Collection, receipts and raw outputs are `/tmp/nebula-runtime-capacity-*`.
No frontend changed in this step. The complete production LAN, mobile WebKit and
packaged-desktop deployment-continuity acceptance gates remain pending.
