# Rolling Core releases without interrupting conversations

Status: design plus a partial prototype, November 14, 2026, on `main` at
`fea7796`. The prototype covers host-generation replacement and the handoff fix
described below. It was rehearsed on a disposable scratch Core with PostgreSQL
and a slow streaming stub model. Nothing here has run against the live Core.
The deployer and unit changes it needs are listed under
[Remaining work](#remaining-work).

## Goal and answer

The goal is that a new Core release reaches every project while conversations
keep running, with no Butler pause/resume ritual.

**Mechanism.** The supervisor sequence that already exists is: stage → migrate →
replace the supervisor → API blue/green → target → per-project handoff → retire.
This design keeps that sequence and makes every step safe to run under live work.

**Status of the three missing pieces:**
- Done in this change:
  - The public listener is held outside the supervisor.
  - Workers wait for the host while it is replaced.
  - The handoff inventory now sees follow-ups that are being dispatched.
- Done since: an in-Core roller (see [In-Core roller](#in-core-roller)).

**What still needs a pause.** Nothing is interrupted for provider conversations,
including:
- streaming turns;
- queued turns and follow-ups;
- goals;
- approvals and schedules;
- provider subagents.

Three cases still need an operator decision:

1. **Pinned projects.** These hold a busy vendor (harness) connection: a turn
   active, connecting or reserved, or a goal dispatch preparing one. They also
   include projects with a terminal, a notebook, a debug or Docker automation
   session, a busy Host automation session, a running mission or execution
   task, an unconfirmed callback or approval delivery, or a charged compute
   job. Three idle kinds move instead:
   - The source closes an idle Host automation session at the handoff
     boundary: its finished results are durable, and the owner's next command
     opens a fresh session on the target.
   - The source detaches an idle vendor connection at the boundary. The
     session row keeps its saved vendor thread (`external_session_id`), and
     the conversation's next turn on the target resumes that thread with a
     fresh vendor process, the same path as a vendor process that dropped
     between turns. A turn sent during the detach waits for it and resumes
     the same thread instead of forking. The adapter must also report that
     the vendor runs nothing of its own (`handoff_detachable`):
     - Claude: no open background task, no unread output and no queued
       guidance.
     - Codex: native shell off, since unified exec can leave background
       terminals in the app-server; no goal turn pending.
     - Grok: the runtime advertises `loadSession`.
     Otherwise the connection stays and pins. Queued chat or analysis harness
     turns also pin until a task picks them up.
   - A scheduled Mission whose timer is still sleeping (native or harness)
     does not pin: its queued run is durable, and the target's startup re-arms
     it from SQL. Every source transaction is fenced after transfer, so a
     source timer that wakes late cannot start the run; it starts once, on
     the target.
   - Their work is never interrupted; the project simply stays on the old
     version.
   - A pause is needed only when that old version must go. That happens when a
     third release arrives while two execution versions are resident.
   - It is a per-project wind-down, not a Core-wide one.
2. **Non-additive schema changes.** Use expand/contract over three releases, or
   a coordinated whole-Core stop.
3. **The first rollout of this mechanism.** The running code still behaves as in
   E1 below. The socket unit and the listener-holding code must already be in
   place before a deploy can benefit from them.

## How deploys work today

### Release staging and activation

- **What a release is.** An immutable, hash-verified bundle under
  `runtime/releases/<version>` (`release_store.py`, `runtime_bundle.py`). Its
  manifest declares:
  - protocol and schema ranges;
  - `additive_migrations`;
  - features such as `project.handoff/v1` and `project.participants/v1`.
- **How it is staged.** Through `POST /api/v1/deployment/operations`
  (`runtime_management.py`). Each operation is idempotent and journaled in a
  receipt ledger.
- **Migrations.** The supervisor runs the candidate's own `runtime-migrate`,
  only when `additive_migrations` is declared (`deployments.py:523`). Workers
  never migrate.
- **Version targets.** `target_execution` sets the version that new admissions
  and dead-owner recovery use (`runtime_auto_admission.py:92`).
  `resident_versions` lists the versions of live workers. At most two execution
  versions may be resident (`MAX_EXECUTION_VERSIONS`).
- **Live deploy path.** In supervisor mode, `nebula_deployer.py promote` runs
  `nebula_staged_deployer.py`:
  1. stage and migrate;
  2. `activate_host`: `systemctl stop` the supervisor unit only, with
     `KillMode=process`, swap `current`, start it again;
  3. `prepare_api` and `promote_api`;
  4. `target_execution`;
  5. move projects one at a time: `prepare_execution` → `handoff` → verify the
     new owner and fencing token → `retire` the source.
- **Legacy mode** stops the whole Core and takes a snapshot. Its gate
  (`nebula_deploy_gate.py`) refuses while any chat is pending. That refusal is
  the reason for the AGENTS.md Butler ritual.

### Execution workers and handoff

- **Workers.** Each project has exactly one worker, with a permanent grant:
  - It is spawned from its release bundle's own executable, with a sanitized
    environment and its own session.
  - Lease 30 s, heartbeat 5 s.
  - Every SQL transaction is fenced by the project's monotonic token
    (`execution_scope.py`).
- **Retirement.**
  - Auto-admission retires workers whose project has no open chat.
  - `retire` waits for the worker to exit and never kills it.
  - Crashed owners are recovered after their lease expires, at most 3 times per
    10 minutes.
- **Handoff phases.** `requested → finishing ⇄ transferable → transferred →
  verified` (`runtime_project_handoff.py`, `deployments.py:1167-1554`):
  - The source holds admission of new root turns and lets admitted provider
    turns finish.
  - It parks queued turns.
  - It checkpoints project revisions.
  - Transfer re-checks that digest under the ownership lock.
  - The target restores without entering its lifespan. The supervisor verifies
    the digest, then activates it.

### Per kind of work

| Work | Handoff today | Full Core restart today |
| --- | --- | --- |
| Streaming provider turn | Finishes on the source; new roots wait | Interrupted (`core_shutdown`), re-requested from the last saved step: partial text lost, second provider call |
| Queued provider admission | Parked, restored on target | Re-queued |
| Chat follow-up queue | Dispatched turns are parked; **a claim in flight was lost (E3, fixed here)** | Undispatched items survive; a mid-dispatch item goes to review |
| Harness turn | Pins the project | Interrupted, not resumed |
| Idle harness session | Detached; the target's next turn resumes the vendor thread | Resumed on the next turn |
| Goal | Continues after its turn transfers | Paused and resumed with its turn (provider); harness goals stay paused |
| Mission | Running mission pins; a sleeping schedule is re-armed on the target | Resumes from checkpoint; schedules re-armed |
| Approval awaiting operator | Transfers; unconfirmed delivery pins | Provider approvals survive; harness approvals are lost |
| Background-command callback | Pins until the result arrives | Settles as `missing_callback`, effect unknown |
| Schedule | Transfers; due occurrences fire once | Fires once after restart |
| Provider subagent | Child links restore | Restored if the child turn is recoverable |

## Rehearsal evidence

**Setup.**
- Scratch Core on PostgreSQL, with an execution worker per project.
- Stub OpenAI-compatible model: 40 chunks × 500 ms, so each reply streams for
  about 21 s.
- Each run opens a fresh chat per project with a marked message, then queues two
  marked follow-ups through the durable chat queue while the reply streams.
- The verifier reads SQL and the stub's request log. Per marker it checks:
  exactly 1 user message, 1 provider request, 1 completed turn, no stream error
  frame, and every queue item `complete`.
- Every run used a local three-line fix for an unrelated defect: since #939,
  `chat_queue.py` called async `start_provider_turn`, `prepare_chat` and
  `start_chat_turn` without `await`, so every follow-up was silently dropped.
  `main` fixed it independently in #1017.

| Run | Disruption | Result |
| --- | --- | --- |
| Baseline | none | 0 violations |
| E1 | SIGTERM the supervisor only, start a new one | See E1 below |
| E2 | SIGTERM every Core process (legacy stop), start | See E2 below |
| E3 | Existing in-Core roll: stage, migrate, API, target, 4 serial handoffs (76 s) | 8 of 9 messages perfect; 1 follow-up lost |
| E4 | Prototype: the full deploy behind a held listener | 9/9 messages perfect, 0 refused connections |
| E5 | Prototype deploy started while admission was held, including a cold chat start | 0 violations |
| E6 | Handoff of a project whose cold `http.invoke/v1` chat start was still queued | 0 violations; turn restored and admitted on the target |

**E1** (supervisor only):
- All 3 streaming turns finished on their workers. Uvicorn kept the old process
  alive for 15 s to relay the open streams.
- The API was unavailable for 84 s. `serve` probe-binds the port without
  `SO_REUSEADDR` and failed for about 60 s on TIME_WAIT sockets.
- **6 of 6 follow-ups were lost to review.** Their worker's browser-bridge and
  host-pressure calls hit a missing host socket or a 503.

**E2** (whole Core):
- 3 of 3 streams showed `chat_stream_failed`.
- 3 of 3 first turns were re-requested from the provider (2 requests each).
- The API was unavailable for 85 s.
- Follow-ups survived only because none had been dispatched yet.

**E3** (existing roll): the lost follow-up was claimed by the source dispatcher
just before the transfer.

## Design panel

Three independent designs were produced from the same fact sheet.

1. **Rolling per-project handoff.**
   - Keep the existing sequence.
   - Make the supervisor swap a short wait instead of a failure.
   - Move the serial loop into Core as a reconciler.
   - Fix the queue claim.
2. **Ingress blue/green.**
   - A never-restarted launcher holds both the public TCP socket and the
     host-services socket across supervisor generations.
   - The old generation releases host authority without closing them.
   - Worker code is still upgraded by handoff.
3. **Drain and resume at turn boundaries.**
   - Park every conversation at its next durable boundary, record exactly what
     to resume, replace Core, then resume exactly that.
   - This automates the Butler ritual. It works for any release, but costs a
     restart window, a second provider request per resumed turn, and new
     boundary checks in every producer.

**Verdict.** The panel agreed on the following.
- **(1) is the mechanism.** E3 shows it already moves provider work with zero
  provider re-requests.
- **(2) is what makes step 2 safe**, the supervisor replacement. The smallest
  version holds only the public socket (systemd socket activation) and lets
  workers wait out the host-services gap. A dedicated launcher that also holds
  the host-services socket is the follow-up.
- **(3) is reserved for releases that cannot roll**: non-additive schema,
  protocol breaks, and the first transition.

## The design

1. **Stage and migrate** (unchanged).
   - Additive migrations only.
   - Also required before two versions may overlap in practice: tolerant
     decoding of entity payloads. Entities use `extra="forbid"`, so before
     `storage.tolerant-entities/v1` a field written by release N+1 made
     release N raise `CorruptRecordError` on the same row. That broke old
     workers and code rollback alike. See
     [Tolerant entity readers](#tolerant-entity-readers).
   - Release N+1 may write a new field only after every resident and retained
     rollback version tolerates unknown keys. The coordinator enforces this.
2. **Replace the supervisor generation.** *(prototype)*
   - **The listening socket is owned by a holder that never restarts.** That
     is the systemd `nebula-core.socket` in production.
   - **How `serve` adopts the socket.** Through `LISTEN_FDS`/`LISTEN_PID`
     when systemd names the Core process itself. Otherwise through an explicit
     `--listen-fd N` when a launcher sits in between: the live unit's
     `mercury-run` wrapper and the PyInstaller onefile bootloader make
     `LISTEN_PID` name a parent.
   - **What adoption checks.** The fd must be a listening TCP socket bound to
     exactly the requested `--host` (which must be an IP literal) and
     `--port`, so a wildcard socket can never pass as loopback. The fd is
     marked non-inheritable, and the `LISTEN_*` variables are cleared.
   - **Effect.** Connections that arrive while one generation exits and the
     next starts wait in the kernel accept queue instead of being refused.
   - **While a holder keeps the socket, the old generation drains its relayed
     exchanges.**
     - An exchange that already answered (an open viewer stream) is closed
       after `VIEWER_DRAIN_SECONDS` (5 s); the viewer resumes from its cursor.
     - An exchange still waiting for its response headers is never closed
       before it answers, up to 30 s. The API worker answers an accepted
       request with its saved receipt within 15 s, so a chat start or work-lane
       mutation in flight at SIGTERM gets its receipt, not a 500.
     - Once every relayed exchange has finished or been closed, a connection
       may still hold bytes its client stopped reading (a phone that left the
       network, a paused download). It gets 5 s to flush and is then aborted,
       so a stalled reader cannot keep the old generation and its host lock
       alive. Uvicorn's graceful timeout is set to 45 s (`relay_stop_bound()`: viewer drain 5 s + relay answer limit 30 s + two 5 s flushes) as a backstop, so a
       holder-backed stop always ends.
     - Without a holder (a self-bound listener) the old generation relays
       every exchange to completion, as before: closing early would gain
       nothing, because nothing queues the next connection.
     - Today the drain is unbounded: the old process keeps the host lock until
       the longest stream ends.
   - **Deployment operations survive the drain.** Each operation runs in a
     shielded task that records its receipt even if its request is cancelled.
     A cancelled request answers 503 with the operation ID and `Retry-After`;
     the deployer's `deployment_post` and the rehearsal's `roll.py` honour it
     and resubmit the same operation ID, which returns the saved receipt. The supervisor joins those tasks
     before releasing host authority. An operation still waiting for the
     operations lock then fails as closed, with a definite receipt.
   - **Without an inherited socket**, `serve`'s pre-start probe now uses
     `SO_REUSEADDR` like the real listener. TIME_WAIT no longer blocks a
     restart for a minute; a live listener is still refused.
   - **A worker's host-service client resends only read-only observations:**
     host capabilities, host pressure, the desktop-browser bridge, and
     provider and integration credentials. It resends them for up to 120 s,
     and only when the host provably did not run them:
     - a connection that was never established, or
     - the gateway's new `X-Nebula-IPC-Undispatched: 1` 503, which it
       returns before dispatch only while the host is being replaced (closed,
       or not yet holding the host lock).
     - Capacity limits (busy request slots, the 16-peer cap) and peer
       failures (an unreadable credential) answer a plain 503 and fail at
       once, as before. A peer that is not ready, or whose lease expired,
       still gets a 401 that is not resent.
   - **Waiting callers share one observation.** The host-pressure gate keeps
     one observation in flight; every turn queued behind it gets its outcome.
     During a host outage the queued turns therefore fail together within one
     120 s window, not one window each.
   - Admission, follow-up dispatch, credentials and the browser bridge
     therefore wait through the gap.
   - Calls with effects (Butler memory, knowledge, memory control, host
     settings, search) keep their single attempt and the caller's deadline. A
     late resend could otherwise land after the caller gave up.
   - An older supervisor (rollback) omits the header. Against it, only
     connection failures are resent.
3. **API blue/green** (unchanged).
   - Old viewer streams stay with the draining API worker until it retires.
4. **Target the new execution version.**
   - `target_execution` and `rollback_code` must refuse or defer a third
     version. Today both ignore `_compatible()` returning `False`
     (`deployments.py:506`, `deployments.py:566`). They would point
     auto-admission and recovery at a version that cannot be spawned.
5. **Roll projects.** *(handoff fix and in-Core roller done)*
   - Per project: `prepare_execution` → `handoff` → verified → `retire` source.
   - K projects at a time, by the in-Core roller next to auto-admission, so
     pinned projects are retried without rerunning the external deployer
     (see [In-Core roller](#in-core-roller)).
   - **The handoff fix.** The source's inventory now reports "A queued
     follow-up is being dispatched" while `ChatQueueService._inflight` is
     non-empty. It is read after the checkpoint query: a claim committed before
     that read is still in flight, or it has since changed the checkpointed
     revisions, and transfer then refuses the stale proof.
6. **Retire** old API and execution workers once they own nothing.

## In-Core roller

`runtime_project_roller.py` runs in the supervisor beside auto-admission.
Every 2 s it reads ownership, handoff rows and processes from SQL and acts
only on what that read shows. It does nothing until the host's
`target_execution` differs from a resident project's worker version.

- **K at a time.** At most `NEBULA_ROLL_PROJECTS_AT_ONCE` (default 2, at
  most 16) handoffs it requested are unfinished at once. `0` turns it off and
  leaves moving projects to the external deployer. Projects roll in project
  ID order; never-tried projects go before retries.
- **Each step is a deployment operation.** `prepare_execution`, `handoff`
  and `retire` go through the same receipt ledger and dispatch as
  `POST /api/v1/deployment/operations`, under the actor `core:roller`. The
  roller also writes `roll.target_prepared`, `roll.handoff_requested`,
  `roll.handoff_withdrawn`, `roll.source_retired` and `roll.target_retired`
  receipts.
- **Retire after verify.** A source is retired only when a handoff to one of
  the roller's own targets is `verified`, the target owns the project, and
  the source owns nothing. `retire` never kills a worker.
- **Pinned projects are never held.** Before requesting a handoff, the
  roller checks the project's open resources against the pin rule of the
  source checkpoint. A project with a live harness session, terminal,
  running mission or similar is skipped without a hold. A handoff whose
  source reports a pin is withdrawn, and its prepared target is retargeted
  to the next project. Withdrawal is a compare-and-cancel
  (`withdraw_handoff`): it cancels only while the row still names that
  target at that revision, is `requested` or `finishing`, and the source
  still owns the project. A handoff that transferred meanwhile is left to
  finish. A "handoff to the current owner" is never used for this, because
  after a transfer it would start a handoff back to the old version. A held
  handoff that has not transferred within `NEBULA_ROLL_HELD_LIMIT_SECONDS`
  (default 300) is withdrawn too. Each such project is retried after
  `NEBULA_ROLL_PINNED_RETRY_SECONDS` (default 60), doubling to 15 minutes.
  The roller never evicts a source: its projects keep running on the old
  version until they can move.
- **No spare workers.** A prepared target that no project took in a pass is
  retired at the end of that pass, because an unassigned worker holds memory.
  A worker that was the source of any handoff is never retired as a spare.
  It retires only through the verified-handoff rule above, even when an
  earlier roll prepared it.
- **Auto-admission is not held back.** Auto-admission still waits while an
  unknown unassigned execution worker exists. It no longer counts the target
  of an unfinished handoff as unknown, so a cold project starts during a
  roll.
- **Failed steps back off.** A refused `prepare_execution`, `retire` or
  withdrawal is retried after 5 s, doubling to 5 minutes, never on every
  pass. Pinned projects back off separately, as above.
- **Restart mid-roll.** Handoff phases are durable, and the roller finds the
  targets it prepared from its `roll.target_prepared` receipts and from the
  spawn checkpoint of its own `prepare_execution` operations. A new host
  reuses a prepared target, keeps counting unfinished handoffs toward K, and
  retires verified sources. Only the backoff and held-limit clocks restart.
- **It defers to an operator's deployer.** While an `operator` or `device:`
  deployment operation is running, or ran within
  `NEBULA_ROLL_OPERATOR_QUIET_SECONDS` (default 600), the roller starts
  nothing new. It still withdraws its own pinned or over-held handoffs and
  retires its own verified sources and spare targets. An operator operation
  still marked `running` after an hour is treated as left behind by a
  stopped host and no longer defers the roller. The external deployer's
  `promote` therefore moves projects as before. Pinned projects it leaves behind are rolled by Core once the
  deployer has been quiet for that window. The roller never touches a
  handoff or target that it did not request or prepare.
- **Reported in** authenticated `GET /api/v1/deployment/status` as `roll`:
  `state` (`disabled`, `idle`, `deferred`, `rolling`, `waiting`,
  `blocked`, `complete`), `detail`, `target_version`, `projects_at_once`,
  `remaining`, `moving` (project, phase, target, whether admission is held),
  `pinned` (project, version, blockers, `retry_in_seconds`, `attempts`),
  `blockers` and `retired_sources` (counted since this Core started).
  `roll` describes the roller's last pass, at most 2 s old: a reader that
  has just set a new target must wait until `roll.target_version` equals
  `target_execution_version` before trusting `complete`. Lists are bounded
  to 20 items, with pinned blockers cut to 300 characters. `blocked`
  means the target cannot take a project now, for example a third execution
  version or a refused worker preparation.

## Queued turns and chat-start commands across a release

Turns that are queued but not yet admitted are **parked on the source and
restored on the target. They are not handed off live.**
- The handoff request holds admission of new root turns under the project
  ownership lock.
- The source cancels the local producer of each queued root turn. It emits no
  pause or cancel frame, keeps the turn and its `provider_turn_queue` row
  `queued`, and closes its journal segment.
- The checkpoint covers those rows. The target restores the same turn ids,
  through `_restore_queued_turns`, without crash-interruption semantics.
- Once verified, the target admits them. Each turn sends one provider request
  in total (E5, E6).

Chat-start commands depend on which command carried them.

| Command | When it is used | During handoff |
| --- | --- | --- |
| `chat.accept/v1` | The project already has an owner whose release declares the feature | Completes at acceptance (the turn is durable and bound); viewers stream from the SQL journal, so it does not hold the handoff |
| `http.invoke/v1` (chat start) | The project had no owner when the request arrived (cold project), so the API could not check the feature | The source executes the whole streaming request (see the cases below) |
| Queued, unclaimed commands | Any | Stay on the project's work lane; the target claims them after transfer. Transfer refuses a target that lacks `chat.accept/v1` or `chat.resume/v1` when such commands are queued |

For an `http.invoke/v1` chat start:
- **If its turn is still queued at the hold** (E6), parking closes the turn's
  journal segment. That ends the response and completes the command (HTTP 200)
  on the source, so the project transfers. The turn then runs on the target.
  The viewer's POST stream ends without a terminal frame and must resume by
  turn id (`/chat/turns/{id}/events`). This UI step is not yet verified.
- **If its turn was already admitted**, the command stays `running` until the
  turn ends. The project stays `finishing` with an "unfinished command" blocker.
- **Head-of-line blocking.** A running `http.invoke/v1` also blocks the
  project's work lane. In E6 a follow-up enqueue accepted behind it waited 29 s,
  then ran on the target after transfer. This is the same shape that leaves
  `runtime-stop` uncertain. A rolling release never uses that whole-Core stop
  for additive releases.
- **Fix to adopt.** Decide `chat.accept/v1` for a cold project from the release
  auto-admission will spawn, instead of "no owner, no feature". Cold chat
  starts then stop occupying the work lane.
- **Turn-following responses leave the lane.** Harness chats (which never use
  `chat.accept/v1`) and provider chat starts carried by `http.invoke/v1` start
  their turn before they begin streaming, and the rest of the response only
  follows it. Their route marks the response with the internal
  `x-nebula-follows-turn` header. When the owner sees it, it renames the running
  command `http.follow/v1` under its fence, and lane heads skip that name. The
  response keeps streaming to its viewer and completes as before, but later
  project work no longer waits for the whole turn. Without this, a harness turn
  that never ended held every later chat and mutation in its project until the
  operator stopped it. A followed command still blocks handoff and retirement
  like any running command. If the owner stops, the command becomes uncertain
  and is settled as failed by dead-owner reconciliation, without blocking the
  lane.

## Adversarial review

The review checked every kind of work against the design.

| Work | During the roll | Residual risk |
| --- | --- | --- |
| Streaming provider turn | Finishes on its original worker; viewer may be cut after 5 s at host swap and resumes by cursor | UI reconnect across a host swap not yet verified in a browser |
| Queued provider admission | Waits at host swap (host pressure call is resent); parked then restored at handoff | Bounded by the 120 s host wait; a longer host outage still fails the turn |
| Follow-up queue item | Waits at host swap; a dispatch in flight keeps the project finishing | Requires the #1017 `await` fix |
| Harness turn | Project pinned on the old version, untouched | None new |
| Idle harness session | Detached by the source; the target's next turn resumes the saved vendor thread | A vendor that lost the thread starts fresh with the saved conversation (existing recreate path) |
| Goal | Continues on the target after its turn transfers | Harness goals pin |
| Mission | Running mission pins; a sleeping schedule is re-armed from SQL on the target | A source timer waking after transfer fails on its fence and records a diagnostic |
| Approval | Durable; unconfirmed delivery pins | None new |
| Callback | Pins until the result is materialized | None new |
| Schedule | Occurrence ledger prevents a double fire | Fires late while held |
| Provider subagent | Parent links let descendants admit during the drain | None new |
| Terminal / notebook / debug / Docker or busy Host automation | Pinned on the old version | Pause only when that version must retire |
| Idle Host automation session | Closed by the source at the boundary; recreated on next command | None new |

**Schema migrations.**
- "Additive" is a reviewed manifest flag; nothing verifies it. For two
  versions to share one database it must mean only:
  - new tables;
  - new nullable or defaulted columns;
  - no tightened constraints.
  A new NOT NULL column without a server default breaks the older writers,
  even though the startup schema check passes.
- Additive migrations are also safe only with tolerant readers (step 1).
- Non-additive changes need expand/contract across three releases, or a
  coordinated stop under the drain design (3).

<a id="tolerant-entity-readers"></a>
**Tolerant entity readers** (`stored_entities.py`, feature
`storage.tolerant-entities/v1`).
- Reading: every stored-entity decode (`_row_to_entity`, the connection
  readers, and the modules that validate `row.payload` directly) goes through
  `decode_stored`. It validates strictly first, so a row without unknown keys
  costs one ordinary validation. Only on failure does it set aside the keys
  the model reports as `extra_forbidden`, at any depth, and validate again.
  Any other failure is still `CorruptRecordError`, and so is an unknown key
  reported through a union member: pydantic then also reports keys that are
  valid in the matching member, so nothing is guessed. No entity uses such a
  union today.
- Keeping: the keys set aside travel with the entity and are written back
  whenever this release rewrites the row: `StoreTransaction.update`, both
  connection update paths, `replace`, legacy relation replacement and
  operator profile updates. A top-level key always survives. A nested key
  survives while its parent object exists at the same path, even when this
  release rewrote that object (so a newer field derived from replaced data
  can go stale). A key inside a list item follows the item: an item with an
  `id` is found by it after reordering or appends; an item without one must
  keep its index and content. Otherwise the key is dropped rather than
  attached to another item. A key this release defines is never overwritten
  by an old unknown value. A new row, even a copy of a decoded entity, never
  inherits another row's unknown keys.
- Input stays strict: API bodies, patches and `update` changes are validated
  with `extra="forbid"` as before. In an update an unknown key is tolerated
  only where the stored row already holds it with the same value.
- Cost: the update path validates the merged payload once, as before. It
  also checks the old value of a rewritten field for unknown keys, but only
  for fields whose type contains a strict nested model (53 of about 1,900
  entity fields today); free-form dicts and scalar lists hold no unknown keys.
- Entity dumps embedded in other rows (a chat turn's request snapshot, a
  harness session's MCP and scope snapshots) are decoded the same way.
- Not covered: append-only event payloads (`ButlerEventPayload` and
  operation/run events) and host-call bodies; see host-call skew below.

**Adding a stored-entity field** (the rule for release N+1):
1. Field additions must be optional or defaulted, so release N's rows still
   validate on N+1.
2. `runtime-compatibility.json` records `entity_fields_sha256`, a digest of
   every stored entity field name (nested models included).
   `load_compatibility` refuses to package or prepare a release whose models
   differ from it, so a field change always gets a review. When the reviewed
   digest differs from `entity_fields_tolerant_baseline_sha256` (the fields
   of the first tolerant release), the release must declare
   `storage.entity-field-additions/v1`.
3. The coordinator refuses to start that release as API or execution
   (`reserve_worker`, `target_execution`) while any resident version or the
   current execution target lacks `storage.tolerant-entities/v1`. The current
   target becomes the rollback version when the candidate is targeted, so a
   stale rollback slot does not block.
4. Conversely, once a release declaring additions has run or been targeted,
   its fields may be in any row: no release without tolerant readers can
   start, become the target or be the destination of `rollback_code` again.
5. The first tolerant release must therefore be rolled out, and older
   versions retired, before any field addition. After that the checks only
   refuse going back to a pre-tolerant release.

**Host-call version skew.**
- During a roll, the new supervisor serves old workers. On a rollback, the old
  supervisor serves new workers.
- Host-call clients check exact response key sets (for example
  `runtime_memory_control.py`, `remote_knowledge.py`,
  `runtime_host_settings_bridge.py`), and request models forbid extra keys. So
  the tolerant-reader rule must cover host calls, not just stored entities.

**Rollback.**
- Code only, through the same roll in reverse: `rollback_code`, then host swap,
  API and handoffs. Data is never restored.
- It is only safe inside the schema window and only with tolerant readers.
- A host rollback to an older supervisor loses the undispatched marker, so it
  can still fail work that hits a 503 during its own swap.

**Failure modes.**
- **New generation fails to start.** The holder keeps queuing, but nothing
  answers. The deployer must start the previous retained release. Worker host
  observations fail after 120 s.
  - Held projects (`admission_held`) stay held until some supervisor runs,
    because only the supervisor advances handoffs.
  - Repeated start failures hit systemd's start limit, which fails the socket
    unit and closes the listener. The deployer must watch for that.
- **Socket-triggered restart.** After `systemctl stop`, a connection to the
  socket unit starts the service again, possibly on the old `current`. The
  deployer must switch `current` before stopping, or stop and start in one
  restart.
- **Old generation hangs.** With a holder, the stop is bounded: at most 30 s
  for an exchange awaiting its receipt, 5 s of flush, then stalled connections
  are aborted, with uvicorn's 45 s timeout as a backstop. With the onefile
  build, `TimeoutStopSec` and `KillMode=process` kill only the bootloader; the
  supervisor child can survive holding `supervisor.lock`. The next generation
  then refuses to start until that child is gone.
- **Supervisor crash mid-roll.** Handoff phases are durable and transfer is
  idempotent; held projects wait.
- **Keep-alive race.** If the old host closes a pooled connection just as a
  request is written, the worker sees `RemoteProtocolError`, which is not
  resent. That follow-up can still go to review. The window is narrow.
- **Butler actions.** A Butler action in `dispatching` when a project transfers
  is reconciled to `uncertain` on the target (`butler_authority.py`). Butler
  actions are not in the handoff inventory.
- **PostgreSQL blip longer than 5 s.** Workers exit after a failed heartbeat
  and are recovered. This predates this design and is out of scope.

## Prototype

Changed modules:
- `runtime_service.py`: `inherited_listener`, and the drain that starts only
  while a holder keeps the socket.
- `supervisor.py`: `drain_relays`, which closes only exchanges that already
  answered.
- `runtime_host_pressure.py`: one shared host-pressure observation.
- `cli.py`: socket adoption, `--listen-fd`; `probe_listen_address` with
  `SO_REUSEADDR`.
- `runtime_management.py` and `supervisor.py`: shielded deployment operations,
  a retryable 503 for a cancelled request, and joining before host authority
  is released.
- `runtime_ipc.py`: `RuntimeIpcUndispatched`.
- `runtime_host_services.py`: `HostServiceClient`, undispatched 503 marker.
- `runtime_project_handoff.py`: in-flight follow-up blocker.

The rehearsal tools live outside the repository:
- `listener_holder.py` emulates the systemd socket unit.
- `deploy.sh` runs stage+migrate, swaps the holder's supervisor generation to
  release B, then promotes the API, targets, and hands off every project.

The tools are committed under `scripts/deploy_rehearsal/`. The E4–E6 results
are recorded in
[docs/validation/rolling-core-releases.md](../validation/rolling-core-releases.md).

## Remaining work

Landed since this list was written: third-version targeting
(`_require_startable_target`), `serve --listen-fd`, closing idle Host
automation sessions at the handoff boundary
(`release_idle_sessions_for_handoff`), Butler actions in `dispatching` in the
handoff inventory, and the in-Core roller.

- **Production unit.** *(done in source; not yet installed on the live host)*
  - `scripts/simulation/systemd/` ships `nebula-core.socket`
    (`ListenStream=127.0.0.1:8000`) and a service drop-in with
    `Requires=`, `KillMode=process`, `ExitType=main` and `TimeoutStopSec=90`.
    The operator adds `--listen-fd 3` to `serve` (see
    `scripts/simulation/README.md`).
  - The deployer detects the socket, verifies that configuration, selects
    `current` before stopping the old supervisor, restores a socket failed by
    the start limit, and reports `socket_listening` in its readback.
  - It waits up to 120 s for the live supervisor's in-flight deployment
    operations (`GET /api/v1/deployment/operations`) and refuses the swap if
    one is still running.
  - Rehearsed with real user systemd and a forwarding wrapper: two swaps,
    213 requests, 0 refused, worst queued request 5.6 s. Not yet rehearsed
    with a frozen Core or on the live host.
- **In-Core roller follow-ups.**
  - Teach the external deployer to hand the per-project loop to the roller:
    stop after `target_execution` and wait on `roll.state` instead of moving
    projects itself.
  - A deployer handoff requested in the same instant as a roller pass can be
    retargeted by the roller. The window is one pass, because the deployer's
    `prepare_execution` operation already defers the roller.
  - Pinned and harness journeys, a third resident version, and a supervisor
    swap during a roller roll are not yet rehearsed on a real Core.
- **Tolerant readers.** *(done for stored entities:
  [Tolerant entity readers](#tolerant-entity-readers).)* Still open: event
  payloads and host-call request and response bodies.
- **Pinned adapters:**
  - Done: detach idle harness sessions and resume the vendor session on the
    target; re-arm scheduled-only missions from SQL; close idle Host
    automation sessions (no command in flight, running or holding a detached
    group) before the inventory.
  - Remaining kinds (live terminals, notebooks, debug, Docker and busy Host
    automation sessions, busy harness sessions,
    running missions, unconfirmed callbacks or approval deliveries) stay
    pinned by design.
- **Host-services socket ownership.** Hold the host-services socket in the
  holder too, which makes the 120 s worker wait the exception rather than the
  path. The browser-bridge registration lives only in memory and is lost at a
  host swap.
- **Cold chat starts.** Use `chat.accept/v1` for a cold project's chat start
  (see [Queued turns and chat-start commands](#queued-turns-and-chat-start-commands-across-a-release)).
- **Evidence still missing:**
  - browser UI reconnect across a host swap, desktop and mobile;
  - harness, goal, mission, approval, schedule and callback journeys during a
    roll;
  - packaged (frozen) bundles;
  - the live socket unit (source and deployer support are done).
- **AGENTS.md.** Once the production unit and roller are live and rehearsed,
  replace the Butler ritual for supervisor-mode deploys with a pinned-project
  report. Keep the ritual for legacy and non-additive deploys.
