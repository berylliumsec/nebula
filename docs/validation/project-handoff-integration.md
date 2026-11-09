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

Automatic controller inventory, physical retained-process handoff orchestration,
full resume/approval/goal restoration and crash recovery still need integration.
The table above describes the full contract, not evidence that those remaining
steps have passed.
