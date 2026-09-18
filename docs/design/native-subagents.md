# Native subagents for provider chats

Provider-backend chats (OpenRouter and other non-harness providers) can delegate
independent work to parallel subagents. Harness runtimes keep their vendor
subagents; this is the provider-neutral equivalent planned as G8 in
`openrouter-native-harness.md`.

Mockup (approved September 18, 2026): Figma page "Subagents" in
https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=21-2

- `S1 · Desktop · Subagents pane open` (`22:2`)
- `S2 · Desktop · Pane closed (default)` (`24:178`)
- `S3 · Mobile 390 · Subagents sheet` (`25:2`)

Static mockups; names, steps and token counts are illustrative.

## Behaviour (backend, implemented)

- Opt-in per request with `allow_subagents` (default off). When set on a
  project provider chat, the tool loop advertises `start_subagent`,
  `wait_subagents`, `list_subagents` and `stop_subagent`. Subagent turns never
  receive them (depth 1).
- `start_subagent` creates a child `ChatSession` (`parent_session_id`,
  `metadata.subagent_id`) and a `ChatSubagent` record, then runs the child as an
  ordinary background provider turn with the parent's provider, model, command
  runtime and MCP servers. It returns immediately, so children run in parallel
  despite one tool call per routing step. Limits: 3 running per conversation,
  6 per parent response. Retried steps reuse the same child (idempotency key).
- `wait_subagents` (mode `all` or `any`) pauses the parent turn in
  `waiting_callback`. When the wait is satisfied, Core resumes it and the tool
  result carries each child's final answer as its report.
- Once the parent is idle, every finished child is posted to the parent
  conversation as an assistant message with `metadata.kind = "subagent_result"`,
  so later turns remember it. If the parent's goal is running and no children
  remain, Core continues the goal once.
- Approval mode is inherited: commands follow the project's automation policy
  and a child always runs in its parent's project, so "always", "on boundary"
  and "never" (full auto-approval) apply to subagents exactly as to the parent.
  Pending child approvals stay on the child turn and are surfaced through the
  list API (`status: "waiting_approval"` with the exact command and rationale);
  approving and resuming the child turn continues it.
- Stopping a parent response stops the children it started. Restart marks
  running children `interrupted` without resuming anything. Deleting a parent
  conversation removes its finished subagent conversations and is refused while
  a subagent is running.
- Child token usage is charged to the parent's goal when one exists.

API: `GET /chat/sessions/{id}/subagents`,
`POST /chat/sessions/{id}/subagents/{subagent_id}/stop`,
`POST /chat/sessions/{id}/subagents/stop`.

## UI (next increment)

Composer strip `ChatSubagentRail`, docked `ChatSubagentPane` (sheet at ≤1100 px
and inside Terminal/Browser side panels), inline `ChatSubagentResultCard`, and an
"Allow subagents" toggle in Assistant settings. Poll the list endpoint every 2 s
while any subagent is active.
