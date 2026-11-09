# Execution handoff integration contract

| Journey | Observable invariant | Authority | Planned evidence |
| --- | --- | --- | --- |
| Request update during work | Admitted work finishes on its original runtime | Frozen admission inventory and SQL lease | Inert retained-process provider barrier |
| Submit while draining | Intent remains queued without operator pause or cancellation | Command and provider queues | SQLite/PostgreSQL state readback |
| Finish a parent | Its required descendants can still admit; later roots wait | Durable child links and atomic admission check | Parent/child admission fixture |
| Move queued work | Same turn restores on the new owner, with a larger fence | Five-phase coordinator and queued-turn restoration | Actual source/target processes |
| Preserve approvals and schedules | Durable state and identity survive | SQL revision checkpoint | Checkpoint digest and target verification |
| Keep a resource-bound project | Concrete blocker; no kill deadline | Resource inventory | Pinned and transferable projects together |
| Restart orchestration | A committed phase resumes idempotently | SQL phase and retained process identity | Supervisor reconstruction/crash fixtures |

This contract concerns repository implementation and isolated synthetic fixtures.
It does not authorize an operational deployment. Required production LAN/mobile
and packaged desktop evidence remains a separate completion gate.

## Admission and queued producer evidence

Migration `0041_handoff_admission` stores the exact running-provider inventory
when the request takes the project ownership lock. Provider admission checks
the handoff in the transaction that changes its queue to running, under that
same lock. Bounded durable child links permit descendants of admitted work;
new unrelated roots wait and release acquired host capacity. A pinned project
remains usable on the old version. When its blocker clears, re-draining freezes
the work admitted during that interval as well.

`ChatService.park_queued_for_handoff` joins unadmitted producer cancellation as
an internal detach. It preserves the queued turn and admission, emits no
operator pause/cancellation, and closes its journal resource even if the task
never entered its first step. A target can restore the same durable turn using
the existing queued recovery path and wait for verification before admission.

Thirty distinct focused cases passed: 29 in 17.44 s, followed by one existing
cancellation fixture in 2.22 s after independent causal validation synchronized
its wait-explanation write. The eight new cases cover both databases, including
an admission transaction racing handoff, child admission, host-capacity release,+pinned/re-draining inventory and an actual ChatService queued-turn transfer.
The transfer fixture retains one turn/session, no pause/cancel frames, and one
provider execution on the target. Other selected cases cover cancellation
compensation, legacy resume states, host-wide schedulers and prestart stop.
Ruff and targeted mypy passed. Raw outputs:
`/tmp/nebula-handoff-admission-tests.txt` and
`/tmp/nebula-handoff-admission-rerun-tests.txt`.

## Automatic retained-process handoff evidence

The optional `project.handoff/v1` feature now connects the source worker and host
supervisor. A source checks its real controller handle maps, durable process
states, charged compute jobs, active provider segments and host capacity. Idle
vendor connections, terminals, kernels and other handles without restoration
adapters pin the project with an explicit reason. Missing participant contracts
and oversized inventory/checkpoints also retain a usable old project. No timer
kills pinned work. Running provider segments drain with admission held; they do
not require a candidate adapter because they finish on their original worker.

The source checkpoints project entity revisions and provider queue state. SQL
payloads and credentials are excluded. Transfer rechecks that proof, active
resources, unfinished commands and held host capacity in the ownership
transaction. A target validates the same checkpoint before constructing its
controllers and reports its restoration digest. Queued creation, routing and
finalization intents restore without orphan-interruption semantics, and queued
child rounds retain their parent links. The host releases work only after
matching target verification. A failed target does not prevent the host from
checking other projects.

The checkpoint currently bounds project rows and queue rows at 10,000 each.
Projects exceeding that bound remain on their current version with an explicit
blocker until a paged checkpoint adapter is available. Inventory bounds are 100
resident resource explanations per project. These are explicit transfer limits,
not reasons to stop existing work.

Sixty-five distinct cases passed: 63 in the initial 111.65 s run, followed by the
two retained-process cases in 50.95 s after an independently validated fixture
repair. The fixture initially installed its inert provider after lifespan startup
had captured the real localhost adapter for restored work. Configuring the
kernel before its lifespan corrected that fixture; product code did not change
after the initial run. An earlier independent source review also established
that treating an active provider as an indefinitely pinned unsupported resource
prematurely reopened admission; the focused regression now keeps that resource
finishing while new roots wait.

