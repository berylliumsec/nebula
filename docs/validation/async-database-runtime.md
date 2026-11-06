# Async database runtime and same-chat runtime switching

Status: **not ready for merge or promotion**. The selected runtime and recovery
checks pass. Browser acceptance and bundled product-knowledge verification remain
blocked by this environment's download policy. Chat admission performance has
regressed in the corrected mixed-load experiments. This receipt must be completed
before promotion.

## Baseline and boundary

Implementation starts from `origin/main` at
`928727eb3a80a4516ff2ad2c30e02855697b56f4`. Baseline performance runs use a separate,
unchanged worktree at that commit. Both versions use the same Python environment,
PostgreSQL 16 fixture, hardware, synthetic data, provider delay, and workload.
The benchmark records a hash of each runtime source tree.

The branch is being integrated with freshly fetched `origin/main` at
`b93b66844815574e0d3de209b08e59ea6cec1a3f`. Its two commits after `928727eb` change
terminal-client behavior and documentation; Core's database/runtime sources match
the recorded benchmark baseline.

The fixture's deployment allocation is deliberately explicit:

```text
max_connections = 100
PostgreSQL reserved slots = 3
additional administrative reserve = 5
other consumers (including maintenance) = 12
peak simultaneous Core processes = 2
per-process ceiling = floor((100 - 3 - 5 - 12) / 2) = 40
```

This is a disposable test allocation, not a production default. Runtime pools
have zero overflow. Equal-limit measurements configure both versions with 15
reusable connections and zero overflow; maximum-limit measurements use the
calculated 40. Baseline pool configuration is changed only by the external test
harness. One-connection SQLite coverage checks nested and shared read admission.

## Selected correctness evidence

The diff-bound selection is maintained in `.github/test-selection.json`.
The current completed selection contains **103 passing Python cases**, including:

- native SQLite and PostgreSQL CRUD, rollback, revision conflicts, stream
  sequencing, idempotency, connection budgeting, cancellation, and pool waits;
- real Core lifecycle, bearer authentication, device listing, provider streaming,
  and settings-assistant read/mutation callbacks;
- rejection of synchronous SQLAlchemy driver operations and synchronous SQLite
  connections on the exercised Core event loop;
- 25 existing storage regressions and 14 existing recovery/tool regressions;
- interrupted-turn restart, late receipt adoption, competing worker ownership,
  compaction budget debit, and ordered concurrent tool results;
- idle/running/paused/blocked goal preservation, pending switches, queued work,
  goal-claim release, competing switches, stale compaction consent, and explicit
  repair of legacy split runtime state;
- frozen-package dependency requirements for SQLAlchemy asyncio, aiosqlite, its
  SQLite dialect, and greenlet.
- rollback and re-upgrade on both databases without losing entity revisions,
  payloads, or ordered events;
- actual provider continuation of an explicitly resumed, formerly harness-bound
  blocked goal in the same conversation, stopping at its saved step budget.
- native auxiliary Butler-memory and browser-action SQLite transactions, writer
  waits, cancellation cleanup, authoritative revisions and project isolation;
- preservation of later settings and transcript cursors during pending
  activation, changed harness privacy/options, dependency revision fencing,
  and compaction consent surviving the pending write's own revision increment.

The combined 103-case run passes. The three focused UI API mapping cases also pass. This is focused evidence,
not a claim that the repository's unchanged synchronous test and diagnostic
callers have all been migrated or exercised.

The harness-list work test grows to 10,000 turns and 10,000 temporary chats,
including 5,000 turns matching the requested session. It checks three batched SQL
statements, eligible-chat partial-index usage, bounded PostgreSQL buffer work,
and SQLite VM work that does not grow with matching history. Observed SQLite VM
steps were 783 for the small fixture and 593 for the large fixture.

Static type checking currently reports the same ten unrelated diagnostics found
in the baseline. The changed runtime compiles, and undefined-name checks pass.
The remaining repository test and diagnostic callers still need an async API
audit before this conversion is ready for review.

## Performance evidence in progress

Run `scripts/benchmark_async_database.py` with `NEBULA_TEST_POSTGRES_URL` set to an
owned disposable database. Each invocation creates and removes one isolated
schema. Providers and tools are inert. The corrected harness uses a separate
fixed-arrival HTTP load-generator process and a real TCP Core. It checks the
exact final chat message, SSE errors, tool output, and durable completion counts.
It records successful-request latency separately from failures, pool state,
query/transaction telemetry, loop lag, host admission observations, and resident
memory. It excludes browser rendering. Measurement-window telemetry is reset
after setup and warmup. Seeded historical turns are complete.

The original closed-loop, in-process ASGI experiment is retained in
`scripts/benchmarks/async_database_asgi.py`. Its exploratory results follow:

Completed comparisons so far:

| Workload | Pool ceiling | Baseline p95 first token | Candidate p95 first token | Baseline p95 listing | Candidate p95 listing |
| --- | ---: | ---: | ---: | ---: | ---: |
| Low load, 60 chats | 15 | 246.521 ms | 186.240 ms | 111.244 ms | 50.523 ms |
| Closed-loop mixed load, 180 chats | 15 | 6622.927 ms | 4555.526 ms | 865.773 ms | 381.028 ms |
| Closed-loop mixed load, 180 chats | 40 | 9597.375 ms | 8413.454 ms | 951.578 ms | 397.298 ms |

