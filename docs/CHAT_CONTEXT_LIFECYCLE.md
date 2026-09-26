# Conversation history and working context

This describes the provider-backed chat implementation at `main` commit
`9a983ee` as inspected on 2026-09-26. It distinguishes the durable record from
the input assembled for a particular model call. The
[state diagrams](design/assistant-context-state-machine.md)
map these rules to request and recovery states. The separate [design proposal](design/openrouter-native-harness.md)
contains future work; its unchecked items are not claims about current behavior.

## The four boundaries

| Boundary | Owner | What persists | What the next model call receives |
| --- | --- | --- | --- |
| Conversation | Core `ChatSession` and `ChatMessage` | Original, sequenced user/assistant messages, selected context attached to each turn, metadata, and retraction history | The active transcript reconstructed from stored messages, subject to conversation compaction |
| Provider turn | Core `ChatTurn` and append-only step ledger | Tool intent, result, status, latest step projection, and checkpoint rows | Conversation input plus this turn's replayable calls/results or compact receipts |
| Context snapshot | Core `ContextSnapshot` | Immutable derived memory, source IDs/sequences/hash, model, prompt version, usage, and success/failure | Memory and selected original excerpts when old conversation messages are archived from the request |
| Tool output and artifacts | Core tool-call ledger and artifact store | Full retained output and artifact references | Recent results in full; older results may be receipts or absent from a bounded checkpoint |

A **turn** is one user request and the assistant's response, potentially including
many provider calls and tool steps. A **provider call** is one request to the
selected model. A **checkpoint** compresses *tool history inside a turn*; a
**snapshot** summarizes *older conversation messages across turns*. Neither
rewrites the canonical transcript or turns a derived summary into evidence.

### What is sent, by example

1. The operator sends user message A. `prepare_async` selects a runtime,
   assembles instructions and A, checks capacity, and starts a durable turn.
2. The model calls a tool. Core records the step and result. On the next
   **provider call in the same turn**, `_with_tool_history` sends the same
   conversation input plus that turn's replayable tool call/result history.
3. The assistant finishes and Core saves assistant message B. The operator
   sends message C. `_merge_history` reconstructs A, B, C from canonical
   messages (and the selected context saved with A/C). It does **not** append
   the previous turn's raw tool transcript to the new request. Tool activity
   remains available in the ledger and artifacts. B may contain the assistant's
   account of what it learned, but that is not the same as replaying tool output.
4. If the A/B/C history becomes too large, `_model_context` replaces its older
   portion in the request with a sourced snapshot and can add matching original
   excerpts. The stored A/B/C messages remain intact.

Project instructions, current goal/plan/skills, unresolved questions, and
permissions are assembled from their current authorities for each applicable
turn. For example, the project-root `AGENTS.md` is read again rather than
trusted to a summary. A pending approval or recovery turn prevents a new user
turn before retrieval or compaction. A memory summary never grants permission,
resolves an operator question, or proves that a tool effect completed.

## Conversation compaction

`resolve_context_limits` sizes the selected provider/model request. Known
catalog and verified route limits, explicit profile caps, and a labeled safe
fallback feed one resolver. For verified OpenRouter routes, the smallest limits
across routes eligible for the required parameters apply. The fallback window is
8,192 tokens with a 2,048-token output allowance when reliable capacity is
unknown. This is an estimate, not a promise from the provider.

The default trigger is `floor(0.75 × input_capacity)`, where input capacity is
the resolved context window minus reserved output, also capped by any separate
input limit. The post-compaction target is `floor(0.60 × input_capacity)` for
the compactor's sizing policy. The effective values depend on model, route,
output request, and profile; the Workbench context meter is an estimate of saved
conversation and project instructions and excludes some material added during a
request. The last provider request has a separate recorded estimate.

When messages plus instructions exceed the 75% target, `_model_context`:

1. Refuses to proceed if the current user message and required instructions
   exceed hard input capacity, or if no durable older messages can be archived.
2. Keeps a recent, complete tail beginning with a user message. It initially
   budgets up to 40% of the target for this tail, while always retaining the
   current message.
3. Summarizes the older canonical messages, including the selected context
   originally supplied with them, through `ContextCompactor`. The selected
   model produces structured memory with cited source references. Long source
   sets are chunked and reduced hierarchically; source references must validate.
4. Adds that memory to the request instructions as derived history, and may
   append up to eight matching original transcript excerpts within the remaining
   excerpt budget. It tightens the tail until the assembled estimate fits.

Snapshots are immutable, scoped to one chat session, and keyed for reuse by
canonical source content, provider/model, and prompt version. A ready snapshot
can keep serving while the later messages still fit and its source IDs exactly
match the archived portion. An edit/retraction of covered messages forces fresh
compaction. A failed summarization records a failed snapshot and returns an
error; Core does not silently drop the old messages. The context API exposes
status, limits, source references, coverage, usage, and cost, but no mutation
route: `GET /api/v1/chat/sessions/{session_id}/context`.

This validation proves structural provenance and a bounded request. It cannot
prove that a model summary preserved every fact or the exact meaning of a long
source. For consequential details, consult the original message or artifact.

## Tool history within one uninterrupted turn

`ChatTurnLedger` folds append-only step events to the latest projection of each
step. Each follow-up model call receives the turn's replay, subject to two
bounded transformations:

