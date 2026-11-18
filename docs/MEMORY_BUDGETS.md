# Compute memory budgets

Open **Settings → Automation → Memory budgets**, or ask Butler to open Memory
budgets. Settings are host-wide across projects and runtime versions. Values use
GiB (1 GiB = 1,073,741,824 bytes); Core stores exact integer bytes. The minimum
job ceiling is 16 MiB. The page shows administrator maxima, host capacity and the
minimum reserve for Core, SQL, indexes and essential services.

When independent runtimes are resident, Core also shows their declared reserved
memory and the current minimum Core reserve. A budget save cannot remove capacity
reserved by those processes. New runtime starts serialize with policy edits; an
expired heartbeat alone does not free their reserve. Graceful verified retirement
or a verified failed-start cleanup does. If older runtime records exceed the
saved reserve, new compute waits until you increase Core reserve or retire an
inactive runtime. Existing job limits stay unchanged. This service accounting is
separate from each compute job's kernel-enforced limits.

## Core reserve and project workers

Each project in use runs its own worker, and every resident worker (and the API
worker) holds 256 MiB of the Core reserve. A Core whose policy was never saved
sizes its default reserve from host memory: the administrator's services floor
plus a quarter of the host's capacity (its cgroup limit when Core runs in one)
for resident workers, at least 3 and at most 16 workers, and no more than the
PostgreSQL connection budget's declared processes allow. A 16 GiB host gets
2 GiB plus 16 × 256 MiB = 6 GiB; a host of 2 GiB or less keeps the earlier
default of 3 workers. Increasing the reserve leaves less for compute jobs.

A saved policy is never changed. A policy whose last change was Core's own
default (revision 1, or a recorded `policy.default_sized` change) is raised to
this size when Core starts; the audit history records it. The reserve only
grows, and never takes memory charged to compute jobs.

When the reserve is full and a project needs a worker (a sent message, queued
project work, an open project view or a storage alert), Core retires the least
recently used resident worker whose project runs nothing, has no open view and
no pending work, at once and without waiting for its idle grace. Open chats stay
saved; that project gets a worker again when it is opened or used. A worker with
running chats, goals, missions, terminals or other unresolved work is never
retired for this. If every resident worker is busy, the project shows
**Waiting for memory** with the projects holding it, and its requests fail after
3 minutes (`NEBULA_WORKER_MEMORY_WAIT_SECONDS`, 10–3600) with a message naming
those projects; nothing of them ran. Send it again when one finishes, or
increase Core reserve to run more projects at once. A request still waiting
when Core stops stays saved and runs after the restart; it never blocks
`runtime-stop`.

Set a per-job ceiling, an explicit swap allowance (zero by default), an optional
pressure threshold where a runner supports it, and a total job budget. The total
reserves each job's ceiling **plus swap** before launch. Increasing the Core
reserve reduces available job capacity. Dedicated network/loopback helpers have
an additional 64 MiB reservation and explicit zero swap; their capacity stays
charged until their removal is verified. Visible current and ancestor cgroup
limits and the runner's reported capacity constrain available host capacity.
Admission uses SQL transactions across
all workers; available-memory samples never substitute for kernel limits.

Environment defaults replace the host default. Enabled execution profiles can
override their environment default. Job-specific requests may only lower the
selected ceiling and swap, and cannot remove or raise a pressure threshold.
Administrator ceilings bound every layer. Unsupported pressure thresholds are
rejected; they are never silently ignored.

Save reads the policy back from Core with its revision. A concurrent edit returns
a conflict; keep or discard your draft after reading the new values. Refresh and
Core restart retain the saved policy and memory-failure records. Saved policy
applies to **new jobs**. Queued and running jobs retain their recorded snapshot.
Lowering the total below existing reservations reports draining and queues new
work until capacity becomes available.

## Observe and change a running worker

