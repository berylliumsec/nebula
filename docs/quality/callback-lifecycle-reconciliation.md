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
- The turn ledger (`ChatTurnLedger`; `ChatTurn.tool_history` for turns written
  before it, #559) owns provider-visible continuation history.
- `ChatSubagent` owns the child round shown to its supervisor and operator.
- Core owns reconciliation during normal execution, startup, reconnect, and
  periodic recovery; the browser only renders the resulting durable state.

Observable invariants:

1. A terminal callback producer cannot leave its `ToolCall` indefinitely
   `running` or its `ChatTurn` indefinitely `waiting_callback`. A producer
   `interrupted` by a Core stop or crash is terminal for the wait.
2. Process termination without a callback does not prove the external effect's
   outcome. Provider history records `side_effects: unknown` and
   `retry_safe: false`.
3. Recovery commits that receipt before queueing the provider continuation, so
   concurrent message delivery cannot leave the old tool call looking live.
   If an earlier release already settled the turn with that stale row, periodic
   reconciliation backfills the same terminal receipt without replaying work.
4. A successful or failed authoritative callback reaches the model as a valid
   `nebula.tool-result/v2` receipt with the posted summary and references to
   the posted output. A receipt that arrives after the wait settled is kept as
   `late_callback_receipt` on the call and reopens nothing.
5. A still-running producer remains waiting and is never classified early.
6. Startup and the normal runtime terminal notification converge on the same
   idempotent continuation path. The resumed turn keeps `waiting_callback`
   through provider admission, so the outcome is projected before routing.
7. Once the child turn settles, the existing subagent delivery path removes
   false running state and reports to an idle or waiting parent exactly once.
8. Operator Stop terminates the background commands holding the stopped turn's
   callback lease. The results key reaches only the process.

## Lifecycle coverage

- Discover/select: existing conversation and subagent rail.
- Stream/use: existing provider stream and tool result rendering.
- Background: required; the process receives a callback lease.
- Failure: required; producer terminal without callback becomes unknown.
- Retry: automatic reconciliation retries without replaying the invocation.
- Reconnect/refresh: durable Core state is re-read and rendered.
- Interrupt/stop: terminal and interrupted producer states use the same
  classifier; Stop ends the callback-bound commands.
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
