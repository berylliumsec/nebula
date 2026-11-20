# Butler home worker

Status: proposed (2026-11-20). Owner: Core.

## Problem

Every Butler session belongs to one project and runs on that project's
execution worker. An execution worker runs one database transaction at a time,
because each transaction takes the runtime's exclusive advisory key
(`execution_scope.py`, `_BEGIN_LOCKS`). When a project is busy, Butler waits in
the same line as every research agent in that project.

Here is what the live measurements showed on 2026-11-20 (release d223a68):

- **The trigger.** Apple Bug Bounty went from 15–30 to 45–84 concurrent turns.
- **Throughput stayed flat.** The worker handled about 1,800 tool calls per hour regardless of how many turns ran.
- **The pool was saturated.** The project's database pool, at its size of 4, always had about 15 waiters, and each waited 262 ms on average.
- **Tool calls slowed down.**
  - `tool_output.read` p50 went from 0.3 s to 30–50 s.
  - `run_command` went from about 11 s to 45–66 s.
  - The same calls in other projects stayed at 0.2 s.
- **Butler became slow.**
  - A simple Butler question took 19 minutes. Only about 5 of those minutes were model time.
  - `GET /butler/overview` averaged 41 s.

The host had spare capacity the whole time: about 11 of 32 cores were busy and
46 of 60 GB of RAM was free.

## Goal

Butler gets its own execution worker. Its turns, tool bookkeeping, tool output
and reads of other projects never wait behind another project's advisory key.

**Observable invariants:**

1. A Butler question about a saturated project costs model time plus a few seconds of tool time, whatever that project's load.
2. Butler cannot write, lock or start work in another project's rows directly. Every action on another project is a typed command that the project's owner executes, under the project's own Butler authority policy.
   - Shared global rows (`engagement_id IS NULL`, `project_id IS NULL`) are the exception, as for every worker today.
3. A deploy treats the Butler worker like any other worker: it gets a grace period, then a crash-style handoff. A Butler turn may be interrupted. No effect is replayed or recorded as not having happened.
4. Operators keep one Butler per project in the UI, plus Butler mode across projects. The home project is invisible except where Butler explains itself.

## Non-goals

- **Faster research agents.** That needs fewer concurrent turns per project, splitting campaigns across projects, or the reads-skip-the-lock change in Alternatives.
- **Moving Butler's existing chats.** Legacy project-local Butler sessions stay readable where they are.

## Design

### 1. The Butler home project

The host creates one system project the first time a Butler session is reserved, not at bootstrap: `Engagement(id="__nebula_butler__", name="Butler", metadata={"system": "butler"})`.

- **Why lazily.** At bootstrap it would become the oldest project.
  - `list_entities(Engagement, limit=1)` would then select it for the Kali image (automation_runtime.py:1594, runtime_host_automation.py:112).
  - It would also break setup's "exactly one project" check (setup.py:1335).
- **A real project.** It has its own `project_ownership` row, workspace, scope policy, worker and advisory key, so handoff, eviction, recovery and fencing apply unchanged.
- **Special-cased by ID.** Like `GLOBAL_EXECUTION_PROJECT` (deployment_protocol.py:15), it is recognized by its id, not by `metadata.system`. The constant lives in `deployment_protocol.py`.
- **Hidden.** A single `user_projects()` filter excludes the reserved ids. Every engagement enumeration uses it:
  - API list and search: search.py:689.
  - Setup: setup.py:1335.
  - Automation image selection.
  - Butler project list (butler_projects.py:83), recovery scans (butler_recovery.py:1015/1120) and global capabilities (butler_global_capabilities.py:752).
  - Container terminal (container_terminal.py:2287).
  - The project switcher and board.
  - Archive and delete routes refuse it, and `create_child_project` cannot target it.
  - A grep test fails on any new `Engagement` enumeration that does not use the filter.
