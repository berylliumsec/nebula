# Durable producer journal

Versioned execution workers persist provider stream frames before exposing them.
Replaceable APIs read that journal through the existing authenticated
`GET /api/v1/chat/turns/{turn_id}/events` route. The cursor is the SQL run-event
sequence and the epoch is `durable-v1`; it does not reset when an API or project
owner changes. Frames use the existing event-retention policy.

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Publish output | Every visible frame is committed first | Exact project lease and SQL journal | Actual inert producer; independent reader |
| Replace API | The producer keeps running and acknowledged text is not repeated | Execution process plus durable cursor | Retained processes on SQLite/PostgreSQL |
| Reconnect | Later frames retain one epoch and increasing sequences | SQL journal | Separate API instances and cursor checks |
| Resume a paused turn | A POST follower starts at its new segment; explicit cursor replay retains history | Captured segment start | Prefix and pause regressions |
| Import legacy content | Saved content is seeded once before new output | History observed before admission frames | Pre-journal and already-journaled cases |
| End or stop | A new segment waits for the prior end to commit | Durable segment marker | Close barrier and prestart cancellation |
| Authentication | An unauthenticated viewer is rejected before replay | Normal Core authentication | API route fixture |

Each segment commits its end marker before publishing the runtime as finished.
End markers are internal; replay skips an old marker when later committed frames
exist, including at a page boundary. A cursor ahead of the journal returns a
conflict before starting SSE. A terminal turn row alone does not end a viewer:
the producer may still need to commit its final frame. Publication and viewer
cleanup join owned tasks through cancellation. Durable mode does not retain a
second unbounded in-memory frame list.

Validation uses synthetic projects and inert local model implementations. The
process journey starts the actual kernel in a retained worker, acknowledges
`First `, promotes API v2 on the same listener, disconnects the original viewer,
retires API v1, and reconnects with the saved cursor. It receives `last` and the
completion, records one provider start and one turn, and retains the execution
PID. Private Unix sockets are fixture transport; this does not claim LAN/browser
or packaged desktop acceptance. No live provider, harness, model download,
operator installation, SSH command or deployment was used.

The focused runs cover 36 distinct passing cases:

- Initial journal/legacy selection: 20 passed in 14.19 s (`/tmp/nebula-chat-journal-tests.txt`).
- Cursor/process selection: 22 passed, 2 fixture failures in 28.06 s (`/tmp/nebula-chat-journal-process-tests.txt`).
- Corrected process fixture: 2 passed in 20.93 s (`/tmp/nebula-chat-journal-process-repair-tests.txt`).
- Resume/close boundaries and affected integrations: 14 passed in 29.15 s (`/tmp/nebula-chat-journal-segment-tests.txt`).
- Prestart stop and affected legacy API stops: 4 passed in 4.21 s (`/tmp/nebula-chat-journal-stop-tests.txt`).

Independent fresh-context validators confirmed the following before repairs:

- The process fixture expected identity on a delta, but the established contract
  carries identity on `started`; persisted SQL and the UI consumer confirmed this.
- Default resume replay began at zero and could expose an old pause frame.
- A new segment's own queued/admitted frames suppressed legacy prefix import.
- Publishing `done` before the end commit allowed overlapping segment boundaries.
- Cancelling before the first coroutine step bypassed producer cleanup and left
  a queued-only journal open. Stop now records cancellation and end itself when
  that cleanup never ran, without duplicating a repeated stop.

Ruff and targeted mypy passed. Process runs emitted only the existing WebSocket
dependency deprecation warnings. The inert vLLM fixture also causes the existing
OpenRouter-only credit observer to reject a non-OpenRouter refresh locally;
that rejection contacts no provider.

This stage does not detach durable command acceptance from a chat follower,
complete handoff inventories/restoration, recover unknown effects automatically,
or change compatibility launchers, clients or desktop lifetime. A crash before
the segment end still requires owner recovery or explicit reconciliation.
Authenticated production LAN, mobile WebKit and packaged desktop checks remain
required before claiming complete non-disruptive deployment support.
