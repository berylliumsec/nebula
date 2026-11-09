# Host memory controls contract

| Journey | Observable invariant | Authority | Required evidence |
| --- | --- | --- | --- |
| Recover insufficient Core reserve | Authenticated Memory budgets remains available before an API can start | Stable host and saved policy | Isolated floor-only policy, save/readback, actual API admission |
| Butler changes host budgets | The same validated policy API records its project actor and readback | Private bound peer plus host MemoryApi | Actual project Kernel and stale/conflicting requests |
| Apply running-job limits | The durable job ID selects its owning project | MemoryJobRow and project lease | Routing, conflict and original-controller integration |
| Authorization and restart | Public actor headers cannot impersonate Butler; policy and audit survive | Normal Core auth and SQL | Unauthorized/revoked/stale-owner and fresh host readback |

Existing project execution services retain their restriction against direct
global policy writes. This change supplies the authorized host Settings path;
it does not weaken job containment, administrator ceilings, revision guards or
the requirement for positive runner application evidence.

Implementation: the stable listener serves the existing MemoryApi policy,
job-read and runner-probe routes using the host budget service. Explicit
running-job application continues through durable API delivery; routing now
resolves MemoryJobRow identities to the project's current owner. The private
`host.memory-control/v1` protocol admits only budget reads, policy PUT and
runner probes from a live authenticated execution peer with its exact project
lease. Its writes and audit share that ownership transaction. The actor is
derived as `butler:<project>` and cannot come from a public header. Both paths
use the same models, administrator ceilings, revisions and read-back handlers.
Mutating host memory requests have a 128 KiB bound; accepted private operations
join cleanup even when their socket is cancelled.

Fifty-eight focused cases passed in 75.71 s on SQLite and PostgreSQL. New cases
start from a valid saved reserve equal to the administrator floor, observe API
admission held, save and read back a larger reserve through the host, then start
and promote an actual API process. Other cases cover normal authentication,
concurrent revision conflicts, actor-header impersonation, private forged and
expired grants, shared policy read-back, and durable job-ID routing conflicts.
The actual project-kernel case invokes Butler's typed action through durable
public delivery, saves a 4 GiB default, and verifies host read-back plus the
`butler:admitted-project` audit. It reports enforcement unavailable when no
runner was verified, rather than treating a save as application evidence.
Existing project-ledger isolation and global-write denial cases remain passing.

Raw receipt: `/tmp/nebula-memory-control-tests.txt`. The static Settings import
now names the canonical execution Kernel model: the compatibility module's
dynamic sys.modules alias worked at runtime but was invisible to mypy. An
independent inert identity probe established that both runtime imports reference
the same class; this correction changes no route model.

Five additional capability and actual Chroma synchronization cases passed in
8.91 s, for 63 distinct passing cases. The regenerated bundled index contains
407 passages (776,863 bytes), source digest prefix `4a6d31863ed9`; verification
passed, and host startup synchronized that digest into a disposable Chroma
collection and preserved it on service restart. Targeted mypy passed eight
changed service modules and Ruff passed all changed code. The existing Settings
destination and five typed actions retain their public names and schemas.

These are source/backend results. Browser asset/auth bootstrap while no API
worker exists still needs integration before claiming that complete Settings
recovery journey. Large Butler result handling, actual running-apply routing
with an allocator, production LAN/mobile WebKit and packaged desktop continuity
evidence also remain outstanding.
