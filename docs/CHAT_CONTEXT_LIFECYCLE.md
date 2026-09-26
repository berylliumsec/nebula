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
| Tool output and artifacts | Core tool-call ledger and artifact store | Full retained output and artifact references | In a turn: recent results in full, older ones as receipts or checkpoint entries. In later turns: a bounded tool-activity block on each answer, with ids that read the output again |
| Working notes | Core `ChatWorkingNotes`, one per conversation | The assistant's own markdown notes (`notes.write`, at most 8 KiB), revision, time, and writing turn | The notes as a turn starts, after the operator's message; the latest notes inside each checkpoint written during the turn |

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
   the previous turn's raw tool transcript to the new request. B is sent with
   a **tool activity** data block rendered from its stored
   `metadata.tool_results`: for each step, the tool, status, what it acted on
   (`did`, the main argument, redacted, at most 120 characters), Core's result
   summary (at most 160 characters), its `tool_call_id`, and up to four
   artifact ids. The block is at most 2,500 bytes: every failed, denied or
   unsettled step first, then the most recent successful ones, in the order
   they ran, and a count of the rest. It is derived from stored metadata only,
   so it is the same bytes on every later request. The full output stays in
   the ledger and artifacts, and `tool_output.search` (by `tool_call_id`) or
   `tool_output.read` (by artifact id) reads it from any later turn of the
   same conversation, never from another conversation. The operator's
   transcript shows B's text alone.
4. If the A/B/C history becomes too large, `_model_context` replaces its older
   portion in the request with a sourced snapshot's memory, carried as a
   labeled block at the start of the first message kept verbatim, and can add
   matching original excerpts after the current message. Neither goes into
   the instructions. The stored A/B/C messages remain intact.

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
output request, and profile.

The estimate compared with the trigger counts everything the request will
carry: instructions and messages, the conversation's working notes, and for
a tool turn the function declarations (converted exactly as routing sends
them), the routing instructions, and a reserve for the largest on-demand
catalog picks the ranker could still add. Core estimates about three UTF-8 bytes per token. After each
turn whose last provider request reported at least 1,000 input tokens, the
conversation records `reported / estimated` for that provider profile and
model in `ChatSession.metadata.context_calibration`, smoothed (half the new
sample, half the previous factor) and bounded to 0.6–1.5, in the same session
write that saves the answer. Target decisions (compaction, in-turn clearing)
scale the estimate by that factor; hard capacity checks never scale it below
0.8 of the raw estimate, and after a provider context-length rejection the rest
of the turn uses the raw estimate. The calibration assumes `input_tokens` is
the provider's whole prompt, cached tokens included.

The context endpoint's `estimated_input_tokens` (the Workbench meter) uses the
same accounting: the latest turn's `reserved_input_tokens` for tools,
routing instructions and the working notes, scaled by `estimate_calibration`. It still excludes goal,
skill, and operator-decision instructions, retrieved help or knowledge, and
excerpts, which are known only when a turn is assembled. The last provider
request has a separate recorded estimate, with the provider's
`reported_input_tokens` and `reported_cached_input_tokens`.

When the estimate exceeds the 75% target, `_model_context`:

1. Refuses to proceed only if the current user message, required instructions
   and tool reserve exceed hard input capacity. A conversation with nothing
   durable to archive (a new chat) is sent unchanged while it fits capacity.
2. Reuses the latest ready snapshot, without a model call, when it covers
   exactly the stored messages up to its boundary (the same message IDs and
   the same canonical content hash) and every later message fits beside its
   memory.
3. Otherwise keeps a recent, complete tail beginning with a user message,
   initially budgeted at 40% of what the target leaves after instructions and
   reserve, while always retaining the current message, and summarizes the
   older canonical messages, including the selected context originally
   supplied with them, through `ContextCompactor`. The compactor's objective is
   the active goal's objective, or none: the latest message ("thanks") is no
   guide to what later turns served by the same memory will ask. The selected
   model produces structured memory with cited source references. Long source
   sets are chunked and reduced hierarchically; source references must
   validate.
4. Sends the memory as a leading block of the first kept message, labeled as
   derived history rather than instructions, and appends up to eight matching
   original transcript excerpts (as JSON data, within a fifth of the target and
   the room left) after the current message's own text and selected context.
   The conversation's working notes follow them, and within a turn the
   tool-history checkpoint follows those.
5. Never leaves a message out. Every canonical message in the active
   projection is either sent verbatim or covered by the snapshot that is sent.
   Over the target, the excerpts go first; then the compaction boundary moves
   forward past the messages that do not fit, and the archive is compacted
   again, at most three compactions per request. A request still above the
   target is sent when it fits input capacity (diagnostic
   `chat.context.over_target`); only a request above capacity fails. When the
   instructions, tools and current message alone exceed the target, input
   capacity is the goal, so turns do not compact again chasing a target no
   compaction can reach. If compacting past a later boundary fails, the
   boundary already compacted serves when it fits capacity.

The memory block is rendered from the snapshot alone, and the excerpts, which
change every turn, come last. So between compactions each request repeats the
previous request's instructions and messages byte for byte up to the previous
current message, and provider prefix caches keep hitting. The stored message
never contains its excerpts; the next turn replays it without them.

