# Callback lifecycle reconciliation

## Operator journey

From the conversation list, the operator opens or reconnects to a conversation
whose provider child launched a background command. If that command terminates
without posting its required callback, the conversation must stop presenting
the tool, turn, and subagent as running. Nebula resumes the durable turn,
reports the callback loss as an unknown effect, refuses automatic replay, and
settles the child report through the existing supervisor path.

## State authorities and invariants

- `CommandExecution` owns producer liveness and terminal process status.
- `ToolCall` owns the durable invocation status and result.
- `ChatTurn.tool_history` owns provider-visible continuation history.
- `ChatSubagent` owns the child round shown to its supervisor and operator.
- Core owns reconciliation during normal execution, startup, reconnect, and
  periodic recovery; the browser only renders the resulting durable state.

Observable invariants:

1. A terminal callback producer cannot leave its `ToolCall` indefinitely
   `running` or its `ChatTurn` indefinitely `waiting_callback`.
2. Process termination without a callback does not prove the external effect's
   outcome. Provider history records `side_effects: unknown` and
   `retry_safe: false`.
3. Recovery commits that receipt before queueing the provider continuation, so
   concurrent message delivery cannot leave the old tool call looking live.
   If an earlier release already settled the turn with that stale row, periodic
   reconciliation backfills the same terminal receipt without replaying work.
4. A successful or failed authoritative callback retains the existing callback
   completion path.
5. A still-running producer remains waiting and is never classified early.
5. Startup and the normal runtime terminal notification converge on the same
   idempotent continuation path.
6. Once the child turn settles, the existing subagent delivery path removes
   false running state and reports to an idle or waiting parent exactly once.

## Lifecycle coverage

- Discover/select: existing conversation and subagent rail.
- Stream/use: existing provider stream and tool result rendering.
- Background: required; the process receives a callback lease.
- Failure: required; producer terminal without callback becomes unknown.
- Retry: automatic reconciliation retries without replaying the invocation.
- Reconnect/refresh: durable Core state is re-read and rendered.
- Interrupt/stop: existing terminal producer states use the same classifier.
- Fork/delete/revoke: not changed by this defect.

## Planned proof

- Focused Python regression for terminal producer notification, periodic/startup
  reconciliation, bounded unknown-effect provider history, and durable ToolCall
  settlement.
- Existing focused subagent lifecycle regression to prove terminal child
  settlement and parent delivery behavior remains intact.
- Production build and immutable Core deployment.
- Authenticated live verification that the known stale callback tree no longer
  remains `running`/`waiting_callback` after deployment.