These runs had zero request errors. The candidate had zero checked-out
connections after shutdown. The low-load baseline had one connection checked out
immediately after its lifespan returned; the saturated baseline had zero.
The 40-connection experiment improved first-token p95 by only 12.34%, missing the
20% target; authentication p95 also increased. That experiment has material
limitations: closed-loop offered load, queued historical turns triggering startup
recovery, setup included in SQL telemetry, and unobserved host admission. It does
not prove database saturation and is not acceptance evidence.

Corrected fixed-rate TCP summaries are saved in `async-database-http.json`, with
source/harness hashes, declared budgets, completion counts, failure counts and
cleanup results. These measurements precede the final auxiliary-store and
activation-correctness fixes; they do not certify the final source tree.

| Monitor / workload | Pool | Baseline → candidate admission p95 | Baseline → candidate first token p95 | Baseline → candidate listing p95 |
| --- | ---: | ---: | ---: | ---: |
| System, 1 group/s, 30 chats | 15 | 109.138 → 141.908 ms | 211.477 → 182.297 ms | 107.980 → 51.112 ms |
| System, 4 groups/s, 120 chats | 40 | 131.736 → 193.131 ms | 259.636 → 274.971 ms | 125.229 → 62.779 ms |
| Controlled, 8 groups/s, 120 chats | 15 | 7690.613 → 10693.405 ms | 9028.056 → 10896.243 ms | 16654.871 → 728.074 ms |
| Controlled, 8 groups/s, repetition 1 | 40 | 6585.556 → 10068.122 ms | 7909.132 → 10261.110 ms | 14928.311 → 590.565 ms |
| Controlled, 8 groups/s, repetition 2, reversed order | 40 | 6455.616 → 8116.964 ms | 7834.096 → 8338.758 ms | 14766.598 → 606.303 ms |
| System, 2 groups/s, five-minute soak | 40 | 151.038 → 171.231 ms | 276.869 → 219.760 ms | 147.547 → 57.330 ms |

All 14 corrected runs had zero request errors and expected durable completion
counts. Every candidate run ended with zero checked-out connections. Candidate
pool diagnostics recorded no timeouts or connection failures. Authentication and
listing improved substantially in these comparisons, but chat admission did not.
The soak first-token gain does not establish the mixed-load acceptance target.
Performance acceptance has **not passed**.

Controlled-monitor experiments retain the real finite host gate and provider
limits, but use a synthetic healthy host monitor; they cannot replace production
host-admission evidence. The 16-groups/s overload pair also experienced over two
seconds of load-generator clock slippage, limiting interpretation of its offered
rate. It had no lost or duplicated completions, but does not prove a production
overload-rejection contract.

Subsequent harness-only traces show longer serial storage phases and provider-slot
residence with the candidate, and nearly full use of one event-loop CPU thread
during the controlled busy interval. They do not establish exhausted connection
capacity or identify a specific CPU repair. CPU attribution remains in progress.

## Production UI and LAN evidence

The candidate production UI build and its three focused API mapping tests pass.
A disposable CLI-launched Core served the production bundle at
`http://172.26.91.250:32873`. Unauthenticated project listing returned 401;
authenticated health, project listing, and device listing succeeded. The served
entry asset matched SHA-256
`e20f3a01ec49c44495dea4de87f160fa1284630a697c1e4d2740e9f8be2dbf23`.
The build manifest identified base commit `928727eb` and build time
`2026-11-06T10:56:57.649Z`; this is an uncommitted candidate bundle, not a release.
The owned Core and its temporary data were removed after the check.

The committed real-Core switching journey covers the same conversation through
an approval boundary, refresh, deferred activation, explicit goal resume, and
provider continuation. Its selected desktop/compact Chromium and mobile
Chromium/WebKit runs remain **unexecuted**: Playwright's browser download was
denied with HTTP 403 by environment policy. HTTP checks do not substitute for
these browser journeys.

Focused browser collection finds 24 cases: 16 mocked runtime/compaction cases
across desktop, compact and mobile engines, plus eight real-Core switching cases
across the selected desktop and mobile boundary widths. These cases are selected
for PR CI; collection is not an execution result.

The product-knowledge rebuild was also denied while fetching the ONNX embedding
model. No replacement vectors or stale-digest workaround were introduced.
Regeneration, current-source digest verification, and Chroma synchronization are
still required.

## Promotion and rollback requirements

Before promotion, supply the three deployment budgets and account for rolling
process overlap, maintenance, and auxiliary clients. Observe pool usage separately
from that declared allocation. Follow the repository's Butler pause/resume
procedure and verify authoritative state before replacing a running Core.

The two new migrations are additive. The selected rollback rehearsal passes on
SQLite and PostgreSQL: returning to `0028_harness_session_title_lookup` preserves
entity payloads, revisions, and ordered events, and upgrading again succeeds.
Use the candidate's migration
code while Core is stopped; older code cannot resolve the new Alembic revision.
A previous Core binary also needs an allocation large enough for its 15-connection
default, or an already-validated bounded-pool backport. Do not restart an older
binary under a smaller allocation. Keep the complete coherent rollback snapshot
until the new deployment and any recovery have been verified.
