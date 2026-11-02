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

## Final verification

The source Core code validated here is `e5c7a55d`. Subsequent commits add this
receipt and improve only the benchmark fixture/client; the production files'
SHA-256 values in [control-plane-read-results.json](control-plane-read-results.json)
match the accepted source. The baseline is `70ee6328` and uses the same interpreter,
dependencies, private LAN interface and synthetic scale fixture.

The committed `.github/test-selection.json` selects 76 cases across 22 Python
file/node targets. Local execution covered all 76 (75 passed in the selected run,
then the corrected enabled-server fixture passed separately). Fresh GitHub CI
run `41520175486` collected 76 and passed **76 in 83.31 seconds** on Python 3.12.
Its selection receipt's digest, targets and counts were checked against the
committed plan. Other Python versions explicitly reported omission; no tests ran
in those jobs. The Playwright selection was empty and its required gate passed.
Ruff lint/formatting, mypy over 228 source files, diagnostic blind-spot audit and
sdist/wheel builds passed. No full suite ran.

The new tests exercise all changed catalog wrapper categories, actual project
and scheduled capability entry points, Session ownership, a mixed 24-reader
burst with cancellation, two 20-request delayed-SQL API bursts, stale/foreign
identity handling, exact transcript ordering and retractions, late-profile
discovery, legacy policy defaults, temporary lease renewal and racing native
goal replay. Existing tests retain mutation, permission and native-goal contracts.

## Disposable Core comparison

Both runs bootstrapped a complete Core lifespan and used real TCP HTTP on a
non-loopback private LAN address. Pairing/bearer authentication stayed enabled;
unauthorized reads returned 401. Each instance used a fresh temporary SQLite
store and synthetic workspace. No provider, harness or MCP server was contacted.
The diagnostic POST calls the real interactive Butler gateway and global
`work_read`; production GET routes exercise Voice Work chat inspection, goal
children, scope tool candidates and search integration status. The client had
256 connections for 140 readers so its own pool could not manufacture timeouts.

| Concurrent workload | Baseline | Candidate |
| --- | ---: | ---: |
| Ordinary authenticated request median | 3,002.385 ms | 19.376 ms |
| Ordinary authenticated request p95 | 3,316.096 ms | 260.368 ms |
| Maximum completed ordinary request | 3,610.601 ms | 487.159 ms |
| Ordinary 10-second timeouts | 1 | 0 |
| Maximum event-loop lag | 1,501.275 ms | 355.147 ms |
| Completed full settings reads | 140 | 140 |

A full 140-call catalog burst takes about 77 seconds while ordinary requests
remain responsive. Scheduled roster reviews use `chat_settings_many` to avoid
rebuilding those catalogs. This change bounds the cost of full catalog reads
and removes them from the shared request loop; it adds no stale catalog cache.

| Production API operation, median of three | Baseline | Candidate |
| --- | ---: | ---: |
| Eight recent message previews from 5,000 messages | 285.777 ms | 28.893 ms |
| Children with 2,535 unrelated goals | 283.878 ms | 5.486 ms |
| Search status with 2,535 nonmatching policies | 202.548 ms | 7.750 ms |
| Scope tool candidates | 98.888 ms, incomplete (0 tools) | 526.952 ms, complete (24 tools) |

Scope discovery now reads the complete catalog: the candidate returned the eight
tools from eligible server 2,535 plus 16 built-in notebook tools. The baseline
read its first 1,000 disabled profiles and returned none. The longer discovery
request reflects the complete result; delayed-SQL tests prove that bursts still
leave database and worker capacity for ordinary requests.

Origins: `http://172.26.91.246:51277` (baseline) and
`http://172.26.91.246:53235` (candidate). These are source Core builds, without a
UI bundle. Both servers exited with code 0. Their temporary directories
`/tmp/nebula-read-audit-bq3gkqlu` and `/tmp/nebula-read-audit-9wx4vvpm` were removed,
and absence was independently rechecked. No installed-release, live-provider or
physical-device coverage is claimed. Browser/device gates do not apply to this
backend-only change. Production was not accessed or restarted.

Reproduction from the candidate checkout:

```sh
git worktree add --detach /workspace/nebula-read-audit-baseline 70ee6328
PYTHONPATH=/workspace/nebula-read-audit-baseline/src timeout 240 .venv/bin/python scripts/benchmarks/control_plane_reads.py --host 172.26.91.246 --label baseline --output /tmp/nebula-control-read-evidence/baseline.json --reads 140
PYTHONPATH=/workspace/nebula/src timeout 240 .venv/bin/python scripts/benchmarks/control_plane_reads.py --host 172.26.91.246 --label candidate --output /tmp/nebula-control-read-evidence/candidate.json --reads 140
```

Use the current executor's private non-loopback address when reproducing. The
harness owns process termination and temporary-data cleanup, and asserts zero
candidate timeouts, sub-second ordinary HTTP/loop lag, and late-server visibility.
