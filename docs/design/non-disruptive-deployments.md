# Non-disruptive deployments: staged implementation

The target is independently supervised host, API and execution processes. A
routine API/UI update may reconnect viewers, but must preserve accepted commands,
conversation identity and running work. SQLite and PostgreSQL remain supported.

## Acceptance contract

| Journey | Invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Submit/retry | Same actor/key/intent has one immutable receipt; changed intent conflicts | SQL command ledger | Concurrent independent connections, lost-response retry |
| Execute | One project owner; stale or expired owners cannot commit | SQL fencing token and database clock | Competing claims, lease expiry during a write |
| Stream/reconnect | Persist before publishing; cursor continues across processes and owners | Existing append-only run-event ledger | Cross-connection replay and owner replacement |
| Unknown outcome | Claimed work abandoned by an owner is never automatically replayed | Command outcome plus existing effect receipts | Expired claimed command blocks later work |
| Update/handoff | Finish admitted work, transfer only restorable projects, retain pinned work | Supervisor and project ownership | Active synthetic work, resources and fault injection |
| Close/reopen desktop | Work continues; selected chat and scoped unsent draft return | Supervisor, SQL, URL and device storage | Packaged update/reopen and production LAN browser journeys |
| Stop/revoke | Operator stop and authorization remain effective across owners | Authenticated command boundary | Real-Core stop and revocation journeys |

## Review sequence

1. **Durable foundation.** Add runtime identities, project fencing and ordered
   idempotent commands. Reuse the run-event ledger for stream batches, including
   its existing immutable history and owner-based retention. No public routes,
   process launchers, providers, schedulers or desktop behavior change in this
   first change. It does **not** make existing Core processes safe to overlap.
2. **Execution extraction.** Move producers, recovery, schedulers, harnesses and
   every execution callback/controller out of API lifecycle ownership. Route all
   execution state writes through project fencing and all retryable mutations
   through the command ledger. Keep auth, actor identity and revision checks at
   the authenticated entry point. Dispatch must atomically persist turn creation
   and command completion; a receipt alone does not prove exactly-once execution.
   The supervisor owns shared memory/index services and host capacity accounting.
3. **Handoff.** Persist requested → finishing → transferable → transferred →
   verified. Queue new turns while finishing current work; preserve approvals,
   schedules, callbacks, parent/child state and supported vendor sessions. Resource
   inventories must prove transferability. Unknown effects and non-restorable
   resources pin the project indefinitely. Limit resident execution versions to
   two; stage a third without killing either resident version.
4. **Desktop and clients.** Versioned installed bundles outlive the replaceable
   desktop. Authenticated supervisor discovery replaces the desktop lifetime
   pipe. Add explicit coordinated shutdown, durable stream reconnect, scoped
   draft storage, and updated Settings/Butler/product knowledge guidance.
5. **Promotion and rollback.** Keep the public listener available, validate
   protocol/schema/capacity and candidate health, then switch API traffic and
   request handoffs. Only the supervisor migrates. Rollback routes code to a
   retained compatible version; it never restores old data over continuing work.
   Record resident versions, transferred/pinned projects, blockers and cleanup.

Each step needs an independent review and its own diff-bound test receipt before
the next rollout boundary. The first transition from the current architecture
still needs one coordinated shutdown. No live existing Python task can be
retrofitted into the new process model.

Additive SQL is not sufficient for legacy-process compatibility. Current
launchers run Alembic at startup, and a retained older launcher cannot resolve
this newer migration revision. Before enabling overlapping versions or code
rollback on one live database, implement supervisor-only migration ownership and
explicit worker schema compatibility validation. This foundation does not
claim that legacy launchers can roll back against an upgraded database.

## Foundation rules

`RuntimeCoordination` is an internal storage boundary, not an authenticated API.
Its callers must authenticate and authorize the actor/project before submission
or replay. Runtime IDs identify process incarnations and must be randomly unique.
An expired ID cannot be revived. Workers must heartbeat every five seconds;
leases expire after thirty seconds according to the database clock. Registration
and heartbeat primitives do not start a heartbeat loop on behalf of a worker.

Project counters never reset. Claiming requires expiry or no owner. Every
authoritative mutation must use `fenced`, which acquires the project write lock
and checks both leases before and after the transaction. Keep these transactions
short and free of external I/O. The dispatcher must check fencing before admission
of an external operation and preserve existing uncertainty handling if ownership
is lost after admission. A SQL fence cannot revoke a request already sent to a
vendor. Heartbeats cannot resurrect expired ownership.

Submission hashes include project, command, canonical payload and expected
revision. Actor/key uniqueness spans projects and operations. Repeated receipts
do not contain credentials, payloads or mutable execution outcomes. Sequence gaps
from concurrent retries are allowed; dispatch order is strictly increasing.
An expired running command becomes uncertain and blocks later commands until an
explicit, separately authorized recovery resolves its outcome. Generic automatic
retry and automatic marking-success are intentionally absent.

Stream batches use `chat_stream.*` run events and deterministic per-batch
idempotency keys. Sequences share the existing run ledger and may have gaps from
other event types. The durable epoch remains stable across worker generations.
Replay pages are bounded to 256 frames. History lives as long as its owning turn,
using the existing deletion guards and batched purge; no new time-based eviction
discards acknowledged output. Internal replay must only be exposed after normal
project/turn authorization, and the public adapter must encode the stored cursor
without renumbering filtered events.

## Required rollout evidence still pending

The storage tests are not a continuity claim. The later changes must exercise
the request's API replacement, active execution handoff, non-transferable
projects, uncertain effects, supervisor restart, crashes at every handoff phase,
failed migrations, code rollback and third-version scenarios. Run the selected
production bundle journeys against a disposable authenticated non-loopback LAN
Core in desktop Chromium, mobile Chromium and mobile WebKit, plus packaged
desktop update/reopen. Until those gates pass, keep the new architecture disabled
and do not describe deployment continuity as complete.
