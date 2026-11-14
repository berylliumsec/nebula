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
- Still needed: an in-Core roller (see [Remaining work](#remaining-work)).

**What still needs a pause.** Nothing is interrupted for provider conversations,
including:
- streaming turns;
- queued turns and follow-ups;
- goals;
- approvals and schedules;
- provider subagents.

Three cases still need an operator decision:

1. **Pinned projects.** These hold a live vendor (harness) connection, including
   an idle one. They also include projects with a terminal, a notebook, a debug
   or automation session, a running mission or execution task, an unconfirmed
   callback or approval delivery, or a charged compute job.
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
| Harness turn / idle harness session | Pins the project | Interrupted, not resumed |
| Goal | Continues after its turn transfers | Paused and resumed with its turn (provider); harness goals stay paused |
| Mission | Running mission pins | Resumes from checkpoint |
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
     decoding of entity payloads. Entities use `extra="forbid"`, so a field
     written by release N+1 makes release N raise `CorruptRecordError` on the
     same row. That breaks old workers and code rollback alike.
   - Release N+1 may write a new field only after every resident and retained
     rollback version tolerates unknown keys.
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
   - **The old generation relays open requests for at most
     `VIEWER_DRAIN_SECONDS` (5 s), then closes them.** Viewers resume from
     their cursor. Today the drain is unbounded: the old process keeps the host
     lock until the longest stream ends.
   - **Deployment operations are not cut by that bound.** Each operation runs
     in a shielded task that records its receipt even if its request is
     cancelled. The supervisor joins those tasks before releasing host
     authority. An operation still waiting for the operations lock then fails
     as closed, with a definite receipt.
   - **Other supervisor-local requests are cut after 5 s.** These are host
     settings, memory budgets and pairing; they are short, and clients retry
     with their idempotency key.
   - **Without an inherited socket**, `serve`'s pre-start probe now uses
     `SO_REUSEADDR` like the real listener. TIME_WAIT no longer blocks a
     restart for a minute; a live listener is still refused.
   - **A worker's host-service client resends only read-only observations:**
     host capabilities, host pressure, the desktop-browser bridge, and
     provider and integration credentials. It resends them for up to 120 s,
     and only when the host provably did not run them:
     - a connection that was never established, or
     - the gateway's new `X-Nebula-IPC-Undispatched: 1` 503. The gateway
       returns it before dispatch while it is reconnecting, closed or
       slot-limited, or when it cannot load the peer. A peer that is not ready,
       or whose lease expired, still gets a 401 that is not resent.
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
5. **Roll projects.** *(handoff fix done; roller not built)*
   - Per project: `prepare_execution` → `handoff` → verified → `retire` source.
   - K projects at a time.
   - Preferably in an in-Core reconciler next to auto-admission, so pinned
     projects are retried without rerunning the external deployer.
   - **The handoff fix.** The source's inventory now reports "A queued
     follow-up is being dispatched" while `ChatQueueService._inflight` is
     non-empty. It is read after the checkpoint query: a claim committed before
     that read is still in flight, or it has since changed the checkpointed
     revisions, and transfer then refuses the stale proof.
6. **Retire** old API and execution workers once they own nothing.

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

## Adversarial review

The review checked every kind of work against the design.

| Work | During the roll | Residual risk |
| --- | --- | --- |
| Streaming provider turn | Finishes on its original worker; viewer may be cut after 5 s at host swap and resumes by cursor | UI reconnect across a host swap not yet verified in a browser |
| Queued provider admission | Waits at host swap (host pressure call is resent); parked then restored at handoff | Bounded by the 120 s host wait; a longer host outage still fails the turn |
| Follow-up queue item | Waits at host swap; a dispatch in flight keeps the project finishing | Requires the #1017 `await` fix |
| Harness turn / idle harness session | Project pinned on the old version, untouched | Needs a per-vendor idle-session detach adapter |
| Goal | Continues on the target after its turn transfers | Harness goals pin |
| Mission | Running or scheduled mission pins | Scheduled-only missions could be re-armed from SQL on the target |
| Approval | Durable; unconfirmed delivery pins | None new |
| Callback | Pins until the result is materialized | None new |
| Schedule | Occurrence ledger prevents a double fire | Fires late while held |
| Provider subagent | Parent links let descendants admit during the drain | None new |
| Terminal / notebook / debug / automation | Pinned on the old version | Pause only when that version must retire |

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
- **Old generation hangs.** The viewer drain is bounded. With the onefile
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
- `runtime_service.py`: `inherited_listener`, bounded viewer drain.
- `cli.py`: socket adoption, `--listen-fd`; `probe_listen_address` with
  `SO_REUSEADDR`.
- `runtime_management.py` and `supervisor.py`: shielded deployment operations,
  joined before host authority is released.
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

- **Production unit.**
  - Add `nebula-core.socket` (`ListenStream=127.0.0.1:8000`) and make the
    service `Requires=` it.
  - Pass `--listen-fd 3` to `serve`, because `mercury-run` and the onefile
    bootloader sit between systemd and Core.
  - The deployer must switch `current` before restarting. It must watch the
    socket unit's start limit and accept the socket-activated unit in its
    activation readback.
  - It must also refuse a host swap while a ledger operation is `running`; the
    swap would wait for it.
- **In-Core roller.** Retire sources automatically after verified handoff.
  Retarget handoffs off pinned projects. Allow K projects in parallel.
- **Third-version targeting.** Fix `target_execution`'s ignored compatibility
  result (step 4).
- **Tolerant readers.** Entity decoding must tolerate unknown keys before two
  releases share rows.
- **Pinned adapters:**
  - Detach idle harness sessions and resume the vendor session on the target.
  - Re-arm scheduled-only missions from SQL.
  - Remaining kinds (live terminals, notebooks, debug and automation sessions,
    running missions, unconfirmed callbacks or approval deliveries) stay
    pinned by design.
- **Host-services socket ownership.** Hold the host-services socket in the
  holder too, which makes the 120 s worker wait the exception rather than the
  path. The browser-bridge registration lives only in memory and is lost at a
  host swap.
- **Inventory gaps.** Add Butler actions in `dispatching` to the handoff
  inventory.
- **Cold chat starts.** Use `chat.accept/v1` for a cold project's chat start
  (see [Queued turns and chat-start commands](#queued-turns-and-chat-start-commands-across-a-release)).
- **Evidence still missing:**
  - browser UI reconnect across a host swap, desktop and mobile;
  - harness, goal, mission, approval, schedule and callback journeys during a
    roll;
  - packaged (frozen) bundles;
  - the live socket unit.
- **AGENTS.md.** Once the production unit and roller are live and rehearsed,
  replace the Butler ritual for supervisor-mode deploys with a pinned-project
  report. Keep the ritual for legacy and non-additive deploys.
