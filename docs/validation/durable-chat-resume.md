# Durable explicit chat resume

An explicit provider resume now uses optional `chat.resume/v1` acceptance.
Its command finishes after attaching or starting its execution-owned producer;
the replaceable API follows SQL frames. The operator continues using the same
resume route and turn. There is no automatic resume or retry of unknown effects.

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Resume saved work | Same conversation and turn, one new segment | Fenced binding and provider task | Actual retained kernel on SQLite/PostgreSQL |
| Attach during work | No additional producer | Existing segment resource | Actual process start count and SQL |
| Pending or failed resume | Old pause/approval history cannot stand in for a new response | Nullable segment cursor | Independent API reader, pending and failed cases |
| First output | Cursor and initial frames commit together | One fenced transaction | Injected final-fence rejection rolls back both |
| API replacement/retry | Original receipt and new segment return without another provider start | Immutable request hash and binding | API v2 promotion, v1 retirement, original-key retry |
| Compatibility | Older owners retain HTTP transport; incompatible targets cannot consume queued typed commands | Feature negotiation and transfer guard | Transport retry and API/execution reader-floor cases |

Migration `0040_runtime_chat_cursor` adds a nullable `stream_after` column.
Old create bindings remain readable. A resume binding without a cursor is
pending, even if the turn has older journal history. Its first publication
commits the new cursor with its frames. API viewers start at the greater of the
saved receipt cursor and supplied reconnect cursor. Attaching to an existing
producer verifies its exact project/runtime/fencing resource before binding the
cursor; a completed segment may still be replayed if it finished during attachment.

The new feature is independent of `chat.accept/v1`. The accepting API negotiates
with the current project owner; missing support retains `http.invoke/v1`.
Transport negotiation shares the same immutable HTTP identity. Resident workers
and accepted typed receipts establish API reader requirements. Transfer also
checks queued command features against the target release, preventing a resume
accepted just before handoff from reaching an older worker.

Validation: 81 distinct cases passed. The initial run passed 73 in 79.46 s;
independently validated fixture repairs yielded another five in a focused eight
case run, and the remaining three PostgreSQL cases passed in 3.84 s. Failures
were missing required synthetic model/provider fields, an acquisition API used
as an ownership read, and missing explicit reader pool settings. No product
behavior was changed to accommodate them. Raw outputs are
`/tmp/nebula-chat-resume-tests.txt`, `/tmp/nebula-chat-resume-rerun-tests.txt`, and
`/tmp/nebula-chat-resume-pg-tests.txt`. Ruff and targeted mypy passed.

The process scenario creates a real saved provider request, finishes its first
segment, seeds a synthetic interrupted recovery state, resumes it, attaches a
second viewer, replaces the API, retries the original resume key, and completes
from its new cursor. It retains one turn and exactly two provider starts across
the two segments, with the same execution PID. Fixtures use disposable SQL,
private Unix listeners and inert providers; no existing installation or live
provider was contacted.

This is backend source evidence. Harness and nonstreaming commands retain their
existing delivery. Full handoff integration, clients, launchers and desktop
packaging remain in progress. Required production LAN, mobile WebKit and packaged
desktop continuity evidence remains outstanding; this stage does not establish
complete non-disruptive deployment support.