The Compute jobs list distinguishes the policy snapshot, verified worker limits,
current measured usage, available peak measurements, pending application and
failures. Measurements are timestamped observations; missing usage is unknown.
Charged jobs appear before queued work and finished history. If the bounded list
reaches its display limit, Settings and Butler report that more jobs may exist.
Approaching-limit status uses 80% of the observed ceiling. Click **Check
enforcement** for a profile to inspect current support; every actual launch still
requires its own kernel readback before computation is released.

**Change this running job's limits** is separate from saving policy. It uses the
job's current revision. Lowering memory, swap or a pressure threshold can kill
the job and requires explicit acknowledgement. Increases reserve the largest
possible intermediate RAM-plus-swap combination before application. Reductions
release capacity only after kernel readback. A lost or failed application stays
pending, failed or uncertain; it is never reported as applied from configuration.

`memory_limit_exceeded` requires positive kernel/container OOM evidence for the
identified resource group. Exit 137 or signal 9 alone is insufficient. Logs and
completed artifacts remain in the original execution. Review them, change the
workload or budget, and retry explicitly. Nebula never increases a budget,
retries work or resumes a goal automatically after a memory failure. Reservations
remain charged after worker loss until trusted cleanup confirms the group gone.

Core records the launch process's kernel birth identity, a fingerprint of the
owning runner, and immutable workload/helper container IDs before releasing
computation. Its recovery loop leaves live owners alone. After a verified owner
death it removes the recorded groups, verifies cleanup, and only then releases
capacity. It does not replay the job. A changed or unreachable runner, or a
crash before group creation was acknowledged, leaves capacity charged with an
uncertain outcome until trusted reconciliation can establish cleanup.
Automatic recovery currently requires a comparable Linux PID namespace and the
original Docker daemon ID. Foreign or hidden process namespaces and runners
without a stable authority identity require a trusted runner-agent integration;
they never become cleanup proof merely because a PID or container is absent.
The current managed OCI path requires Docker's stable authority ID. Podman
requires a certified memory-runner agent before it can advertise this support.

## Butler actions

Butler discovers the typed actions `nebula.settings.memory_budget`,
`memory_usage`, `set_memory_budget`, `verify_memory_job` and `apply_memory_limit`
through its normal capabilities search and describe tools. These actions use
the same authenticated `/api/v1/memory-budgets` routes as Settings. No manual
configuration-file edit is required. A policy update includes the observed
`expected_revision` and preserves unrelated fields. Audit history records the
trusted actor, scope, previous and proposed values and application result.

Requests such as “Limit each worker to 4 GiB,” “Reduce the total worker budget to
12 GiB,” and “Show which jobs are approaching their limits” use those actions.
Butler reports saved settings separately from verified worker application and
reads the job again after a running-limit action.

## Enforcement and remaining runner dependencies

Dedicated OCI jobs verify cgroup-v2 memory.max, explicit memory.swap.max,
read-only cgroup controls, dropped capabilities and no-new-privileges before
receiving a release signal. Descendants share the container's group. The current
OCI adapter supports explicit running RAM/swap changes, and reports memory.high
as unavailable. Startup creation and workload release are separate, so a lost
startup response never authorizes unrestricted computation.

Managed computation uses the unprivileged container identity. Owner-mapped
read-only tools, including the current debugger adapter, are held until an
adapter can exclude host data and control sockets from exported filesystems.
A read-only bind does not prevent communication with a Unix socket. Injecting
an older PDF pool likewise cannot bypass the protected execution boundary.

An automation or Docker Notebook session is a persistent **worker job**: all its
commands, background children and Notebook kernels share its frozen worker
budget. Capacity remains reserved until that worker and every descendant are
removed. New commands inside it do not create a fresh worker or reset its budget.

The local search service is one persistent `web_search_service` job in the total
job budget. Queries share its existing reservation; they add no per-query charge.
Its selected profile/default may lower the service's 512 MiB RAM ceiling; swap
is explicitly zero. It receives the same pre-launch kernel proof, live-limit
controls and verified cleanup as other managed jobs. It has no additional
search-specific runtime/Core-reserve charge; the administrator's Core/SQL floor
is unchanged. Stop, idle retirement or host shutdown releases capacity only after
the identified group is gone. An idle project worker retires 5 minutes after its
project's last request (`NEBULA_IDLE_WORKER_GRACE_SECONDS`), or at once while
another project waits for a worker Core could not place. Changing saved policy applies on the next service
start; a running-limit change remains a separate explicit action.