* **Deterministic checkpoint.** The most recent eight provider response groups
  stay whole. Older completed, failed, or denied steps can enter a checkpoint;
  waiting approval/callback steps stay whole. The checkpoint advances after
  16 eligible unfolded steps, about 24,000 estimated tokens, or an explicit
  capacity-driven advance. The same checkpoint and replay rows remain stable
  between advances for provider prefix caching. The checkpoint contains a
  digest, covered step ranges/count, tool names, compact step receipts,
  summaries/artifact references, and classified failure facts. It is capped at
  16 KiB; its token estimate is capped at 4,000. When it exceeds the byte cap,
  older successful receipts are dropped first, then failed ones if necessary,
  and `omitted_steps` records the count. The hash and coverage prove which
  durable steps were folded; **they do not mean their individual findings are
  present in the model request**.
* **Older-result clearing.** If the checkpoint plus replay still exceeds the
  target, `_with_tool_history` replaces the fewest older full results with
  short receipts while retaining call IDs/batch identity and, where available,
  artifact references. It keeps the newest result whole when hard capacity
  permits. A receipt directs the agent to `tool_output.search` or
  `tool_output.read` for the full retained output. If no artifact reference
  exists, a necessary result may require another tool call.

This is a bounded replay strategy, not a semantic summary of the task. A long
turn can have complete durable evidence yet lose an early observation from the
model-facing checkpoint and recent tail. The agent must explicitly revisit
canonical outputs or maintain its own concise task findings before drawing a
conclusion that depends on them. The checkpoint does not by itself establish
that the final answer is correct or incorrect.

For a confirmed provider context-length rejection *before a response delta or
tool effect*, Core may refresh exact model/route limits and retry the canonical
request once. The in-turn path can retry once with older results cleared.
Completed tool effects are never retried merely because a provider rejected a
later request. A second rejection becomes an actionable capacity failure.

## Other runtimes and lifecycles

Provider-backed missions use the same `ContextCompactor` for model-facing
dependency context, but their sources and owner are mission run/task records,
not chat messages. The snapshot is scoped to that run. The chat turn checkpoint
algorithm above describes provider-backed chat turns, not every mission call.

Codex/Grok and other external harness chats own their own request assembly,
context limits, compaction, and continuation. Core stores their conversation and
activity for display and recovery, but its provider-chat `_model_context` and
`ChatTurnLedger` rules must not be inferred for the harness's private model
calls. The context endpoint reports `runtime_managed` and unknown capacity
unless the harness supplies authoritative limits. A harness subagent launched
inside a provider turn likewise has its own context boundary; its full output
does not automatically become a verbatim future conversation message.

Cancellation, reconnection, and restart do not promote a summary over durable
state. A provider turn with unresolved approval/callback or uncertain command
outcome remains governed by its pending/recovery record; a new turn is blocked
until it is resolved. Forks have separate session identity and lineage; edits
and retractions preserve canonical history while changing the active projection.
Provider/model switches are preflighted against the current session and limits;
a switch that requires compaction needs an explicit confirmation fingerprint.

## Inspecting a suspected loss of context

1. Identify the exact session, turn, provider/model, and whether the call was
   provider-backed or harness-managed. Avoid inferring the model's input from
   the browser transcript alone.
2. Read the canonical message sequence and active retractions. Compare the
   request's saved `last_provider_request` estimate with the context endpoint's
   current estimate; the latter is not necessarily the historical request.
3. For conversation compaction, inspect `context_snapshots` for status,
   `compacted_through`, source IDs/hash, model, prompt version, and usage. Check
   the original covered messages and any selected context before trusting a
   derived memory item.
4. For an uninterrupted turn, inspect `chat_turn_step_events` and
   `chat_turn_checkpoints`, compare covered ranges, `step_count`, `steps`, and
   `omitted_steps`, then follow tool-call/artifact references for the exact
   outputs. A missing receipt in the model-facing checkpoint is distinct from
   a missing durable result.
5. Correlate request/turn events with provider errors, retries, and restart
   recovery. Make no correctness claim from checkpoint counts alone; compare
   the answer against the relevant original evidence.

The immutable deployed checkout `a31328c` inspected on 2026-09-26 had **no
`context_snapshots`** in its sampled live database, but two very long provider
turns had thousands of tool steps and bounded turn checkpoints that omitted
many successful receipts. This is a point-in-time operational observation;
it does not establish that ordinary conversations never need compaction or that
all omitted findings were lost from every possible retrieval path.

## Code and tests

* `src/nebula/v3/chat.py`: `prepare_async`, `_merge_history`, `_model_context`,
  `_with_tool_history`, `context_status`, and runtime-switch preflight.
* `src/nebula/v3/context.py`: limit resolution, structured compaction,
  snapshot validation/reuse, and budget accounting.
* `src/nebula/v3/chat_turn_ledger.py`: append-only step reconstruction,
  checkpoint eligibility, contents, and size cap.
* `src/nebula/v3/domain.py`: durable `ContextSnapshot` and memory contracts.
* `tests/v3/test_context.py`, `tests/v3/test_api_stream_context.py`, and
  `tests/v3/test_turn_prompt_cache.py`: focused behavioral coverage. These
  tests prove particular contracts, not semantic completeness of a summary.
