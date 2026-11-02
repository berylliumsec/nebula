# Control-plane read audit

Baseline: `70ee6328` (includes the Butler settings sweep fix, PR #911).

## Acceptance contract

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Butler discovery and inspection | Project and global work reads yield while storage is slow; catalogs and scope checks retain their results | Core store and Butler authority | Capability regression tests and disposable HTTP Core |
| Chat preview | Inspecting a chat reads only its recent active transcript window and preserves ordering and role filtering | Core transcript | History regression and scale probe |
| Goal children | Children are selected by project and exact parent without materializing unrelated goals | Core goal records | Scope/order/horizon regression and scale probe |
| MCP tool selection | All eligible servers remain discoverable beyond 1,000 profiles, with existing notebook scope and permission checks | Core profile catalog | Authenticated API regression and scale probe |
| Search integration status | Project usage is counted in storage without hydrating every policy on the loop | Core scope policies | Status regression and scale probe |
| Temporary chat cleanup | Only detached temporary-chat candidates are inspected; ordinary and side chats survive; cleanup yields during storage reads | Core leases and transcripts | Collector regression and disposable lifespan |
| Harness activity | Concurrent readers cannot overwrite a newer native goal, plan or cursor with older replay | Core event ledger and immutable cache snapshots | Deterministic racing replay plus existing native goal/cost regressions |
| Cancellation, retry and refresh | Cancelled readers retain physical worker capacity until their reads finish; later reads recover and see fresh revisions | Worker executor and Core | Held-read and repeat-read regression |
| Authentication | Pairing and bearer authentication remain enabled; unauthenticated requests fail | Core auth | Disposable LAN Core |

No UI controls, settings schema, model/provider behavior, or browser-origin behavior
changes. Browser/device geometry, native suites and production UI builds do not
exercise this backend scheduling/query change. Streaming, interruption and
reconnection behavior remain owned by the existing async services; they must not
be moved onto worker event loops. The disposable instance uses only synthetic
data, inert runtimes and a random test credential, on a non-loopback LAN address.

## Independently confirmed causes

A fresh-context diagnostic subagent traced the actual capability gateway and
reproduced each fault without a proposed patch. The synthetic SQLite database had
2,535 MCP profiles with eight tools each (only one enabled), 2,535 unrelated goals,
5,000 messages, 2,535 nonmatching scope policies and 2,537 ordinary chats. No
provider requests or competing writes ran. Tiny or empty outputs exclude response
volume as a necessary cause; the fixture also excludes vendor latency and
concurrent SQLite locking.

| Baseline operation | Records materialized | Wall time | Maximum heartbeat gap |
| --- | ---: | ---: | ---: |
| Global work gateway to chat settings | 2,535 MCP profiles | 576 ms | 577 ms |
| Project work chat settings | 2,535 MCP profiles | 409 ms | 411 ms |
| Goal children, zero matches | 2,536 goals across all projects | 133 ms | 134 ms |
| Chat inspection, eight previews | 5,000 messages | 490 ms | 492 ms |
| Scope tool candidates | First 1,000 profiles; omitted eligible profile 2,535 | 96 ms | 97 ms |
| Web-search status, zero enabled projects | 2,535 policies | 274 ms | 272 ms |
| Temporary-chat collector, zero expired chats | 2,537 ordinary chats | 67 ms | 67 ms |

The existing global `chat_settings` worker path was responsive to I/O but still
spent 518 ms rebuilding its full catalog. Its batch alternative read no catalog.
The general work dispatcher and direct global roster/catalog methods bypassed
that offload. Several typed Butler catalog wrappers had the same synchronous
handler inside an async function. The worker bound must be shared across these
paths; a per-case semaphore does not bound a burst across projects or cases.

## Compatibility and concurrency review

An independent diff review identified the harness activity cache as another
shared-state boundary. Butler roster/goal reads and the existing synchronous
Voice Work API can reach the same cache as async session-activity reads. Replay
now folds a private snapshot, then publishes it under a short lock only if its
cursor is current. Storage I/O occurs outside the lock. A deterministic race
holds an older replay until a newer goal has been published and checks that the
older reader cannot regress either the goal or its cursor.

The selected existing Assistant-settings test also failed on unchanged baseline
`70ee6328`: it incorrectly required every Work mutation to be directly available
to scheduled Butler. The newer permission contract explicitly excludes rewind
and interactive Work edits. Only that blanket assertion is narrowed to the two
Assistant settings operations; permissions remain unchanged. Baseline reproduction:
`timeout 30 /workspace/nebula/.venv/bin/python -m pytest -q tests/v3/test_butler_capabilities.py::test_scheduled_butler_can_edit_all_assistant_settings`
from the baseline worktree: one existing assertion failure, matching the candidate.

Final selection, before/after measurements, disposable instance identity and
cleanup evidence are recorded below after execution.