Search startup can use a verified cached official image. An uncached search image
or workstation image requiring download/build is held: a daemon worker executes
that work, so restricting its client would not enforce the worker's memory.
Support requires a runner that owns and verifies the daemon/build-worker groups
before starting computation. Fixed inspection and fully verified cached-image
reuse remain available. No provisioning failure falls back to unrestricted
Docker, BuildKit or a host shell.

Host commands use the protected local memory runner described below. Host
Notebook kernels still need that runner integration; PDF CPU work needs a
contained CPU runner; raw SSH needs an authenticated remote agent that can
verify both kernel application and descendant cleanup; interactive terminals
need a memory-managed PTY adapter. Unsupported paths are
held or rejected with a durable explanation. They cannot fall back to a host
thread, raw shell or SSH after queue failure. These integrations require explicit
runner support and environment-specific validation before they are supported.
Native vendor harnesses may use their bounded vendor adapter lifecycle without
a Nebula memory-runner limit; adapters with a managed opener continue using it.
Essential control operations are fixed internal actions such as runner info,
image inspection, kernel readback and removal; arbitrary commands cannot acquire
an administrative exemption by describing themselves as lightweight.

Validation uses small allocation workloads in an explicitly selected disposable
OCI daemon and isolated authenticated Core fixtures. A fixture's daemon health
does not certify production rootless isolation. Deployment and existing
installations are outside this change's validation scope.

## Protected local runner for Host commands

On Linux, Host-mode commands run through `nebula-memory-runner@<user>`, a root
system service shipped in the Nebula DEB
(`/usr/lib/nebula/memory-runner/host_memory_runner.py`). Enable it once for the
account that runs Core:

```sh
sudo systemctl enable --now nebula-memory-runner@$USER
```

Core finds it at `/run/nebula-memory-runner/<user>.sock` (override with
`NEBULA_HOST_MEMORY_RUNNER_SOCKET`). Without a running runner, Host commands stay
held with that instruction; Settings reports the `local` environment as
unsupported. macOS has no equivalent kernel boundary, so Host commands remain
held there.

Each Host command session is one worker job. The runner creates a root-owned
cgroup-v2 group for it under the unit's delegated subtree, writes `memory.max`,
`memory.swap.max` and `memory.high`, and reports the kernel readback before any
process exists. Every command then starts inside that group as Core's OS user:

- The process is moved into the group before it runs anything but a fixed
  root-owned stage script, so every descendant, including `setsid`/`nohup`
  children, is charged to the job and removed with it.
- The workload has no capabilities, an empty bounding set, cleared
  supplementary groups and `no_new_privs`, so `sudo`, other setuid helpers and
  the `docker` group cannot leave the group. Root owns the group directories,
  so the workload cannot write `cgroup.procs` elsewhere.
- A private mount namespace hides the runner socket, `/run/user/<uid>` (the
  user's systemd and D-Bus sockets) and container daemon sockets. The runner
  also refuses any client process that is inside a job group.
- Usage, peak and the kernel `oom_kill` count are sampled from the group.
  A positive `oom_kill` count ends the worker with `memory_limit_exceeded`; the
  next command starts a new worker with the same saved budget.
- Running-limit changes are written to the group and read back.
- Cleanup writes `cgroup.kill`, waits for `populated 0` and removes the group
  before Core releases capacity. If Core dies, the reconciler asks the same
  runner (checked by its persistent instance authority) to remove the group,
  whose name is derived from the job identity.

Remaining boundary: Host commands still run as Core's OS user with its
filesystem and network access. The runner prevents them from leaving the
memory group through the kernel, setuid programs or local service sockets. It
does not stop a command from asking a network service, for example `ssh
localhost` with the user's own keys, to run work outside Nebula.
