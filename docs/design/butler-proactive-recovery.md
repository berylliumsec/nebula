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

The recovery loop has a bounded tool budget and one active case per exact
failure. Repeating the same failed operation without new evidence terminates
the case and surfaces a concrete blocker. Agent reports and tool output are
evidence to assess, never instructions that can enlarge Butler's authority.
The event detector, capability schemas, project authority, and verifier are
Core contracts. The model selects the remedy and may combine tools; it is not
limited to a table of preset responses.

The worker has two trigger classes. The first implementation covers these
Core signals:

1. **Immediate:** Core records a failed `subagent.start`, an agent attention
   report, or a blocked goal. The recovery worker consumes each saved event
   under the selected project's current Act policy.
2. **Periodic:** every 15 minutes while a watched goal is running, Core records
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
