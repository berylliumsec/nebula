# Durable chat acceptance

A capability-gated execution worker commits a streaming submission's command
binding, conversation, user messages and turn in one fenced transaction. It
starts the execution-owned producer and returns a small acceptance response to
the command runner. The public API preserves SSE by reading the durable turn
journal. A viewer no longer holds the project's ordered work consumer for the
whole response, so separate chats in one project can run concurrently.

| Journey | Observable invariant | Authority | Selected evidence |
| --- | --- | --- | --- |
| Accept a message | Its receipt points to exactly one committed turn | Fenced SQL transaction | New and existing sessions on SQLite/PostgreSQL |
| Lose response and retry | Transport negotiation does not change request identity | Canonical HTTP payload hash | Sequential and concurrent old/new modes |
| Run another chat | Accepted commands finish while both producers remain active | Execution task and host capacity ledgers | Two actual retained-process producers |
| Replace API | Retry returns the same receipt/turn and output resumes at its cursor | Binding and journal | Promote API v2 and retire v1 |
| Fence or conflict | A stale token or duplicate binding cannot leave a second turn/message | Scoped transaction rollback | Independent SQL readers |
| End a producer | Resource closure and stream end commit together | One fenced publication transaction | Injected final-fence rejection and process readback |
| Roll back compatible code | APIs that cannot read accepted work remain unpromoted | Release features and durable command names | Resident and accepted-data guards |

`chat.accept/v1` is an optional release feature and command transport. APIs select
it only for streaming provider submissions whose current owner declares support.
Older owners keep the existing `http.invoke/v1` streaming path. A project with no
owner yet (its first chat after Core starts or after idle retirement) also gets
`http.invoke/v1`, because the API cannot know which release will claim it. The
owner that claims such a chat start knows what its application serves: under its
fence it renames the running command to `chat.accept/v1` (or `chat.resume/v1` for
an explicit resume) before invoking it, so it too ends at acceptance and frees the
work lane. Viewers read the binding before the command, so a rename followed by a
binding never looks like a binding on an untyped receipt. Both transports
hash the same HTTP identity, so retries after negotiation return the original
receipt; changed content still conflicts. Public JSON cannot set the private
command identity. Binding reads require the accepting actor, and execution
revalidates a paired device before dispatch.

Release manifests now carry bounded feature and required-API-feature lists.
Missing lists preserve the old manifest behavior. A resident feature worker, or an
already accepted typed command, blocks promotion of an API lacking that reader
feature. A new execution candidate waits for a compatible active API. Workers
reject unsupported declared features and verify that supervisor-owned additive
chat migrations exist before readiness; they do not migrate themselves.

Migration `0039_runtime_chat_binding` is additive. Bindings are immutable from an
execution database; retention belongs to the host. A duplicate request cannot
commit another conversation, turn or user message. Provider segments also own
explicit runtime resources while queued/running. Their durable end publication
closes the resource in the same transaction, preventing a detached acceptance
receipt from making active work appear transferable.

Validation collected 77 distinct cases across two focused runs:

- Acceptance/compatibility: 27 passed in 13.58 s (`/tmp/nebula-chat-acceptance-tests.txt`).
- Process/journal/delivery/atomic-publication/bundle integration: 50 passed in
  64.68 s (`/tmp/nebula-chat-acceptance-integration-tests.txt`).

The process fixture runs the actual kernel with two inert model producers. Both
emit `First ` and wait. Their two commands already report succeeded acceptance,
their resources remain active, and shared provider usage is two. API v2 takes
traffic and API v1 retires. Retrying the first key returns its original receipt
and turn, with exactly two total provider starts. Releasing the fixture produces
`last` plus completion for each saved cursor, closes both resources, and retains
the execution PID. The generic older transport also passes API replacement.

Ruff and targeted mypy passed. Only existing WebSocket dependency deprecations
were emitted. Fixtures use unique SQLite/PostgreSQL state, private Unix sockets,
retained source wrappers and inert model implementations. No live provider,
operator installation, SSH, model download, compute workload or deployment was
used. Final selection: `/tmp/nebula-chat-acceptance-final-selection.json`.

This stage detaches new streaming provider submissions. Explicit provider resume
is covered by [the subsequent resume stage](durable-chat-resume.md). Harness
commands and nonstreaming requests still use their existing delivery paths.
Complete handoff admission/restoration, crash reconciliation, clients, launchers,
desktop and packaging integration remain in progress. This is backend source
evidence; production LAN, mobile WebKit and packaged desktop continuity gates
remain required before a complete non-disruption claim.
