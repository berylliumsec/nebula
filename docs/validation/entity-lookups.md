# Entity lookup stalls: validation and promotion gate

The code change starts from `main` at `c0f303c0`. The November 2 production build
was reported as `aa797c9`; the affected listing, artifact lookup, query builder,
Session context, and recovery function bodies are identical in those builds.
Production itself was not accessed or modified. The user explicitly replaced the
LAN/VPN validation requirement with an isolated local Core and authorized PR merge.

## Acceptance contract

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover/select/list | Existing authenticated list routes return stable project pages without blocking other requests | Core and SQLite | Actual Core HTTP plus query plans |
| Create/update/replace | Every supported write keeps lookup projections consistent, including clearing an artifact call ID | Entity JSON | Focused storage tests across write paths |
| Tool result | Only the requested project's call artifacts return, in `(created_at, id)` order | Entity JSON | Horizon, scope/type/order tests and scale lookup |
| Recovery | Filters reach in-flight chat calls beyond settled history; iteration tolerates calls changing status | Core tool-call records | Existing recovery cases, focused iteration and scale recovery passes |
| Refresh/retry | Repeated reads preserve results; migration startup is repeatable and failed migration is atomic | Database and Alembic | Upgrade/retry/downgrade tests, restored-backup rehearsal |
| Failure | A slow read keeps its Session in its worker; bounded admission leaves the loop responsive | FastAPI/AnyIO | Held-read worker tests and delayed-SQL HTTP probe |
| Authentication | Normal Core authentication stays enabled | Core | Random synthetic bearer token and unauthorized-request test |

No UI controls, browser-origin behavior, settings, streaming, provider/harness
execution, or delete/revoke semantics change. Browser/device suites and native
suites are outside this backend read-path change. No live providers or executable
missions are configured in the throwaway Core.

## Independently confirmed cause

A fresh-context diagnostic agent inspected the original report, code, and raw
reproduction without a proposed patch. Read-only SQLite with `query_only=1` and
`busy_timeout=0` reproduced the delays; `quick_check` was `ok`. The artifact query
executed about 8.8 million VM instructions for three results. Recovery executed
about 2.1 million instructions for the candidate IDs and again for the selected
page. A 1,000-row project page executed about 9.9 million instructions.

The artifact plan used only `(kind, engagement_id)` and a temporary ordering
B-tree. Recovery used a kind-level index plus JSON status/origin predicates; its
page fetch could repeat the broad scan. The project list likewise scanned its
project range and sorted before applying its limit.

The actual generated async Artifact endpoint took 332 ms and stretched a 10 ms
heartbeat to 343 ms. Session creation, use, commit, and close all occurred on the
loop thread. Existing cross-thread Session sharing was not demonstrated. The
periodic recovery methods were already offloaded; their query costs still grew
with settled history. These synthetic measurements establish query and scheduling
causes independently of the separately reported NVMe failure.

The fix maintains three nullable projections through the existing common write
helper and backfills only JSON strings, preserving integer/null metadata filter
semantics. The migration keeps payloads, revisions, and timestamps unchanged.
The artifact and recovery indexes include their exact predicates and stable
ordering; the project index matches `(kind, engagement_id, created_at, id)`.
Both listing handlers are synchronous FastAPI handlers: all their reads and
enrichment run in AnyIO's shared, capacity-limited thread pool (40 tokens by
default). A shared four-listing semaphore admits work before worker dispatch,
leaving database connections for ordinary reads. No Session is passed to a worker.
Tests exercise both two and forty worker tokens, hold read workers during bursts
of twenty requests, and trace Session creation/query/commit/close ownership while
an unrelated authenticated database request still answers.

A separate fresh-context burst diagnosis confirmed why the extra admission
bound is needed: the default database pool has five connections plus ten overflow,
while the worker pool admits forty operations. Twenty held listing reads consumed
all fifteen connections; the event loop then spent 1,006 ms inside pool checkout
for `/auth/devices`, with twenty worker tokens still free. A timer captured the
loop stack in `list_paired_devices -> find_entities -> QueuePool._do_get ->
threading.wait`. Fourteen reads answered in 8.4 ms, and limiting the twenty-read
control to two workers answered in 4.7 ms. The pool-acquisition stack excludes a
SQLite lock wait; unused worker tokens exclude worker-pool exhaustion.

Downgrade uses native column removal because Alembic's SQLite table-copy operation
temporarily drops `entities`, invalidating existing event-ledger trigger references.
A second independent diagnosis reproduced that schema failure with empty event
tables and with/without explicit transactions; rollback retained the original
schema. Upgrade and downgrade tests retain those triggers throughout.

## Reproduction

Run from the candidate checkout with its installed interpreter. These commands
create only synthetic data. Use an absent directory under healthy local storage.
The baseline checkout must remain at `c0f303c0`; passing its source through
`PYTHONPATH` keeps both sides on the same interpreter and dependencies.

