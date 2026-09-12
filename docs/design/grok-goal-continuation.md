# Grok goal continuation contract

## Operator journey

Entry point: select a healthy Grok ACP harness in Sessions, enter
`/goal <objective>`, and observe the goal continue without repeatedly asking the
operator to submit the next turn.

Core owns the durable harness turn and connection lifecycle. The Grok session
owns the goal objective and status. The UI renders persisted Core activity and
does not infer completion from assistant prose or an ACP `end_turn` response.

## Observable invariants

- A Grok goal whose latest validated status is `running` starts another vendor
  turn in the same session after ACP ends the current vendor turn.
- Assistant text such as a list of next steps does not complete an active goal.
- `complete`, `paused`, `usage_limited`, `budget_limited`, and failure states stop
  automatic continuation.
- A turn without a validated Grok goal update is not treated as a monitored goal.
- Approvals remain visible and block the active continuation until decided.
- Interrupting the durable Nebula turn cancels the active Grok turn and prevents
  another continuation.
- Activity from every continuation is persisted under the same Nebula turn, so
  detach, reconnect, and refresh recover one authoritative workflow.

## Lifecycle and test layers

Discover/select are unchanged. Create and use occur through `/goal <objective>`.
Streaming, approval, interruption, background ownership, reconnect, and refresh
continue through the existing durable harness-turn path. Pause, usage limits,
budget limits, completion, and failures are terminal for automatic continuation;
`/goal resume` is the explicit recovery action for a paused goal. Clearing and
deleting a goal remain vendor commands.

The adapter regression layer covers repeated vendor turns and every stopping
state. Existing selected real-Core session coverage owns persistence and
reconnection; the production browser matrix is required before the workflow is
claimed complete.
