# Project execution role

This stage connects the previously separate execution, durable-delivery and host
service components. Compatibility launchers remain on their existing architecture
until client reconnection, handoff and packaged desktop acceptance are complete.
It does not restart or deploy an existing installation.

| Journey | Observable invariant | Authority | Selected validation |
| --- | --- | --- | --- |
| Start a retained worker | No controllers before an ownership grant | Supervisor and SQL | Actual private process |
| Attach a project | One permanent project/token per process | SQL ownership | SQLite and PostgreSQL |
| Submit and retry | One committed mutation for one receipt | Durable command and delivery rows | Inert ASGI and actual kernel |
| Stop or decide | Control delivery proceeds while work is occupied | Independent durable control lane | Held inert request |
| Read and recover | Reads and child tasks retain the original token | Fenced application pool | Real connection isolation |
| Host disconnect/restart | API and execution children retain their identity | Process birth and private IPC | Disposable supervisor replacement |
| Lose ownership | Old tasks cannot start or release compute reservations | Shared SQL memory ledger | Concurrent connections |
| Missing shared service | Startup reports failure without opening a local replacement | Host capability RPC | Actual retained process |
| Worker shutdown | Controller cleanup is joined through caller cancellation | Execution lifespan | Cleanup barrier |

Each project process receives its own declared service-memory reservation. The
two-version limit still applies across all resident execution processes. A saved
Core reserve that cannot accommodate another project holds startup; it is never
silently increased. Per-job kernel containment remains independent of this
service admission accounting.

Memory writes use the same transaction as exact-token ownership checks. Ordinary
project workers cannot edit the host policy or mutate another project's jobs.
The global control project retains the existing authenticated policy interface.
Each project's recovery scan is restricted to its own jobs. Supervisor-owned
orphan cleanup and runner restoration still require the remaining integration.

Controller browser directories are process-specific; project workspaces,
artifacts and durable mission checkpoints retain their stable host paths. Shared
Chroma and Butler memory are accessed only through the private host service.

This stage has no new visible controls and does not establish the full operator
continuity journey. Durable turn admission/stream cursors, complete handoff
inventories, global read routing, desktop lifecycle and launchers remain in
progress. Final authenticated production LAN, mobile WebKit and packaged desktop
checks remain required before claiming non-disruptive deployment support.

Validation used Python 3.12.14, separate SQLite connections, unique disposable
PostgreSQL schemas, owner-only Unix sockets and retained inert executable
wrappers invoking the actual worker and kernel. No provider, harness workload,
model download, SSH command or operator installation was used.

The first 40-case run passed 38 and exposed a kernel recovery query rejected by
the SQL guard: derived columns belong to SQLAlchemy Subquery/Alias objects with
no table key. Independent expression reconstruction reproduced that fault. The
guard now visits the underlying mapped inputs without treating a derived column
as a schema table; all 18 selected scoped/fail-closed query checks passed.

Per-process diagnostics then exposed a second startup fault: memory lifecycle
events used an unregistered diagnostic feature. A fresh validator reproduced it
with a real DiagnosticManager at the normal error threshold. Memory lifecycle
events now use the existing runtime category. Real diagnostics are included in
the recovery and daemon-replacement fixtures, including cancellation callbacks.

Collected runs and raw evidence:

- Initial 40: 38 passed, 2 failed in 39.89 s (`/tmp/nebula-project-execution-tests.txt`).
- SQL guard repair 22: 20 passed, 2 failed in 21.32 s (`/tmp/nebula-project-execution-repair-tests.txt`).
- Diagnostic repair 8: 8 passed in 16.64 s (`/tmp/nebula-project-execution-diagnostics-tests.txt`).
- Receipt/settings assertion check 2: 2 passed in 13.67 s (`/tmp/nebula-project-execution-receipt-tests.txt`).
- These runs cover 64 distinct passing cases; unaffected passing cases were not
  repeated. Ruff and targeted mypy passed. The two warnings are existing
  WebSocket dependency deprecations.

The actual-kernel journey verifies the same idempotency receipt on retry, one
project revision change, private authenticated API-to-execution reads, separate
controller paths, no local memory/index fallback, and the same API/execution
process identities after supervisor replacement. This is source integration
evidence, not the required active-chat production LAN or desktop update evidence.
