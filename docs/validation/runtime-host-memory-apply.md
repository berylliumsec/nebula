# Host-managed running memory updates

The stable host accepts explicit running-limit updates for jobs whose immutable
`project_id` is null. It uses the canonical host compute controller and its exact
per-job runner binding. Project-owned jobs continue through the existing API
proxy and execution controller. The handler never discovers a replacement runner
or creates a global project owner.

| Journey | Invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Read and apply | The current revision and lowering acknowledgement remain required | Canonical memory ledger and exact kernel group | Authenticated ASGI and actual cgroup readback |
| Retry or reconnect | The same actor/key returns its original response without another update | Encrypted host effect receipt | Replay and disconnected viewer |
| Lost kernel reply | Unknown limits retain the maximum charge and prevent unsafe replay | Memory job uncertainty | Synthetic acknowledgement loss |
| Missing owner | No profile/default runner substitutes for the admitted binding | Exact host controller job map | Refusal and controller identity checks |
| Butler action | Apply and before/after readback use the same host state as the UI | Signed exact-grant Settings bridge | Real private listener and canonical job read |

The existing Memory API validation and HTTP error translation are shared by the
host effect handler. Host admission retains its bounded body, normal Core
authentication, Idempotency-Key, shutdown fence, and actor-scoped receipt lookup.
The old direct host Memory API does not expose a separate non-durable apply
route. The assistant's existing action allowlist and acknowledgement contract
remain unchanged.

Selection: eight SQLite/PostgreSQL synthetic backend cases and one actual
kernel case in `tests/v3/test_runtime_host_memory_apply.py`. The latter starts
an inert managed service in the explicitly assigned disposable Docker daemon
with its cached pinned image, reads `memory.max` and `memory.swap.max` in that
exact group after the public update, then verifies group removal and zero
reservation. It does not run SearxNG, fetch an image, or call upstream engines.

Nine distinct cases passed: four public ownership/guard/replay cases, two
disconnect/uncertainty cases, two signed assistant cases, and one physical case.
The final assistant pair passed in 8.39 seconds and the physical case in 5.11
seconds. Ruff and focused mypy on the three changed source files pass.

The actual service job was `search-4528b145da9e40e794c74f152cb96d8a`, group
`b2fd280a8e933bf6ea1970409140cb531a11830185e7c9f8a1d8b8aafc0a2cc5`.
Its public update changed 33,554,432 bytes to 16,777,216 bytes, with actual
`memory.max=16777216` and `memory.swap.max=0`. Cleanup read back `finished`,
`group_empty=true`, and zero reserved bytes; inspection confirmed that exact
group was absent. The daemon was `unix:///var/run/docker.sock`, using
`/usr/local/bin/docker` and cached image
`postgres@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139`.
The fixture's runner certification does not certify production rootless
isolation.

Fresh independent reviews resolved three test assumptions while retaining the
production guards. Changed physical limits correctly refuse with 503 before a
non-running-state conflict, and keep the uncertain charge. The assistant's inert
resident worker requires a real reserve/aggregate policy save before compute
admission; its operator audit remains alongside the Butler job audits. Existing
SQLite timestamp representation differs on reload, so the full-state assertion
compares exact UTC instants and microseconds without discarding any field.

Raw receipts are `/tmp/nebula-host-memory-apply-tests.txt` (first four passes),
`/tmp/nebula-host-memory-apply-rerun.txt` (two uncertainty passes),
`/tmp/nebula-host-memory-apply-assistant-final.txt` (two assistant passes),
`/tmp/nebula-host-memory-apply-kernel.txt` (actual cgroup proof), and
`/tmp/nebula-host-memory-apply-mypy.txt`. No interface controls change; final
combined UI/browser/LAN acceptance belongs to the parent integration track.