The process journey runs the actual kernel through retained source wrappers and
normal authenticated public chat routes on both SQLite and PostgreSQL. One chat
streams `First ` on v1 and waits; a second turn is accepted but unadmitted. A
separate project has an inert terminal resource without a restoration adapter.
Both request handoff, and a third message is accepted during draining. Releasing
the first producer lets the transferable project reach verified ownership on
v2. The existing queued turn and newly accepted message both complete there,
with provider starts exactly `[v1, v2, v2]`, three total turns and no pause or
cancel frames. The pinned project retains its v1 PID and open admission. The
emptied source worker retires with verified process disappearance.

Additional cases cover resource closure, missing adapters, revision changes
before transfer and target restoration, late capacity reservations, all durable
handoff phases on supervisor reconstruction, and permanent worker grant/fencing
behavior. Raw results: `/tmp/nebula-handoff-agent-tests.txt` and
`/tmp/nebula-handoff-agent-process-rerun-tests.txt`. Ruff and targeted mypy pass.

This proves the selected source/backend journeys using inert model and terminal
fixtures. It does not prove physical terminal/vendor restoration, complete
approval/schedule/callback/goal journeys, orchestration crashes in every phase,
expired-owner recovery, or packaged cross-version compatibility. Those cases,
client reconnect/drafts, launchers/deployer and the required production LAN,
mobile WebKit and packaged desktop continuity gates remain outstanding.

## Durable participant restoration

The optional `project.participants/v1` execution capability covers saved approval
and callback waits, schedules, goals and child rounds. A project containing those
records requires support on both execution versions before transfer; compatible
API forwarding does not require that execution capability. Missing callback
results, unconfirmed approval delivery and unresolved execution claims retain
their current owner with explicit blockers.

The target prepares controllers without entering the application lifespan.
`execution.activate/v1` starts it only after durable checkpoint verification.
The handoff startup path restores existing accepted turns, materializes received
callback results once, preserves approval receipts and prior goal pauses, keeps
waiting child links/rounds, and fires due schedules using the existing occurrence
ledger. Unsupported native/vendor handles remain pinned.

The focused participant fixtures exercise real ChatService and SQL state on
SQLite and PostgreSQL with inert providers. They verify one scheduled occurrence,
pending/acknowledged approval receipts, paused/running goals, a disabled schedule,
waiting children, same-turn callback continuation, unknown-outcome pins, and
explicit legacy execution rejection while a compatible API still promotes.
The first fixture run revealed a reused global entity ID and an assertion before
the separate schedule-outcome task committed. Independent causal review confirmed
both fixture faults; unique IDs and authoritative outcome polling repaired them.
The 18 distinct participant cases pass: 16 in the capability run and the two
API compatibility cases after the same validated ID-collision fixture repair.
Raw results are in `/tmp/nebula-handoff-participants-feature.txt`,
`/tmp/nebula-handoff-participants-rerun.txt`, and
`/tmp/nebula-participant-api-feature.txt`.

Eight additional inert-process reconstruction cases passed on both databases.
They retire only never-spawned or positively absent unowned workers, preserve
owned dead workers, and retain a live worker whose control socket disappeared.
Results: `/tmp/nebula-process-reconciliation.txt`. These fixtures do not replace
the packaged, production LAN/mobile, or physical vendor-restoration rollout gates.

## Coordinated idle shutdown

`project.quiesce/v1` permits an already-used, idle project to stop without an
operator Stop being substituted for pause. The host holds new command, provider
and ownership admissions, while existing grants remain valid for controller
cleanup. A worker verifies local handles and durable queues, goals, schedules,
effects and charged capacity, then joins cleanup. Ownership releases only after
the worker confirms cleanup and SQL rechecks both idleness and the fingerprint
of prior paused work and disabled schedules. The project fencing counter stays
monotonic for its next owner.

Active work refuses shutdown. A signed preflight refusal leaves the worker ready
and reopens admission so its blocker can be resolved; an unknown RPC outcome or
unverified cleanup retains the hold and ownership. The final process-retirement
step follows only after every project relinquishes its grant.

Forty focused cases passed across SQLite/PostgreSQL for quiescence, shutdown
admission, operation receipts and source boundaries. They include a previously
used idle project, paused-state preservation, cleanup failure, local-handle
refusal, a running provider admission missing its resource row, and concurrent
command submission with or without an existing host row. An independently
verified fixture repair removed an unsupported `ChatTurn.completed_at` argument.
Raw output: `/tmp/nebula-shutdown-boundaries.txt`.
