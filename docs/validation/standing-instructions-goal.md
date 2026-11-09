# Durable goal: standing-instruction continuity

## Complete requested goal and acceptance criteria

**Eliminate duplicate standing instructions while preserving them through compaction**

Investigate whether Nebula repeatedly appends standing instructions to retained
conversation context. Modify delivery so each active instruction version appears
once in the model's effective context and remains authoritative after compaction,
reconnect, restart, and goal resumption.

Do not assume repeated transmission means duplication: stateless provider
requests may require instructions on every request. The ACP adapter already
tracks delivery per connection; verify the actual affected paths before changing
behavior.

Requirements:

- Trace instruction assembly and delivery across affected providers and
  harnesses. Identify the duplication's root cause and have a separate subagent
  validate it before implementing the fix.
- For stateful sessions, send unchanged standing instructions once per retained
  context. Persist delivery state where needed, scoped to the actual provider
  session and instruction version.
- Preserve the full current instructions across compaction using a durable
  instruction channel or deterministic reinjection. A lossy conversation summary
  must not be their only surviving copy.
- Apply instruction edits predictably, superseding outdated versions without
  accumulating copies.
- Handle retries, rejected or cancelled requests, context resets, reconnects,
  and concurrent turns without dropping instructions or marking failed delivery
  successful.
- Preserve required instruction delivery for stateless providers.

Validate initial delivery, subsequent turns, instruction updates, failed
delivery, reconnect/restart, goal resumption, and forced compaction. Inspect
actual assembled requests or harness events to demonstrate that instructions
remain present and authoritative without accumulating duplicates.

Follow repository troubleshooting, product-quality, focused-test, and PR rules.
Deliver the fix and a reviewable PR with evidence and any remaining verification
gaps and merge.

**Compaction continuity:** Save this complete goal and acceptance criteria in the
durable goal record. Maintain a checkpoint containing the confirmed root cause,
affected paths, implementation decisions, completed validation, and remaining
work. After compaction or resumption, reload that record and checkpoint before
continuing.

## Execution context

- Isolated branch: `codex/standing-instructions-continuity`.
- Initial baseline: `5fbe697f`, the active integration branch checkpoint.
- Current reviewed integration base after rebase: `698d82a0`.
- Work is repository code and disposable fixtures. Existing installations and
  live provider services are outside scope.
- The requested reviewable PR is to be integrated through the active integration
  branch after focused checks; do not merge unverified work into a release.
- Provider tools context is a separate concurrent workstream. Coordinate shared
  files and preserve its provider-only/native-harness boundary.