- **Pinned.**
  - `RuntimeAutoAdmission` admits it while any Butler thread is open or a scheduled check is due.
  - Capacity retirement skips it, as it already skips `GLOBAL_EXECUTION_PROJECT`.
  - Idle retirement still applies after 300 s with no open work. A cold start costs a few seconds, which is acceptable.
- **Budget.** It uses one runtime slot and one share of the connection budget. There is headroom: slots are 3–16, and live has about 46 GB of RAM free.

### 2. Observer read scope

The home worker runs the full execution kernel. Its roughly 30 background controllers assume they see only their own project. Examples:

- mission reclaim (missions.py:251);
- oversight wake-ups (butler_oversight.py:933);
- queue stepping (chat_queue.py:987);
- notebook MCP registration (execution_kernel.py:2281).

Widening reads for the whole worker would let them start provider, harness or command effects for other projects, and the write predicate cannot fence those. The scope is unchanged by default. Widened reads exist only inside an explicit context:

```python
with observer_reads():          # contextvar; home lease only
    goals = await work.goals(target_project)
```

**Rules:**

- **Who may enter it.** `observer_reads()` raises unless the current lease is the home project's (`__nebula_butler__`). Only Butler read tools enter it. Controllers, schedulers and recovery loops never run inside it.
- **What changes inside it.** SELECTs carry no project loader criteria. A "no read criteria" mode is cheaper to compile than an IN list of hundreds of projects, and it sees projects created mid-turn.
- **Row locks are writes.** Any `Select` with `_for_update_arg` uses the write predicate (owned projects), even inside the context. So a Butler read can never take `FOR UPDATE` or `FOR SHARE` on a target row. Existing sites that would otherwise do so: the Butler tool audit (butler_capabilities.py:641, 663-670) and `assert_project_workspace` with storage's 8 sites (storage.py:630).
- **The audit moves home.** The Butler tool audit records in the home project, with a `target_project` field.
- **Writes stay fenced.** The write branch computes its own predicate: `_validate_insert` (execution_scope.py:807), `_validate_update`, the owner-subselect predicates, the projection-write recheck (:621) and `_commit`'s lease checks all use owned projects. If Butler reads a target row and then flushes an update, the update matches zero rows or raises `LeaseLost`.
- **Reads take no target lock.** They are plain read-committed reads of committed rows, the same guarantee the global read path (`runtime_global_project_reads.py`) gives the Butler board. They may be inconsistent across separate sessions, so tools that need one snapshot read in a single session.
- **Live state.** In-flight turn state comes from the turn journal or the proxy, not from SQL rows. `nebula.work.chat` already reads committed messages plus pending-turn headers.
- **Narrowing still applies.** `_current_lease` narrowing (execution_scope.py:381) still applies outside the context.
- **Shared Butler memory.** `shared_butler_memory._authority` (shared_butler_memory.py:70-74) gains an explicit home-lease branch for `global_*` scopes and for `project_lesson` reads of any project. Memory writes stay host RPCs, as today.

### 3. Sessions: home project plus a focus project

A Butler session lives in the home project, and `metadata.butler_focus_project_id` names the project it is about.

- **Reservation.**
  - The new route `POST /api/v1/butler/sessions` routes explicitly to the home project.
  - The body carries the focus as `focus: {"project": "<id>"}`. Field names that end in `_id` are routing references (`runtime_routing.py:48-92`), and a second project reference trips the 409 at :167.
  - The legacy `ButlerSessionReservation.engagement_id` route stays for the fallback (see Deploy and recovery).
  - The reservation lock keys on the focus project, and `active_control_assistant_session` filters by focus.
- **Panel.** The panel in project X reserves the session focused on X. Butler mode threads pick a focus project, or none for a Nebula-wide thread.
- **Routing.**
  - A chat request names only the session, so it resolves to the home project.
  - Every Butler intent body is audited so that only one project is a routing reference: reservation, action and case commands.
  - A router test pins each shape.

### 4. Tools take a target project

`ButlerCapabilities` and the settings and work tool closures stop deriving `project_id` from `session.engagement_id`.

