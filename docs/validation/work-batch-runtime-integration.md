# Work batch runtime integration

Recorded 2026-11-09. The six cases in
[`test_runtime_work_batch_integration.py`](../../tests/v3/test_runtime_work_batch_integration.py)
have passing evidence across three focused phases: **4 + 1 + 1 distinct cases**.
There was no final six-case rerun. The compact
[receipt](work-batch-runtime-integration-results.json) records fixture hashes,
source hashes, raw-output hashes and the original failures.

## Captured inputs

Product source came from root commit
`5fb00b8a1460175b0d8dea514319adb6d8b6c2c1` plus the captured working-tree
changes in `runtime_global_delivery.py`, `runtime_host_delivery.py`,
`runtime_host_services.py`, `runtime_work_batches.py` and `runtime_worker.py`.
The source tree SHA-256 was
`ef183ee52baa9ec53ac6a3abebacf5e9cfdeb6911799afd080fc521ac0bd02ff`.
Those product bytes remained unchanged through all three phases.

The final test-only commit is `f391c88a`. The source bundles independently retain
their interpreter, dependencies, code and assets. Both release labels in the
public journey contain this captured source; this is process-replacement
evidence, with no historical released-binary compatibility claim.

The static assets are the verified production build from
`4a39c121975bcdad01f1cbddc1c94b934ef6dc15`. They satisfy the retained bundle's
asset contract for these backend cases. No browser or current UI behavior was
tested in this run. The generated product-knowledge asset hash is recorded in
the receipt.

## Selected execution

Collection found three test functions, each on SQLite and PostgreSQL: **six
cases**. PostgreSQL used unique disposable schemas. All mutation requests used
normal authentication and immutable idempotency keys.

| Phase | Exact selection | Result | Seconds | New distinct passes |
| --- | --- | --- | ---: | ---: |
| Original | Dedicated module, six cases | 4 passed, 2 failed | 152.79 | 4 |
| First fixture correction | Public retained-CLI case, both databases | 1 passed, 1 failed | 182.34 | 1 |
| Exact-identity correction | Public retained-CLI case, PostgreSQL only | 1 passed | 115.39 | 1 |

The public case selector is
`tests/v3/test_runtime_work_batch_integration.py::test_retained_cli_import_progresses_without_api_and_keeps_batch_receipts`,
with `[sqlite]` and `[postgresql]` parameter suffixes. The local diff-bound
selection receipts and fixture versions are listed in the JSON receipt. No
other backend cases or browser tests were repeated for this workstream.

## Observed invariants

The private IPC cases start real owned peers. API peers can invoke only the
signed Work coordinator capability; execution peers cannot invoke it. Closing
the owned coordinator reports readiness false. A wrong secret, a cached peer
whose process is no longer ready, and an expired runtime are rejected.

The public cases enter the real retained `desktop-connect` and `runtime_service`.
After an import is durably accepted as queued, the test kills the exact API
PID/birth. Direct SQL observation sees the original command become succeeded
and its two project barriers complete. No Work status/response request,
reconciliation call, browser polling or resubmission drives that progress.

The original HTTP request receives the supported 503 reconnect response. After
automatic recovery has produced a different authoritative live API, the test
explicitly stages, prepares and promotes `acceptance-v2`. The original
actor/key lookup returns the same command, with the same Core/operator identity.
Both imported projects own their default scopes, Work items and check-ins.
Metadata batches create no execution worker or global project ownership row.

The candidate API accepts a two-project hierarchy/Work-setting edit, increments
each revision once, and saves a cycle refusal as HTTP 409 without changing the
parent. Normal `runtime-stop` succeeds. The recorded Core and worker process
births are absent, and the owned runtime data is removed.

## Failure preservation and causal review

The original test incorrectly rejected the supported post-acceptance 503.
Fresh review also found two later assumptions: it would retire an API Core had
already retired, and redeem a paired device through the plain HTTP desktop
connector. Independent review validated these fixture contract errors before
correction.
The CLI journey retains normal operator authentication; paired foreign-actor
receipt isolation belongs to the separate HTTPS authentication fixture.

The first correction also exposed an insufficient recovery predicate: health
and version could still describe the killed API's SQL row before process
reconciliation. A second fresh review verified that the explicit candidate
started before automatic recovery had created its replacement. The final
predicate requires the old row retired and its exact process dead, a different
authoritative API pointer, and a ready replacement matching a live PID/birth,
followed by normal public health/version. It uses bounded observation rather
than a fixed delay or replaying an uncertain operation.

The prior PostgreSQL promotion's precise `CoordinationConflict` branch and the
initial PostgreSQL cleanup cause were not retained; they remain unspecified.
The final success does not reconstruct those erased details. The test now saves
host revisions, process versions, runtime leases, operation receipts and
deployment receipts before failure cleanup. Earlier evidence remains under
`/tmp/nebula-work-batch-integration-first-failure` and
`/tmp/nebula-work-batch-integration-second-failure`; the consolidated local JSON
is `/tmp/nebula-work-batch-integration-complete-execution.json`.

## Scope

This receipt covers backend IPC and the actual source Core's loopback desktop
connector. Production LAN/browser, physical-device, native-window and packaged
binary gates have separate receipts. These cases use synthetic identities and
data, with no vendor service, installed harness, OCI runtime or existing Core.
CI needs built `ui/dist`, the generated knowledge asset and an explicit
`NEBULA_TEST_POSTGRES_URL`; a browser runtime is not required for this module.
