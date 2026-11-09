# Canonical worker feature admission

The production release manifest declares eight implemented features. Worker
startup now admits the three existing provider-tool context, standing-instruction,
and host-search query constants alongside its five original flags. Any unknown
declaration still refuses startup before an application factory or listener.

When `provider.tools-context/v1` is declared, startup executes a zero-row query
against `ProviderToolManifestRow` before readiness. Missing tables or columns
require the supervisor's additive migration; the worker does not migrate them.

| Boundary | Observable invariant | Focused evidence |
| --- | --- | --- |
| API startup | The canonical manifest reaches real API factory and private authenticated HTTP | Actual retained `runtime_worker.main` subprocess |
| Execution startup | The canonical manifest reaches the real unassigned execution factory | Actual retained main and execution status RPC |
| Unknown feature | No factory, socket, diagnostics or readiness | Actual `run_worker` with valid committed identity |
| Missing provider schema | No factory/readiness, migration head unchanged and missing table remains absent | Actual admission on SQLite/PostgreSQL |

Selection: twelve cases in `tests/v3/test_runtime_worker_features.py`. Four start
actual API/execution subprocesses across SQLite and PostgreSQL using all canonical
features. The checksummed retained bundle contains the candidate Nebula package;
the replaceable source directory is deleted before startup, and each child logs
its retained worker module path. Eight refusal cases call the real admission
function with valid private bindings and factory tripwires. They do not substitute
the inert fixture `_WORKER` for production admission.

All twelve cases passed in 21.85 seconds on the first run, with real PostgreSQL
enabled. API workers answered private authenticated HTTP with their retained
version header; execution workers exposed the real unassigned status RPC. Both
roles reached committed ready state with all eight canonical features. Refusal
cases preserved their starting state, absent listener/factory/diagnostics, the
original Alembic head, and the absent provider table.

Ruff and focused mypy on `runtime_worker.py` pass. Raw receipts are
`/tmp/nebula-worker-feature-tests.txt` and
`/tmp/nebula-worker-feature-mypy.txt`. The parent acceptance track owns the final
retained production bundle and LAN/browser journey rerun. No provider, harness,
managed compute or external service starts in this admission slice.