- **Default target.** Every tool resolves its target from an explicit `project` argument, or else from the session's focus project.
- **Reads** run in-process under observer scope (section 2). This covers `nebula.work.*` reads, overview, attention, timeline, catalog, work items, goals, chats, notes, evidence and memory recall.
- **Actions** that change a target project use a new typed command: pause or resume a goal, message an agent, start a chat, change chat settings, create a goal, and Work changes. No existing path fits.
  - `RuntimeCoordination.submit` (runtime_coordination.py:802) is host-side only, and no host service exposes it (runtime_host_services.py:264-316).
  - The generic `RuntimeDelivery` intent `http.invoke/v1` is replayed with the operator bearer token (runtime_delivery.py:1167). That path would skip `ButlerAuthorityPolicy` entirely.
- **The new command.**
  - **Format.** `butler.action/v1` is a command with a closed set of action kinds and typed arguments.
  - **Submission.** A host-service RPC, `butler.submit_action`, accepts it only from a caller holding the home lease. The RPC calls `RuntimeCoordination.submit` for the target on its control lane, with `actor_id="butler"`.
  - **Target-side check.** A handler re-reads the target's `ButlerAuthorityPolicy` and refuses actions it does not grant. It runs the action through the same `ControlAssistantWork` method Butler uses today, and records the audit in the target.
  - **Idempotency.** The key is `turn_id:tool_call_id`. Provider tool-call ids repeat across turns, and the coordination key is scoped to (actor, key) (runtime_coordination.py:858).
  - **Expiry.** Commands carry `expires_at` (default 10 minutes). The handler refuses expired ones, so a command queued behind a held or retiring project (`ProjectRetiring`, :890) does not act long after Butler reported it.
  - **Receipt.** The tool waits up to 30 s for the receipt. If it has not arrived, it returns `accepted` with the command id, and a later read reports the outcome.
- **May repeat.** An interrupted Butler turn that a goal replays gets new tool-call ids, so an action can run twice. Pause, resume and settings actions are idempotent on the target's state. Messages and chat starts may duplicate, which is the same contract as crash recovery today: claims are released and work continues, with no reconciliation hold.
- **Workspace and shell.**
  - `workspace.*` and `tool_output.read` resolve the target project's workspace read-only from `Engagement.workspace_path`.
  - `run_command` runs in the home project's execution mode. Host mode can already reach project trees. Commands are audited against the home project and labelled with the target.
  - Butler gets no container access to another project's sandbox; it delegates that work to an agent in that project.
- **Notes and evidence** stay per project. Writes are commands to the target.

### 5. Scheduled and recovery checks

- **Nebula-wide checks** move to the home project.
  - `_global_anchor` becomes the home project. This removes the "oldest Act project with a Butler chat" anchor and the inference that each worker thinks it is the anchor.
  - `ButlerGlobalActions` reads every project under observer scope, and its actions go through section 4.
- **Per-project recovery cases** keep `case.engagement_id = target`. Their child chat runs in the home project with `butler_recovery_case_id` and the focus set to the target.
  - The target worker still detects cases. Instead of starting a turn in itself, it submits a typed `butler.case_opened/v1` command to the home project, using the same RPC pattern in reverse: the caller holds the target lease, and the home handler opens the case chat.
  - This takes recovery chats out of busy projects entirely. Combined with #1239, which removed overdue check-ins, recovery chat volume drops to real incidents.

### 6. UI

- Panel and Butler mode keep their look. The project picker becomes "Focus", and threads show the focus project instead of `session.engagement_id`.
- No new surfaces. Before UI work, a Figma mockup of the Focus label and Nebula-wide threads goes to the operator for approval.
- The settings assistant catalog and `docs/design/nebula-settings-assistant.md` learn that Butler is Nebula-wide with a focus project.

## Deploy and recovery

