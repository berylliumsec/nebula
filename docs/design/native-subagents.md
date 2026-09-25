# Native subagents for provider chats

Provider-backend chats (OpenRouter and other non-harness providers) can delegate
independent work to parallel subagents. Harness runtimes keep their vendor
subagents; this is the provider-neutral equivalent planned as G8 in
`openrouter-native-harness.md`. Harness chats can also delegate to a chosen
provider model (see the last section).

Mockup (approved September 18, 2026): Figma page "Subagents" in
https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=21-2

- `S1 · Desktop · Subagents pane open` (`22:2`)
- `S2 · Desktop · Pane closed (default)` (`24:178`)
- `S3 · Mobile 390 · Subagents sheet` (`25:2`)

Static mockups; names, steps and token counts are illustrative.

## Behaviour (backend, implemented)

- Opt-in per request with `allow_subagents` (default off). When set on a
  project provider chat, the tool loop advertises `start_subagent`,
  `wait_subagents`, `list_subagents`, `message_subagent` and `stop_subagent`.
  Subagent turns never receive them (depth 1).
- `start_subagent` creates a child `ChatSession` (`parent_session_id`,
  `metadata.subagent_id`) and a `ChatSubagent` record, then runs the child as an
  ordinary background provider turn with the parent's provider, model, command
  runtime and MCP servers, and exactly the SSH hosts the parent turn was given
  (none for "Nebula host only"; a later round takes the hosts of the turn that
  sent its message). It returns immediately, so children run in parallel
  despite one tool call per routing step. There is no limit on how many run
  unless the operator sets "Running at once" (1-100) in Assistant settings;
  `max_active_subagents` then refuses a start at that many as
  `capacity_reached` (wait for one to finish, then retry; the limit and the
  running count travel as `limit`), and the instructions state it too. A goal
  whose token budget is used up refuses a start as `budget_exhausted`.
  Retried steps reuse the same child (idempotency key), and the result
  carries its current status, so a retry after it finished does not read as
  running.
- `wait_subagents` (mode `all` or `any`) pauses the parent turn in
  `waiting_callback` and frees its provider slot. When the wait is satisfied,
  Core resumes it through the provider queue; the turn keeps
  `waiting_callback` through admission, so the wait step itself returns and its
  tool result carries each child's final answer as its report. A wait satisfied
  while the parent is still settling resumes it once the settle has freed its
  slot. Without ids and
  with nothing running it waits on the finished children whose reports the
  parent has not received. The resume check matches the subagent and
  agent-message tools by a contract version (`subagents-v1`,
  `agent-messages-v1`), not a hash of their specs, so a Core update that
  rewords them or adds a ToolSpec field resumes a parked parent; digests
  recorded before the versions read as version 1.
- Once the parent is idle, every finished child is posted to the parent
  conversation as an assistant message with `metadata.kind = "subagent_result"`,
  so later turns remember it. If the parent's goal is running and no children
  remain, Core continues the goal once with the operator's current turn
  settings, as every Core-started goal turn does: model, reasoning level,
  tools, MCP servers, SSH hosts, hooks, subagents and agent messaging. A
  failed, stopped or interrupted report, or a child's message, continues it
  at once. Losing that start to an operator message is not a failure and
  leaves the goal running. A running goal with no claimed or pending turn and
  no running subagent is continued (after a completed turn) or paused with the
  reason by Core's 15 s recovery pass.
- Reasoning level: `start_subagent` and `subagent.start` take an optional
  `reasoning_effort` (`none` to `xhigh`). Unset, a provider parent's child
  uses the conversation's current level (`metadata.reasoning_effort`, which
  the operator may change mid-response). A harness parent's child uses the
  harness session's level when a provider model takes it too; a vendor-only
  level leaves the model's default. The level is kept on
  `ChatSubagent.reasoning_effort` and returned by the start tools and the list
  API.