Snapshots are immutable, scoped to one chat session, and keyed for reuse by
canonical source content, provider/model, and prompt version. A ready snapshot
keeps serving while the later messages still fit and its covered messages match
it by ID and content. An edit (even in place) or retraction of a covered message
forces fresh compaction. A failed summarization records a failed snapshot and
returns an error; Core does not silently drop the old messages. The context API
exposes status, limits, source references, coverage, usage, and cost, but no
mutation route: `GET /api/v1/chat/sessions/{session_id}/context`.

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
  16 eligible unfolded steps, about 24,000 estimated tokens (the same
  `estimate_tokens` count every request estimate uses), or an explicit
  advance when a request crosses its target. The same checkpoint and replay
  rows remain stable between advances for provider prefix caching. The
  checkpoint (schema `nebula.chat-turn-checkpoint/v3`) contains a digest,
  covered step ranges/count, tool names, and one receipt per step:
  `[number, tool_index, state, did, summary, artifacts, failure]`. `did` is
  the call's main argument (command, path, query, URL and so on), redacted
  and at most 120 characters; `summary` is Core's result summary, at most 200
  characters; `artifacts` are the result's artifact references; `failure` is
  Core's classification of a failure and an `arguments_sha256` of the exact
  failed arguments. The receipts are bounded at 3% of the model's input
  capacity, never below 16 KiB or above 64 KiB (16 KiB up to about a
  182,000-token capacity, 64 KiB from about 728,000). Over the bound, older
  successful receipts are dropped first, then failed ones if necessary, and
  `omitted_steps` records the count. There is no separate token cap; the
  stored `token_estimate` is the checkpoint's own estimate. The hash and
  coverage prove which durable steps were folded; **they do not mean their
  individual findings are present in the model request**.
* **Older-result clearing.** If the checkpoint plus replay exceeds the target,
  `_with_tool_history` advances the checkpoint and then replaces the oldest
  full results still whole with short receipts, retaining call IDs/batch
  identity and, where available, artifact references, until the request is at
  a **watermark** below the target: `target − max(10% of input capacity,
  8,000 tokens)`, but never below half the target. A result once cleared stays
  cleared for the rest of the turn (Core recomputes this after a restart), so
  a request changes its earlier bytes only when it crosses the target again,
  not on every step. It keeps the newest result whole when hard capacity
  permits. A receipt directs the agent to `tool_output.search` or
  `tool_output.read` for the full retained output. If no artifact reference
  exists, a necessary result may require another tool call.

**Working notes.** Every provider turn with tools is offered `notes.write`,
which replaces the conversation's working notes (markdown, at most 8 KiB): the
assistant's own findings with exact identifiers, decisions and todo list. The
routing instructions ask for them on long or multi-step work. A checkpoint
written during the turn carries the latest notes as `working_notes`, outside
the receipts' byte bound, so notes written by a step that has since folded are
not lost; until then the `notes.write` call itself is replayed with its
arguments. When a turn starts, the conversation's current notes are appended
to the operator's message as a JSON data block, after the message's own
content and before any checkpoint, and stay the same bytes for the whole turn.
Notes that would push the request over hard input capacity are left out of it
and a `chat.working_notes.omitted` diagnostic is recorded. A context-length
recovery rebuild does not re-add them; the turn's notes calls and checkpoint
still carry them. The notes are derived memory: they are never placed in the
instructions and never grant permission or prove a tool effect. The context
API returns them as `working_notes` (`content`, `revision`, `updated_at`,
`turn_id`), or null when the assistant has never written notes. They are
deleted with the conversation; a fork starts without them.

This is a bounded replay strategy plus the agent's own notes, not a semantic
summary of the task. A long turn can have complete durable evidence yet lose
an early observation from the model-facing checkpoint and recent tail if the
agent did not note it. The agent must revisit canonical outputs, or rely on
notes that cite them, before drawing a conclusion that depends on them. The
checkpoint does not by itself establish that the final answer is correct or
incorrect.

For a confirmed provider context-length rejection *before a response delta or
tool effect*, Core may refresh exact model/route limits and retry the canonical
request once. The in-turn path can retry once with older results cleared, and
those results stay cleared for the rest of the turn. Completed tool effects
are never retried merely because a provider rejected a later request. A second
rejection becomes an actionable capacity failure.

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
   request's saved `last_provider_request` estimate and the provider's
   `reported_input_tokens` with the context endpoint's current estimate; the
   latter is not necessarily the historical request. The session's
   `context_calibration` shows the factor later estimates were scaled by.
3. For conversation compaction, inspect `context_snapshots` for status,
   `compacted_through`, source IDs/hash, model, prompt version, and usage. Check
   the original covered messages and any selected context before trusting a
   derived memory item.
4. For an uninterrupted turn, inspect `chat_turn_step_events` and
   `chat_turn_checkpoints`, compare covered ranges, `step_count`, `steps`,
   `omitted_steps`, and `working_notes`, then follow tool-call/artifact
   references for the exact outputs. A missing receipt in the model-facing
   checkpoint is distinct from a missing durable result. `chat.tool_history.cleared`
   diagnostics record each clearing event, its watermark and how many results
   it cleared.
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
* `src/nebula/v3/tool_activity.py`: step briefs and the tool-activity block
  a stored answer carries into later requests.
* `src/nebula/v3/working_notes.py`: `notes.write`, notes storage, and the
  notes data block.
* `src/nebula/v3/domain.py`: durable `ContextSnapshot` and memory contracts.
* `tests/v3/test_context.py`, `tests/v3/test_chat_context_assembly.py`,
  `tests/v3/test_turn_prompt_cache.py`, `tests/v3/test_in_turn_context_pruning.py`,
  and `tests/v3/test_tool_history_memory.py`: focused behavioral coverage.
  These tests prove particular contracts, not semantic completeness of a
  summary.