```sh
git worktree add --detach /workspace/nebula-entity-baseline c0f303c0
PYTHONPATH=src .venv/bin/python scripts/benchmarks/entity_lookups.py seed --directory /workspace/nebula-entity-scale
PYTHONPATH=/workspace/nebula-entity-baseline/src .venv/bin/python scripts/benchmarks/entity_lookups.py measure --directory /workspace/nebula-entity-scale --label before
PYTHONPATH=/workspace/nebula-entity-baseline/src .venv/bin/python scripts/benchmarks/entity_listing_http.py --database /workspace/nebula-entity-scale/nebula.db --output /workspace/nebula-entity-scale/http-before.json
PYTHONPATH=src .venv/bin/python scripts/benchmarks/entity_lookups.py migrate --directory /workspace/nebula-entity-scale
PYTHONPATH=src .venv/bin/python scripts/benchmarks/entity_lookups.py measure --directory /workspace/nebula-entity-scale --label after
PYTHONPATH=src .venv/bin/python scripts/benchmarks/entity_listing_http.py --database /workspace/nebula-entity-scale/restored.db --output /workspace/nebula-entity-scale/http-after.json
PYTHONPATH=src .venv/bin/python scripts/benchmarks/entity_listing_http.py --database /workspace/nebula-entity-scale/restored.db --output /workspace/nebula-entity-scale/http-after-slow.json --slow-read-ms 2500 --listing-requests 20
```

The HTTP harness runs a complete Core lifespan on an available loopback port,
uses actual `/api/v1/artifacts` and `/api/v1/auth/devices` routes, and stops Core
before exiting. A diagnostic-only authenticated endpoint invokes the actual
stale-approved-call recovery method in its existing worker context. Three waves
each combine eight 1,000-artifact lists (twenty in the delayed-SQL burst), one recovery pass, and twenty lightweight
authenticated HTTP requests. An independent 10 ms heartbeat runs inside the Core
process; clients run in another process. The delayed-SQL probe adds 2.5 seconds
inside artifact SQL execution only after startup. Pairing/authentication remain
enabled; all generated credentials and XDG/runtime/artifact directories are local
to the synthetic fixture. The harness asserts at least 10x median improvement
for every measured lookup/page/recovery operation and sub-second maximum unrelated
HTTP latency and loop lag on the candidate.

## Measured results (November 2, 2026)

Synthetic fixture: 1,100,000 artifacts, 350,001 tool calls (including the lookup
owner), one project, and Core's generated local MCP record. Query medians use
five samples with the same interpreter and dependencies. Full SQL, parameter,
plan, latency, and migration evidence is in
[entity-lookups-results.json](entity-lookups-results.json).

| Operation | Before median | After median | Improvement |
| --- | ---: | ---: | ---: |
| Artifact hit (3 rows) | 1,499.28 ms | 0.488 ms | 3,075.1x |
| Artifact miss | 1,459.88 ms | 0.414 ms | 3,523.6x |
| Recovery candidates (3 rows, both SQL stages) | 1,041.20 ms | 0.798 ms | 1,304.1x |
| Actual stale-approved-call recovery pass | 978.24 ms | 0.896 ms | 1,091.7x |
| Project page (1,000 artifacts) | 307.28 ms | 22.574 ms | 13.6x |

The after plans seek `(kind, engagement_id, artifact_tool_call_id)` for artifacts,
`(kind, tool_call_status, tool_call_origin)` for both recovery stages, and
`(kind, engagement_id)` in the matching creation-order index for project lists.
None scans a broad kind/project history or uses a temporary ordering B-tree.

| Concurrent Core workload | Before | After | After with 2.5-second SQL delay |
| --- | ---: | ---: | ---: |
| Listing HTTP median | 2,968.80 ms | 250.17 ms | 8,062.51 ms |
| Lightweight HTTP p95 | 2,058.94 ms | 31.96 ms | 15.10 ms |
| Lightweight HTTP maximum | 3,480.23 ms | 329.95 ms | 17.07 ms |
| Recovery HTTP median | 3,992.31 ms | 14.94 ms | 30.87 ms |
| Event-loop lag maximum | 3,463.63 ms | 316.46 ms | 378.44 ms |

The tenfold gate applies to the measured database operations above; HTTP list
latency also includes eight concurrent ORM materializations and responses. All
candidate unrelated-request and loop-lag maxima passed the sub-second gate.
Origins were `http://127.0.0.1:39515` (before), `:42081` (after), and `:41873`
(delayed SQL), using the actual source Core API with a complete lifespan and no
UI bundle. Local validation replaces LAN validation by explicit user instruction.
No physical device, browser-engine, installed-release, or live-provider result is
claimed.

The rehearsal verified source and backup integrity, copied the backup to a
restored database, upgraded the restored copy in **21.183 seconds**, and verified
integrity, projection population, row counts, and a matching SHA-256 over every
authoritative envelope/revision/timestamp. Legacy size was 1,638,883,328 bytes;
migrated size was 1,989,644,288 bytes. Peak restored database/WAL/SHM usage sampled
every 20 ms was 3,388,936,944 bytes. This peak excludes separately retained source
and backup copies and any SQLite temporary sort files. All temporary Core
processes were stopped; generated database/runtime data is removed after recording
the evidence. This is a synthetic migration rehearsal, not validation of the real
production backup or storage.

## Focused checks

The diff-bound `.github/test-selection.json` selects exactly 47 Python tests:
`test_session_record_lookups.py`, the three new hot-lookup/migration/worker files,
two exact restart-recovery cases, and one exact payload-filter storage case.
Collection found 47; execution passed 47 in 24.57 seconds. No full suite ran.
Ruff lint and formatting passed. CI also validates the committed selection receipt.

## Production promotion remains blocked

Hardware and real-backup work is tracked independently in
[issue #905](https://github.com/berylliumsec/nebula/issues/905). The task has not
verified the production backup, repaired the drive, or migrated production.
Starting the candidate against production triggers Alembic at bootstrap, so even
a trial production startup must wait for healthy storage and a verified restore.
Record the real backup identity/checksum, restored integrity and representative
artifact reads, drive remediation, and production-scale rehearsal before promotion.
Use the existing Butler coordination procedure for any eventual production restart.
Code PR merge is separate from permission to promote Core onto the suspect drive.
