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
   Nebula operator-help articles and, when the request includes project
   knowledge, project document chunks retrieved for A travel as a labeled
   reference block appended to A in the request, after A's own text and
   selected context: operator help as Nebula's own product documentation,
   project knowledge as untrusted JSON data, neither as instructions. Every
   provider call of the turn, and a resume of it, sends the same block; the
   stored A never contains it.
2. The model calls a tool. Core records the step and result. On the next
   **provider call in the same turn**, `_with_tool_history` sends the same
   conversation input plus that turn's replayable tool call/result history.
3. The assistant finishes and Core saves assistant message B. The operator
   sends message C. `_merge_history` reconstructs A, B, C from canonical
   messages (and the selected context saved with A/C). A is replayed without
   its reference block; C carries its own. It does **not** append
   the previous turn's raw tool transcript to the new request. B is sent with
   a **tool activity** data block rendered from its stored
   `metadata.tool_results`: for each step, the tool, status, what it acted on
   (`did`, the main argument, redacted, at most 120 characters), Core's result
   summary (at most 160 characters, left out when it only lists the result's
   keys), its `tool_call_id`, and up to four artifact ids. The block is at
   most 2,500 bytes. It keeps every failed, denied or unsettled step first,
   then successful steps whose output only their artifact ids reach again
   (a command's output), then other successful ones, the most recent of each
   first; it lists them in the order they ran and counts the rest. It is
   derived from stored metadata only, so it is the same bytes on every later
   request. The full output stays in
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
input limit. `compacted_input_target`, `floor(0.60 × input_capacity)`, is not a
size the assembled request is reduced to: it only sizes the compactor's output
allowance (see [The compactor](#the-compactor)). The effective values depend on
model, route, output request, and profile.

The estimate compared with the trigger counts everything the request will
carry: instructions and messages (the current message's reference block
included), the conversation's working notes, and for
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
   model produces structured memory: lists whose every item cites the
   canonical messages that support it, and a short free-text summary (see
   [The compactor](#the-compactor)).
4. Sends the memory as a leading block of the first kept message, labeled as
   derived history rather than instructions, and appends up to eight matching
   original transcript excerpts (as JSON data, within a fifth of the target and
   the room left) after the current message's own text, selected context and
   reference block. Excerpts are matched to the operator's words, not to the
   reference block. The conversation's working notes follow them, and within
   a turn the tool-history checkpoint follows those.
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

The memory block is rendered from the snapshot alone, and what can change
every turn (the reference block, the excerpts, then the working notes) comes
last. Nothing retrieved for one message enters the instructions. So between
compactions each request repeats the previous request's instructions and
messages byte for byte up to the previous current message, and provider prefix
caches keep hitting. The stored message never contains any of these blocks;
the next turn replays it without them. One exception stays in the instructions: when a tool
step failed, final synthesis adds operator help matching the observed failure
to that request's instructions, which already differ from routing's.

Snapshots are immutable, scoped to one chat session, and keyed for reuse by
canonical source content, provider/model, and prompt version. A ready snapshot
keeps serving while the later messages still fit and its covered messages match
it by ID and content. An edit (even in place) or retraction of a covered message
forces fresh compaction. A summarization problem degrades the memory rather than
failing the turn (see [The compactor](#the-compactor)); only capacity and budget
errors record a failed snapshot and return an error, and Core never silently
drops the old messages. The context API exposes status, the served snapshot's
`quality`, limits, source references, coverage, usage, and cost, but no
mutation route: `GET /api/v1/chat/sessions/{session_id}/context`.

### The compactor

`ContextCompactor` is shared by chat and provider-backed missions. Its prompt
(`CONTEXT_PROMPT_VERSION = "nebula-context-v2"`) is fixed instructions plus a
bare JSON schema; both are byte-identical on every call so a provider's prompt
cache can serve them. It asks, in this order, for the operator's requests (in
order, close to verbatim), the current state and next step, corrections,
constraints, decisions with reasons, confirmed facts, attempts and their
outcomes (failures included), exact references (paths, URLs, hosts, commands,
IDs), open questions, and last a summary of at most about 200 words, so an
answer the output limit cuts off loses the least. Pleasantries, superseded
plans, and bulky output already referenced by an ID are dropped. Source text is
presented as data, never as instructions. The request states its answer limit
(`answer_limit_tokens`). The full guidance may take at most half of the input
capacity; a smaller model (roughly under 3,000 input tokens) gets a brief form.

Each source is sent as a short id and its text (`m12` for chat message 12), and
the model cites those ids; Core maps them back to canonical references. A
whole reference with its UUID cost about 35 output tokens per citation, which
cut memories off on small allowances. The objective (the active goal's, or none)
only tells the model what the work is for; the model never writes or answers
it, and Core copies it into the memory. When no objective is supplied, none is
sent and none is reserved.

**Sizing.** A compactor call may write
`min(max_output_tokens, max(1,024, floor(0.05 × compacted_input_target)))`
tokens, lowered further only so that two memories still fit one roll-up request
on a small window. On the 8,192-token fallback window that is 1,024 tokens
(previously 184). Every request stays within the model's input capacity; the
instructions, schema, and objective are reserved first, and 60% of the remainder
is the budget for one group of sources. Long source sets are split into groups
greedily from the start. Each group becomes a memory; memories that fit the
allowance together are unioned mechanically (no call, nothing lost), and only
memories that must be compressed are rolled up by the model. A model roll-up
that keeps fewer than half the items the trimmed union would keep is replaced
by that union. If no two memories fit one roll-up request, they are unioned
and trimmed to the allowance. Trimming drops the oldest items of whichever list
is longest for its importance, so references, attempts and facts give way
before decisions, and operator requests, constraints and corrections last.

**Validation and repair.** Every list item must cite only the sources of its
request, and every strong identifier in its text (URL, CVE, UUID, IPv4 address
and port, long hex string, rooted or path-like multi-segment path, file name
with a known extension) must appear whole in the original text of the sources
it cites. For a roll-up, that is the original messages its items cite, never a
derived summary. Identifiers in the free-text summary must appear in some
source. Evidence and artifact IDs must be named by the sources and exist in
the project. An answer that fails any check, is not JSON, or was cut off by the
output limit gets one repair request that names the problems (or asks for a
shorter answer). After that, invalid items are dropped and counted, an
unverified summary identifier becomes `[unverified]`, a cut-off answer keeps
every complete item before the cut, and the rest is used. An answer with items
but no summary (the summary is written last, so a cut-off answer loses it
first) gets one composed from its first operator request and latest current
state.

**Degraded, not blocked.** If the provider call fails, or neither answer holds
a usable memory, that group's memory is a deterministic extract of the original
messages: each operator request (up to 240 characters), an excerpt of the
latest reply, and the exact identifiers the messages contain, each citing its
message, under a summary that says summarisation failed. After one such
failure the rest of that compaction is deterministic rather than calling a
failing model again. The snapshot is still written `ready`, so the turn
continues. Its `quality` records the outcome: `complete`, `salvaged` (items
dropped; `dropped_items` counts them), or `degraded` (an extract). A degraded
snapshot is served until the next compaction, which asks the model again.
Capacity errors (the current message or a minimal compaction cannot fit the
window) and goal/mission budget errors still fail the request and record a
`failed` snapshot; they need the operator to act.

**Incremental cost.** Each summarised leaf group is also stored as a
`ContextSegment` owned by the session (or mission run), keyed by a digest of its
exact sources, provider profile, model, prompt, objective, and output allowance.
Because groups are formed greedily from the start of an append-only archive,
the next compaction finds the earlier groups unchanged and reuses their
memories. Only new or changed groups (and any roll-up that must compress) are
summarised again. `segment_count` and `reused_segments` on the snapshot show
how much was reused. Segments are derived like snapshots and are deleted with
their owner.

These checks prove structural provenance, identifier faithfulness, and a
bounded request. They cannot prove that a model summary preserved every fact or
the exact meaning of a long source. For consequential details, consult the
original message or artifact.

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
  characters (empty when it only lists the result's keys), and for a lookup
  (`workspace.read`/`search`, `tool_output.read`/`search`,
  `conversation.search`) also `found` and up to six identifiers its output
  named (codes, paths, hosts, addresses, URLs, ids); `artifacts` are
  the result's artifact references; `failure` is Core's classification of a
  failure and an `arguments_sha256` of the exact failed arguments. The
  receipts are bounded at 3% of the model's input capacity, never below
  16 KiB or above 64 KiB (16 KiB up to about a 182,000-token capacity,
  64 KiB from about 728,000). Over the bound, older
  successful receipts are dropped first, then failed ones if necessary, and
  `omitted_steps` records the count. There is no separate token cap; the
  stored `token_estimate` is the checkpoint's own estimate. The hash and
  coverage prove which durable steps were folded; **they do not mean their
  individual findings are present in the model request** (the progress
  memory below carries findings).
* **Older-result clearing.** If the checkpoint plus replay exceeds the target,
  `_with_tool_history` advances the checkpoint and then replaces the oldest
  full results still whole with short receipts, retaining call IDs/batch
  identity and, where available, artifact references, until the request is at
  a **watermark** below the target: `target − max(10% of input capacity,
  8,000 tokens)`, but never below half the target. A result once cleared stays
  cleared for the rest of the turn (Core recomputes this after a restart), so
  a request changes its earlier bytes only when it crosses the target again,
  not on every step. What the model looked up this turn (`workspace.read`,
  `workspace.search`, `tool_output.read`, `tool_output.search`,
  `conversation.search`) is cleared only after every other older result, since
  a cleared lookup reads as an unanswered one, and a cleared lookup's receipt
  lists what it found the same way. It keeps the newest result whole when
  hard capacity permits. A receipt directs the agent to `tool_output.search` or
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
recovery rebuild before any tool ran does not re-add them (a mid-turn
compaction does, as they are then); the turn's notes calls and checkpoint
still carry them. The notes are derived memory: they are never placed in the
instructions and never grant permission or prove a tool effect. The context
API returns them as `working_notes` (`content`, `revision`, `updated_at`,
`turn_id`), or null when the assistant has never written notes. They are
deleted with the conversation; a fork starts without them.

**Progress memory.** Receipts say what each folded step did, not what it
found. Before each routing request and the final answer, Core checks the steps
a checkpoint may fold (outside the recent eight response groups and not
waiting). Once their arguments and results that the progress memory does not
yet cover reach about 8,000 estimated tokens, or a quarter of the input
capacity on a smaller model (`turn_progress.digest_trigger`; replayed reasoning
is not counted), the turn's own model summarises them with `ContextCompactor`
(owner type `chat_turn`, see [The compactor](#the-compactor)). Each step is a
source cited as `t<step>`, given as its tool and status, its `did` brief,
Core's summary, and the output the model was sent (the result's summary and
status first, redacted), whole up to 4,000 characters and otherwise its head
and last 1,000 characters. The result is structured memory (findings,
attempts and outcomes, current state, exact references), each item validated
against the steps it cites. It is incremental: the previous memory stands for
the steps it covers, only steps folded since are summarised, and the two are
unioned (or rolled up by the model when they outgrow the allowance). The
objective is the turn's goal, or else the operator's request of this turn,
presented as context. When a request fits only by folding all but its newest
steps (`_fold_deeper`, before routing on or answering), the steps that fold is
about to move are summarised first, so that checkpoint carries their memory
too.

A refresh runs beside the routing loop instead of holding it (nothing needs
the memory before the checkpoint next advances), one at a time per turn; a
deeper fold waits for it, and the final answer waits for one still running so
its usage is accounted.

The memory is stored as a `ContextSnapshot` owned by the turn and is only
carried by the next checkpoint the turn writes, as `progress` (schema
`nebula.turn-progress/v1`: covered step ranges, `quality`, the memory with
each item's steps named, and a note that it is derived), trimmed by importance
to a tenth of the input capacity (at least 256 estimated tokens). Only a
memory whose steps the checkpoint folds is carried. So the checkpoint block,
progress included, still changes only when the checkpoint advances, and
provider prefix caches keep hitting between advances. The summary calls are
added to the turn's `context_usage` and charged to the goal (or the parent
goal of a subagent) like conversation compaction; a goal already at its budget
skips them. A failure (capacity, budget, or a provider error the compactor
cannot absorb) records `chat.turn_progress.caught_failure_001` and leaves the
checkpoint with its receipts; the turn continues, and another attempt waits
until as much new output has folded again. Turn memories and their reusable
segments are deleted with the conversation.

This is a bounded replay strategy plus derived progress memory and the
agent's own notes. A long turn can still lose an early observation from the
model-facing request if it fell outside a step's excerpt or the memory's
allowance. The agent must revisit canonical outputs, or rely on memory and
notes that cite them, before drawing a conclusion that depends on them. The
checkpoint does not by itself establish that the final answer is correct or
incorrect.

**Mid-turn conversation compaction.** Clearing shrinks only the tool history.
When a request still cannot fit, Core compacts the *conversation* ahead of it
in the running turn, as it would between turns, rather than stopping the work:
`_compact_mid_turn` reserves everything else the request carries (the
instructions around the conversation, function declarations, the replayed
results, the checkpoint, and the current working notes) plus the same headroom
below the target that clearing leaves. It then compacts the canonical
conversation afresh into what is left (`_model_context` with
`reuse_snapshot=False`; the input capacity is the goal when even the current
message does not fit the target). The turn's ledger, checkpoint and replay are
sent exactly as before, so no step leaves the request and no tool runs again.
The compacted conversation (with the notes as they are now) becomes the turn's
request from then on: it is recorded in the turn's request snapshot, so later
steps and a resumed turn extend it and keep their prefix cache, and
`chat.context.midturn_compacted` records the event. A tool turn whose older
messages are now served by a snapshot is also offered `conversation.search`.
Compaction usage is charged to the turn's goal like any other compaction. Each
cause is tried once per step:

* **Routing that no longer fits** (`context_full`): even with every result
  cleared, the request is over input capacity. After compaction routing starts
  the same step again. When the replayed steps themselves are what no longer
  fits (clearing replaces outputs, not the calls or the reasoning each replays),
  every step but the newest response group folds into the checkpoint
  (`chat.tool_history.folded`), and routing goes on from its receipts and the
  notes. Only when neither makes room does routing stop and the turn answer
  from what it gathered (`chat.routing.context_full`).
* **The final answer.** The synthesis request must fit, or the turn's work is
  lost. The runbook help retrieved after failed steps is dropped first, then
  the conversation is compacted, then every step but the newest, and finally
  that one too, folds into the checkpoint. Only a request whose current
  message, instructions and checkpoint still cannot fit fails, with an
  actionable error that says its tool results are saved.
* **A provider context-length rejection** after tool routing began, when
  clearing could not shrink the request, or cleared it and the provider still
  refused. Every result but the newest stays cleared, and the conversation is
  compacted for one more retry.

For a confirmed provider context-length rejection *before a response delta or
tool effect*, Core may refresh exact model/route limits and retry the canonical
request once. Within a tool turn the first retry clears older results (they
stay cleared for the rest of the turn), and a request clearing cannot fix, or
a cleared retry the provider refuses too, is retried once more with the
conversation compacted. Completed tool effects are never retried merely because
a provider rejected a later request. A further rejection becomes an actionable
capacity failure.

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
   `quality`, `dropped_items`, `compacted_through`, source IDs/hash, model,
   prompt version, usage, and `segment_count`/`reused_segments`
   (`context_segments` holds the reusable leaf memories). A `degraded` snapshot
   is an extract, not a summary. Check the original covered messages and any
   selected context before trusting a derived memory item.
4. For an uninterrupted turn, inspect `chat_turn_step_events` and
   `chat_turn_checkpoints`, compare covered ranges, `step_count`, `steps`,
   `omitted_steps`, and `working_notes`, then follow tool-call/artifact
   references for the exact outputs. A missing receipt in the model-facing
   checkpoint is distinct from a missing durable result. `chat.tool_history.cleared`
   diagnostics record each clearing event: results cleared then (`count`),
   cleared in that request (`dropped_count`), replayed (`item_count`), and
   the watermark (`limit`).
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
* `src/nebula/v3/context.py`: limit resolution, the compactor prompt and
  sizing, validation/repair/salvage, the extractive fallback, leaf-segment reuse,
  snapshot reuse, and budget accounting.
* `src/nebula/v3/chat_turn_ledger.py`: append-only step reconstruction,
  checkpoint eligibility, contents, and size cap.
* `src/nebula/v3/turn_progress.py`: when a turn's folded steps are summarised,
  what each step contributes, and how the checkpoint carries the memory.
* `src/nebula/v3/tool_activity.py`: step briefs and the tool-activity block
  a stored answer carries into later requests.
* `src/nebula/v3/working_notes.py`: `notes.write`, notes storage, and the
  notes data block.
* `src/nebula/v3/domain.py`: durable `ContextSnapshot`, `ContextSegment`, and
  memory contracts.
* `tests/v3/test_context.py`, `tests/v3/test_chat_context_assembly.py`,
  `tests/v3/test_turn_prompt_cache.py`, `tests/v3/test_in_turn_context_pruning.py`,
  `tests/v3/test_tool_history_memory.py`, and `tests/v3/test_turn_progress.py`:
  focused behavioral coverage. These tests prove particular contracts, not
  semantic completeness of a summary.

## Measuring retention

`scripts/context_retention_eval.py` measures what a model still knows after
context management has run, which the contract tests above cannot show. It
plants exact values (ticket codes, paths, ports, names, one later corrected)
in a real conversation, forces compaction and checkpoint folding with a small
configured window, asks for the values back and scores the replies by exact
match; no model judges another.

| Scenario | Exercises | Scored |
| --- | --- | --- |
| `s1` | Sixteen turns of status notes with twelve facts in filler, 12K window: several conversation compactions | Recall of all twelve in one tool-free probe turn (a superseded port answered alone counts as `stale`), then again with tools on (`recall_retry`, for example through `conversation.search`) |
| `s2` | One tool turn following a 32-file `workspace.read` chain, 16K window: result clearing and checkpoint folding | Recall of every file's key; repeated reads and `tool_output.*` re-fetches |
| `s3` | Four files read in turn 1, asked about in later turns | Recall with tools off (what crosses the turn boundary) and with tools on, plus re-fetches |

Each report also records compactions (from the context endpoint, or every
`context_snapshots` row when the store is readable), failed compactions,
snapshot quality with dropped items and reused segments, compaction latency
(the wait before a compacting turn started), sends Core refused before a turn
started (the harness resends up to twice, as an operator would), in-turn
checkpoint rows,
re-reads of planted files and `tool_output.*` re-fetches (separately, those
naming an earlier turn's output), `conversation.search` and `notes.write` calls
and the working notes' size, provider-reported input with cache reads and
writes, the ratio of reported input to Core's estimate and Core's
`estimate_calibration`, cost from Core's model catalog prices, and wall time.
Results are mean (min) across `--repeat` runs with a fixed seed.

Run it only against a scratch Core. It creates projects and provider profiles
and switches its tool-using projects to host execution with approvals off,
so it refuses a Core holding projects it did not create unless
`--allow-existing-data` is passed:

```sh
.venv/bin/python scripts/context_retention_eval.py \
  --serve-from <checkout> --data-dir <fresh dir> --port <free port> \
  --repeat 2 --label <commit> --out <report prefix>
```

`--base-url`/`--token` evaluate a Core that is already running. The default
model is `deepseek/deepseek-v4.1-flash` through an OpenRouter profile with
`secret_ref: env:OPEN_ROUTER_API_KEY`; when that variable is absent, the served
Core starts through `bash -ic` so the shell profile supplies it. The script
prints an upper-bound cost estimate first and stops at `--max-cost-usd`
(default $1). It writes `<prefix>.json` and a Markdown table `<prefix>.md`.

Recall on one model and seed is evidence about that configuration, not a
guarantee: compare builds with the same model, seed, windows, repeat count
and harness (reports record its sha256), and read the per-probe table and
failed turns before trusting a mean. `s2` tends to be all or nothing per run
(the model either keeps its findings or loses them to clearing), so compare it
over at least five repeats (`--scenarios s2 --repeat 5`).
