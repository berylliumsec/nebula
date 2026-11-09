# Enforced compute memory budgets

This work is part of the independent host/API/execution architecture. It must
use one SQL reservation ledger for every resident runtime version, not a
process-local semaphore. An available-memory observation is admission telemetry;
it never proves that a job has a lifetime limit.

## Execution boundaries

| Boundary | Previous control | Current memory boundary |
| --- | --- | --- |
| `sandbox.py`, `runtime_platform.py` command and parser containers | Per-request OCI memory flag and runtime certification | Shared SQL reservation, frozen policy, explicit swap, protected guard/readback, full container tree |
| `automation_runtime.py`, Docker `notebooks.py` sessions | Persistent container/session lifecycle | One persistent worker reservation; commands, Notebook kernels and background children share its limits |
| Host command sessions and Host Notebook kernels | Host subprocesses, timeouts and policy grants | Held before process creation pending a protected local runner |
| Native harness backends in `harnesses.py` | Vendor adapter lifecycle | Requires `open_memory_managed`; no fallback to unrestricted `open` |
| `environments.py` SSH tools | SSH command and timeout | Held before SSH submission pending a remote memory agent |
| `reporting.py`, `report_cpu_executor.py` | Thread fallback or CPU affinity/queue bounds | Service-level hold before pool startup or dispatch, including injected executors |
| Interactive terminals and owner-mapped debugger tools | PTY or read-only bind with a mapped owner | Held pending protected PTY and filesystem-export adapters |

Available-memory/host-pressure telemetry and CPU affinity are admission or CPU
controls; they cannot enforce a job's lifetime memory ceiling. Fixed runtime
inspection and cleanup operations are trusted administrative controls. Arbitrary
shell or SSH commands cannot acquire that classification from a caller's label.
Core, SQL, indexing, browser control and external configured integration services
use the service reserve; they are not represented as per-job protected compute.
Nebula's managed local search service is an explicit exception: its one
`web_search_service` job reserves aggregate capacity for the entire SearXNG
process tree. Queries do not create additional reservations. It is not an
API/execution runtime record and adds no second search-specific Core-reserve
charge. The administrator's Core/SQL/other-services floor remains unchanged;
Nebula never guesses how much of an old reserve an operator intended for search.
An external service's own work is outside this runner's containment scope.

The search service follows normal administrator, host, container-environment and
profile precedence. Its request can only lower the selected ceiling, to at most
512 MiB RAM and zero swap. Required memory.high still causes refusal on an adapter
without that capability. The durable job snapshot, verified group and usage are
visible through the same Memory budgets API and Butler actions as other jobs.
The stable host owns the service; project tools query it through that host.

Only a verified cached official image may start. Fixed image inspection and
cached-image verification remain administrative controls. Image pull/build is
held before any client starts: Docker/BuildKit does the work in daemon workers,
so a memory limit on the CLI would not contain it. A provisioning runner must
atomically reserve capacity, own immutable daemon/build-worker identities, apply
and read back hard RAM/swap/pressure controls before work, report usage/OOM, and
prove every descendant stopped before release. No such daemon adapter is
implemented here; an absent or stale cached image reports that dependency.

## Operator contract

| Journey | Required observation | Owner | Validation |
| --- | --- | --- | --- |
| Discover | Settings exposes Memory budgets, units, ranges, scope and runner support | Settings catalog and authenticated Core API | Component and production browser |
| Save/read back | Valid revisioned configuration persists; stale saves conflict | SQL policy and audit ledger | Real Core and restart |
| Submit | Durable job records freeze effective policy and queue when full | Shared SQL admission | Concurrent SQLite/PostgreSQL fixtures |
| Launch | Kernel limits and descendant containment are verified before computation | Trusted runner | Small real allocation workloads |
| Run/observe | Usage, peak, limits and application state are distinct from saved policy | Runner evidence plus SQL records | Real runner and UI/Butler readback |
| Cancel/recover | Descendants are confirmed gone before capacity is released | Runner reconciler | Cancellation, launch failure, worker loss |
| Change running limit | Separate revisioned action explains possible termination | Core plus verified runner application | Explicit confirmation and failure paths |
| Memory failure/retry | Only kernel evidence establishes memory_limit_exceeded; retry is explicit | Durable outcome and artifact capture | Allocation failure, refresh and restart |

Policy precedence is administrator ceilings, host policy, environment defaults,
permitted profile overrides, then a job request that may only lower its selected
profile ceiling. A job may not remove or raise a configured pressure threshold.
Reserve memory for Core, its database and essential services before calculating
available aggregate capacity. Swap is explicit and defaults to zero. A runner
without verified memory.high support must reject a requested pressure threshold.

Changes apply to newly submitted jobs. Running and already queued jobs retain
their durable snapshot. Lowering aggregate capacity leaves current reservations
charged, holds new admissions, and reports draining rather than claiming the new
target already bounds existing jobs. Applying a new limit to an existing job is
a separate action, with a revision and explicit acknowledgement if it can kill
the job. Capacity is not released until observed limits or cleanup confirm it.

The runner contract is versioned as `nebula.memory-runner/v1`. It reports hard
limits, pressure/swap support, protected process-tree containment, live-update
support, observed application, peak usage, OOM evidence and whether the resource
group is empty. Authentication and runtime ownership protect that report; a
public caller cannot submit its own enforcement evidence. Reported configuration
alone is insufficient. Unknown worker outcomes remain charged until the trusted
runner verifies cleanup, preventing overlap from multiplying available capacity.

The launch record binds the owner's OS process birth identity and runner
configuration fingerprint. Immutable workload and helper group IDs are committed
after creation and before start; readiness and subsequent measurements preserve
that ownership metadata. Recovery checks actual owner death, checks the original
runner, removes every known group and releases the SQL reservation using revision
checks. An unacknowledged create or unavailable runner stays uncertain and charged.
No heartbeat timeout alone proves cleanup or authorizes a retry.
Automatic orphan recovery also requires the same inspectable PID namespace and
a stable runner authority. The current implementation uses Linux boot/namespace
identity and Docker's daemon ID, including checks at normal cleanup. A Podman
or remote agent must implement the equivalent stable authority contract before
advertising support; a mutable socket or context name alone is insufficient.

Container limits must set an explicit combined memory-plus-swap amount and
verify the kernel boundary before releasing the workload. Local, persistent
container, SSH and remote execution must use the same admission contract. If a
runner cannot protect the complete tree or verify application, hold or reject
the job with a supported next action. A failed queue submission must never call
an unrestricted shell fallback. Administrative operations are fixed typed
control operations, never arbitrary shell commands labelled lightweight.

Memory-limit failure requires positive kernel/container evidence tied to this
job. Signal 9 or exit 137 alone is not proof. Preserve logs, completed artifacts,
effective policy and peak usage, clean surviving descendants, and leave goals
paused or failed according to their existing recovery policy. No automatic
larger-budget retry, enforcement bypass or goal resume is permitted.

Implementation and validation use repository source and disposable authenticated
Core/runner fixtures. No existing installation, live provider, or deployment is
required. End-to-end completion requires actual kernel enforcement plus the
selected production LAN browser and Butler journeys; mocked limits or saved
configuration do not satisfy that claim.
