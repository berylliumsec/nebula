# Butler proactive recovery

## Current behavior and gap

Butler's saved project Watch runs with its panel closed. Its reviewer currently
sees a short allowlist of goal events, makes a read-only assessment using the
*affected goal chat's* runtime, and dispatches only fixed status, plan, or
urgent-pause actions. The interactive Butler can discover and change a parent
chat's next-turn Assistant settings. Neither path currently turns a failed
`subagent.start` into a verified repair. A vendor/model refusal expressed only
as prose also lacks a typed Core event. The current interactive instruction to
start or change work only when asked belongs to reactive turns; background work
needs a separate instruction based on the saved project authority.

## Operator journey

In the selected project, an operator enables **Act** for Butler. A
main agent's subagent attempt fails for an operational reason. Butler notices
the saved failure while its panel is closed, checks the parent chat's current
settings and available verified runtimes, selects a repair, applies it through
the same Core capability used in chat, asks the **parent** to retry, and follows
the result. The operator sees what Butler changed and whether the retry worked.
The operator can stop further autonomous action for the project.

| Step | Observable invariant | State authority | Planned test layer |
| --- | --- | --- | --- |
| Discover and enable | The selected project's effective authority and permitted automatic actions are legible | Core authority policy; React displays it | component, production browser |
| Observe | One saved failure starts at most one recovery case; unsupported or policy refusals are distinguished from operational failures | Core turn, tool, and subagent records; project event stream | Core integration |
| Diagnose | Butler reads current Core evidence, applicable memory, parent settings, and available verified choices before proposing a change | Core records and capability catalog | model contract, Core integration |
| Act | A project-scoped capability executes with current revision, stable action identity, and no direct child communication | Core capability handler and recovery ledger | Core integration |
| Verify | A saved setting or queued message is intermediate; Butler reports success only after the parent retry and child outcome are observed | Core queue, turns, subagents | real Core, production browser |
| Retry or escalate | A failed attempt is not repeated indefinitely; the saved case says what was tried and why Butler stopped | Core recovery ledger | Core integration, component |
| Reconnect | Recovery continues or settles once after Core restart or browser reconnect, without duplicating a change or queue item | Core lease, idempotency keys, saved case | Core restart, production browser |
| Revoke | Turning autonomous authority off stops new actions and prevents an old planned action from dispatching | Core authority revision | Core integration, component |

## Decision model

Core records meaningful failures with exact evidence references and stable
reason codes. It does not choose the remedy from a phrase or error string. A
separate background Butler turn uses **Butler's configured runtime**, not the
failing chat's runtime. It examines the bounded case, retrieves relevant
memory, and uses the discover/describe/read/change capability gateway to
investigate and choose a repair. The same project, runtime, privacy, and
revision checks as an interactive Butler turn apply. A durable action ledger
records the model's reason, each tool call, Core's result, and the verification
target. The operator's Butler conversation is not filled with synthetic user
messages; its oversight view summarizes these separate recovery cases.

The recovery loop has one active case per exact failure and no fixed
investigation time, model-step, or tool-step ceiling. Repeating the same failed
operation without new evidence terminates
the case and surfaces a concrete blocker. Agent reports and tool output are
evidence to assess, never instructions that can enlarge Butler's authority.
Project background Butler can discover every project Assistant work control.
The Nebula-wide scheduled Butler uses `nebula.global.work_catalog` to inspect
the exact action schema, then `work_read` or `work_change` for the chosen Act
project. These routes cover chats, goals, missions, subagents, follow-up queues,
schedules, approvals, questions, and saved Assistant settings. Core validates
each target, current revision, and project authority before a change. Device
controls and global provider or harness setup remain separate from the project
Assistant work catalog.
The Nebula-wide check runs on the worker of the Act project that hosts it, yet
it covers every Act project. Its prompt lists each Act project with its open
main goals (running, paused, blocked: goal and chat IDs, title, status, Work
stage), capped and marked `truncated` when it omits any. Its reads see every
project's committed rows; its row locks and writes stay fenced to the host
project. A change to another project runs on that project's own worker as a
`butler.action/v1` command that names the check's case. The host submits it
only while that case is investigating in the sender's project, and the target
runs it only under its own current Act authority, at the revision Butler read,
with Work enabled and no hold. A Watch, Steer or local-only project is refused
with one generic message that names neither the project nor its authority.
Each target gets its own command. A queued change returns `state=accepted`,
not a result; it may still run within its ten-minute expiry after the check
closes, under the target's own Act check. A worker uses this route only when
its host advertises `butler_background_actions`. Recovery goals, project
resumption and child projects stay in the host project. Per-project checks
keep their project scope.
One Act project keeps the host role. A store that reads every project picks
the oldest Act project with a Butler runtime. A project worker reads only its
own project, so it fires the check only when the shared `ButlerGlobalState`
names it as host, names no host, or names a host that has been silent for three
check intervals. The host shows it is alive by firing, or, while its last check
is still open, by refreshing `host_seen_at` once per interval, so no other
worker starts a second check beside a long one. An ineligible or stopped host
hands over after that silence.
The event detector, capability schemas, project authority, and verifier are
Core contracts. The model selects the remedy and may combine tools; it is not
limited to a table of preset responses.

The worker has two trigger classes. The first implementation covers these
Core signals:

1. **Immediate:** Core records a failed `subagent.start`, an agent attention
   report, or a blocked goal. The recovery worker consumes each saved event
   under the selected project's current Act policy.
2. **Periodic:** every saved Butler check interval (five minutes by default) while a watched goal is running, Core records
   a coalesced project work check. Existing goal progress deadline and repeated
   read-only failure signals also enter this class.