- Approval mode is inherited: commands follow the project's automation policy
  and a child always runs in its parent's project, so "always", "on boundary"
  and "never" (full auto-approval) apply to subagents exactly as to the parent.
  Pending child approvals stay on the child turn and are surfaced through the
  list API (`status: "waiting_approval"` with the exact command and rationale);
  approving and resuming the child turn continues it. A blocked child belongs
  to its supervising response; once none runs (it blocks while the parent is
  idle, or the parent's response ends without acting), it is stopped and its
  report names the approval it was waiting for.
- A parent response that fails or is stopped stops the children it started,
  and each report says why. That includes a parent whose satisfied wait could
  not resume (it fails and its goal pauses with the cause), an interrupted
  parent Core settles because it will never resume it (its goal was
  cancelled, completed or blocked, or its time budget is spent), and a harness
  turn that is stopped or fails. A completed parent keeps them (they report
  into the conversation), and so does a failed answer the operator can still
  retry; a parked or restart-interrupted parent is not ended. Stopping a
  subagent closes the parent messages it never read, so no new round starts
  from them; a round Core started in the meantime is stopped too, so when Stop
  returns none of its turns runs, and a round that had already finished keeps
  its report. After a restart, a child whose turn Core recovers stays
  `running` (the list API shows `recovering`) and resumes on its own; a child
  parked on an approval or a question keeps waiting; any other running child
  is marked `interrupted` and reported to its parent. Deleting a parent
  conversation removes its finished subagent conversations and is refused while
  a subagent is running. Editing a message in place stops the subagents its
  retracted replies started; their reports and messages are not posted into
  the edited conversation.
- Child token usage is charged to the parent's goal as it accrues, after each
  provider response of the child. One durable charge per (goal, child turn)
  records what was debited, so the settle and restart repairs add only the
  rest. The goal's remaining token budget bounds every child request, and a
  budget pause stops the goal's running children.

## Talking both ways

Parent and child exchange durable `ChatSubagentMessage` records
(`to_child` / `to_parent`, `pending` → `delivered` or `undelivered`).

- The parent sends with `message_subagent` (harness: `subagent.message`). A
  working child reads the message before its next step; a child paused on a
  question takes it as the answer and resumes; a finished child (any end
  state) runs another round in the same child conversation, which keeps its
  earlier work (`ChatSubagent.rounds`). A message that lands while the child
  writes its report starts the next round right after it, and that report
  reaches the parent as a message. The operator's running-at-once limit
  applies to a new round.
- Every subagent turn gets `message_parent` and `read_parent_messages`, so it
  always routes tools. `message_parent` with `wait_for_reply` pauses the child
  (`waiting_callback`, `subagent_wait.reply_to`) until the parent replies or
  cannot: a parent whose response has ended, or ends without replying, closes
  the question with that reason and the child continues on its own judgment.
  No child waits on an idle conversation.
- Delivery to a provider parent: a waiting `wait_subagents` returns as soon
  as a child sends a message or asks (and returns at once while a question is
  open); a working parent gets a `list_subagents` step Core adds before its
  next routing call (`delivered_by_core`, no budget spent); an idle parent gets
  the message posted to the conversation (`metadata.kind =
  "subagent_message"`) and, with a running goal, the goal continues at once.
  A child receives parent messages the same way, as a Core-added
  `read_parent_messages` step.
- Delivery to a harness parent: `subagent.wait` and `subagent.list` return
  messages; `subagent.start`/`message`/`stop` results carry unread `updates`;
  a running Codex or Claude turn is steered with a "Subagent update"; and
  whatever the harness has not received is prepended to its next prompt, once.
  Grok cannot be steered, so it relies on the other paths. What a prompt
  carries counts as received once the vendor accepted it (its `started`
  event), so a turn that fails to start, and its retry, keep it.
- Every delivery fits its bound: a provider tool result the 8 KiB
  model-delivery bound (a larger one would reach the model as a placeholder),
  a harness tool result 16,000 bytes (below Grok's 20,000-byte MCP output
  cap), a harness prompt or steer 40,000 characters. Only news travels: a
  message the parent has not read and a report it has not received; a wait on
  a subagent whose report arrived earlier says `report_received_earlier`
  instead of sending it again. What does not fit is flagged
  (`report_follows`, `messages_follow`) and waits; a working provider turn gets
  it in the Core-added steps before its next routing call, where a report or
  message too long for one result arrives in numbered parts (`report_part`,
  `part`), and a harness gets it from `subagent.list` or its next prompt.
  Nothing is marked received until it went out whole. A report is the
  child's final answer up to 20,000 characters; a longer one keeps its
  opening and its last 3,000 characters and says how much was cut between
  them, so the parent does not ask the child to send it again.

## Failures reach the parent

Every way a subagent ends reaches the parent model with the cause:

- A start failure is the `start_subagent` tool failure
  (`nebula.tool-failure/v1`, see `docs/TOOL_FAILURE_CONTRACT.md`): its
  category and next action, not Core's error text, on provider and harness
  parents alike. The running-at-once limit is `capacity_reached` with the
  limit as Core's numbers; an exhausted goal budget is `budget_exhausted`;
  a rule (turned off, depth, no provider model) is `permission_denied`; a
  bad argument is `invalid_arguments`; a start Core could not complete is
  `execution_failed`.
- A finished, failed, stopped or interrupted round is a report carrying
  `error`, `last_step`, `tool_failures` (failed or denied steps with their
  error, last eight) and `undelivered_messages` (parent messages it never
  read, with why). Posted result messages carry the same lines.
- Core's own failures fail the subagent instead of leaving it running: a
  settle that raises marks it `failed` with "Nebula could not record this
  subagent's result (…)"; a waiting turn (parent or child) that cannot resume
  fails visibly and its reports still post (a failed parent also stops its
  children and pauses its goal); a report that cannot be posted
  stays unreceived (`reported_at` unset) and reaches the parent before its
  next step or in its next harness prompt; a goal that cannot continue is
  paused with the cause.

API: `GET /chat/sessions/{id}/subagents`,
`POST /chat/sessions/{id}/subagents/{subagent_id}/stop`,
`POST /chat/sessions/{id}/subagents/stop`.

## UI (implemented, #459)

Composer strip `ChatSubagentRail`, docked `ChatSubagentPane` (sheet at ≤1100 px
and inside Terminal/Browser side panels), inline `ChatSubagentResultCard`, and a
"Subagents" toggle in Assistant settings. The list endpoint is polled every 2 s
while any subagent is active (`running`, `waiting_approval` or `recovering`),
and while a response that may delegate runs, backing off to 4 s until a first
child appears; a hidden page is not polled. It
reads a finished child's step count and last steps from the end of its ledger,
once per process, and loads neither its turn nor its history.

## Harness chats delegating to a provider model

Mockup (approved September 21, 2026): Figma page "Harness provider subagents"
in the same file (`108:2`): H1 settings popover `108:3`, H2 desktop pane
`108:215`, H3 mobile sheet `108:397`.

- A harness chat (Codex, Grok) opts in per conversation with "Provider
  subagents" in Assistant settings and picks one provider and model. The
  request carries `allow_subagents` with `subagent_provider_id` and
  `subagent_model`; Core keeps the choice in the conversation's
  `metadata.provider_subagent` and on the harness session.
- The model must have passed the tool check (`tools_verified_for`), the
  provider must be enabled, and a cloud provider must permit sensitive data.
  Local-only projects refuse cloud providers as usual. Turning the option on
  is the operator's consent to send the children's tool results to that
  provider, so children run with `allow_cloud_tool_results`.
- The harness gets five Nebula gateway tools: `subagent.start`,
  `subagent.wait`, `subagent.list`, `subagent.message` and `subagent.stop`,
  plus a developer
  instruction naming the model. The vendor catalog is fixed per connection,
  so changing the setting reopens the connection between turns
  (`subagent_binding_changed`); the external thread carries on.
- Children are the same `ChatSubagent` provider conversations as above, with
  `parent_backend: harness` and the chosen `provider_profile_id`/`model`. They
  run in the project with the harness session's MCP servers, Nebula's command
  runtime when the session has one, no SSH hosts, and the project approval
  policy. The optional running-at-once limit travels as
  `provider_subagent.max_active`; changing it reopens the connection so the
  instructions state it. Depth stays 1.
- The gateway serves one call per session and Codex times a Nebula tool out
  after 900 s (Grok after 6000 s, its `tool_timeout_sec` default), so
  `subagent.wait` blocks for at most `timeout_seconds` (default 300, max 600)
  and returns unfinished children in `still_running`. A wait whose harness
  turn ended meanwhile marks nothing received.
- When the harness turn ends, finished reports are posted as
  `subagent_result` messages. Reports the harness has not received through a
  wait (`reported_at` unset) are prepended to its next turn's prompt, once.
- Stopping the harness response stops the children it started. The existing
  rail, pane, approvals and result cards are reused; they name the subagent
  model because it differs from the chat's.