- **Handoff.** The home project hands off like any project. Live Butler turns get `HANDOFF_GRACE_SECONDS` (180 s) to finish, then crash-style recovery marks them interrupted. Pending action commands stay in the target's `runtime_commands` and complete there, because they are durable and idempotent.
- **Feature gate.**
  - Releases declare `butler.home/v1` in `manifest.features`. The existing checks are at deployments.py:1853 and :2172, `_participants_supported`, and runtime_auto_admission.py:977.
  - The host's `_ensure_project` and auto-admission refuse to admit `__nebula_butler__` on an execution version without the feature.
  - The target-side `butler.action/v1` handler exists only in feature versions. Submission checks the target's resident version, and an action to a target on an older version returns "unavailable until the project is upgraded".
- **Rollback.**
  - An older host does not ignore the home project: it is an ordinary `Engagement` row with open chats, and old auto-admission would admit it under old Butler code, which treats the home project as its target. Rollback below `butler.home/v1` is therefore guarded:
    - the deployer refuses an API or host rollback to a version without the feature while `__nebula_butler__` has open chats or queued commands;
    - `deployer butler-home archive` closes the home threads and archives the project first. It then reverts the UI to project-local sessions.
  - Code rollback within feature versions is unaffected.
- **UI capability.** The UI reads `butler.home/v1` from the API's manifest. Without it, the UI reserves legacy project-local sessions, as today.

## Rollout

The feature flag `NEBULA_BUTLER_HOME_WORKER` defaults off in PRs 1–3. PR 4 turns it on.

| PR | Scope | Verification |
|---|---|---|
| 1 | Reserved home id; `not_reserved_project()` filter in every store listing, overview and search; CRUD refuses edit, archive and delete; capacity-retirement exemption; the `observer_reads()` context (home lease only; `FOR UPDATE` uses the write predicate); `butler.home/v1` feature gate in admission. Nothing creates the home project yet. | Scope unit tests prove: cross-project SELECTs work only inside the context; `FOR UPDATE` on a target is refused; target insert and update give zero rows or `LeaseLost`; the context raises on a non-home lease. Staging admission and retirement. |
| 2 | Lazy creation of the home project on first reservation; `butler.action/v1` command, host RPC and target handler with authority check, idempotency and expiry; home session route with focus; router shape tests; tool target resolution; Butler audit moved home; shared memory home branch | Python tests per tool family and per action kind, including refusal under watch authority. Real-Core test: Butler in home reads a saturated project while that project's advisory key is held. |
| 3 | `butler.case_opened/v1`; scheduled and recovery checks to home; `_global_anchor` removal | Staging replay of a recovery case, and of a Nebula-wide check across 3 projects. |
| 4 | UI focus picker, after mockup approval; settings assistant catalog; deployer rollback guard and `butler-home archive`; flag on | Real-Core Playwright area `butler-mode` and panel. Phone width. Staging deploy cycle with a live Butler turn, plus a guarded rollback attempt. |

**Acceptance on staging:** saturate one project with the stub model at 60 concurrent turns. A Butler question about that project should then finish in model time plus at most 5 s of tool time. Today it takes minutes.

## Alternatives considered

- **Reads skip the lock in every worker.** Read-only transactions would take the runtime key only on their first write. This raises every project's ceiling, not just Butler's, but changes the fencing invariant for all code. It is complementary; do it after this, with its own review.
- **Butler priority in the project's queue.** Butler transactions would jump the advisory-key queue. This is cheap, but Butler still shares the saturated worker and its event loop (81% main-thread CPU on live).
- **A bigger DB pool.** No help. The advisory key serializes transactions, so a bigger pool only moves the queue into Postgres.
- **API-process Butler.** Running Butler turns in the API process breaks the rule that provider calls and mutations run under a fenced owner (`runtime_global_api.py`), and it loses crash recovery.

## Open questions

1. Should Butler-initiated chats (goal chats Butler starts *for* a project) stay in the target project? Proposed: yes, because they are real work.
2. How should the operator see Butler's run_command output that targets another project's tree: in the home project's terminal audit, in the target's, or both?
3. The home project's default execution mode: host (as today) or container?