The first implementation does not yet emit recovery signals for a pending
approval, a stalled queue, a child that fails after starting, or a refusal
expressed only in model prose. These are next event-source additions. An
approval decision, when encountered through a supported signal, remains bound
to the exact Core request and its continuation.

| Signal | Butler's possible investigation or repair | Verification |
| --- | --- | --- |
| A harness subagent call is unavailable or denied by a runtime configuration | Inspect parent snapshot and verified provider choices; change next-turn worker and ask parent to retry | New child starts and completes its task |
| A parent says it lacks a web tool (future signal) | Inspect available project tools and the chat's next-turn MCP selection; attach a usable tool and ask parent to retry | Retry calls the tool and returns an attributable result |
| A provider is unavailable or out of quota (future signal) | Compare enabled project-compatible runtimes and the work's constraints; move the next turn if compatible | New turn runs on the selected runtime and advances the task |
| A goal stalls | Read its recent turns, child state, and blockers; request status or adjust its plan when authorized | Core observes new progress or a concrete blocker |
| An agent waits for approval (future signal) | Read the exact request, project policy, and prior decisions; decide through the existing approval capability if delegated | Core records the decision and the waiting turn's continuation |

Core must distinguish tool-level refusal from model prose. A failed
`subagent.start` receipt is direct evidence; the absence of such a call is not
proof of a runtime failure. If a parent merely says it cannot delegate, Butler
can inspect its settings and ask the parent for a concrete attempt before
changing a runtime. Capacity limits may call for waiting, while a spent budget
does not. A tool check, provider privacy rule, and project scope remain enforced
even when Butler chooses an action without an individual prompt.

Operational failures such as an unavailable subagent runtime, missing verified
tool capability, quota exhaustion, or a failed transport may justify a different
configured runtime. An explicit task or safety-policy refusal does not itself
authorize routing the same task to a more permissive model. Butler may explain
the refusal or seek a compliant path within the existing project policy.

## Example: harness subagent cannot start

1. Core saves the failed `subagent.start` receipt and parent turn identity.
2. Butler reads that exact attempt, parent chat settings, and eligible provider
   models. A listed model must pass the subagent tool check and project privacy
   policy; an enabled provider alone is insufficient.
3. Butler changes the **parent main chat's next-turn subagent choice** to an
   eligible configured provider, preserving other settings. The running turn
   retains its original snapshot.
4. Butler queues one parent follow-up with an idempotency key asking it to retry
   the same bounded task. It never messages a child directly.
5. Butler observes the queue result, new `subagent.start`, and child outcome.
   If no eligible provider exists or the retry fails, it reports the exact
   blocker and does not claim recovery.

## Product boundaries

- Only the selected project's existing, enabled profiles and capabilities are
  candidates. Butler does not enable a global provider or expand project scope.
- Browser-only actions require the requesting device and cannot run in Core's
  unattended worker. The standing project authority must clearly include or
  exclude actions that delete data, broaden execution scope, or decide pending
  approvals; it should not create a fresh operator prompt for every step.
- Core's saved action and outcome are authoritative. A model explanation, a
  settings save, and a queued follow-up are not proof that work succeeded.
- Automatic remediation uses the standing **Act** authority for the selected
  project, distinct from a one-off operator request. Turning Act off revokes
  dispatch before the next action; an in-flight action is recorded accurately.

## Planned focused verification

Select feature-specific Core tests for event capture, authority, capability
invocation, parent-only routing, idempotency, retry verification, and restart.
Select Butler Oversight component tests and one real-Core production journey in
desktop Chromium, mobile Chromium, and mobile WebKit at LAN origin. Record the
exact files, collected counts, exclusions, and diff digest in
`.github/test-selection.json` before running tests. Physical iPhone Voice is a
separate acceptance gate; the background worker must also function with the UI
closed.

## Act investigation lifecycle

When a running goal makes a scheduled check due, Core owns the project lease,
records the case, and lets Butler inspect the selected project's current work.
Core renews its 60-second lease while the investigation runs and cancels the
investigation if that lease is lost. No
background action can be treated as verified solely because a tool returned.

A periodic check that only reads live Core state can resolve when no idle main
goals require work. If no main goal is running and paused or blocked goals remain,
the check needs attention even after successful reads or unrelated changes.
A saved change without a confirmed downstream result needs attention;
a change whose side effects are unclear remains uncertain. After a restart,
read-only steps can be replayed, while a possible write is held for review.
The focused Core tests cover these outcomes, lease renewal and loss, and the
existing parent-only retry path. The live acceptance check must observe a
scheduled Act case on production Core with the UI closed.

## Concurrent checks and delegated investigations

Core starts one independent worker for every eligible recovery case. A slow
investigation does not block other due cases or project oversight. Existing
provider and harness capacity limits still apply. Each project keeps one lease
heartbeat; losing that lease or Act authority stops its workers. Live workers
are never treated as abandoned just because they run longer than 90 seconds.
After a restart, Core reattaches to the durable investigation turn and consumes
its result instead of starting a duplicate investigation.

Each case has a separate durable chat session, linked from Butler scheduled
activity through **Open investigation and subagents**. Provider and harness
transcripts, child results, and subsequent investigation rounds stay with that
case rather than entering the operator's Butler conversation.

Background checks inherit Butler's saved Subagents switch, selected provider or
harness child runtime, reasoning effort, and maximum active children. Disabled
Subagents stays disabled. These are Nebula-managed children; vendor-native
subagents remain disabled for background Butler. Parent workers may use the
normal start, wait, message, and stop tools for their own children. They must read
child results before concluding, and a child claim alone never verifies a repair.
Case-scoped Act, revision, privacy, and uncertain-action checks still apply.
