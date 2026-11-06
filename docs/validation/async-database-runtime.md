# Async database runtime and same-chat runtime switching

Status: final acceptance in progress. Local correctness passes. The final runtime
must pass the recorded load rechecks, hosted product-index checks, two frozen
Linux database journeys, and the selected browser matrix before merge.

## Baseline and deployment budget

Implementation and performance baseline:
`928727eb3a80a4516ff2ad2c30e02855697b56f4`. The clean baseline worktree remains at
that revision. The branch includes freshly fetched `origin/main` at
`b93b66844815574e0d3de209b08e59ea6cec1a3f`; its intervening terminal-client and
documentation changes do not change Core's database/runtime sources.

The owned PostgreSQL 16 fixture declares this allocation:

```text
max_connections = 100
PostgreSQL reserved slots = 3
additional administrative reserve = 5
other consumers, including maintenance = 12
peak simultaneous Core processes, including rolling overlap = 2
per-process ceiling = floor((100 - 3 - 5 - 12) / 2) = 40
```

These values are synthetic test configuration, not production defaults. Startup
discovers server, login-role and database limits with one bounded connection,
closes it, and rejects missing/infeasible deployment budgets. Runtime pools use
zero overflow, pre-ping, and an explicit finite timeout (30 seconds by default).
A stricter finite pool override is permitted. Current free connections never
increase the allocation. CLI/maintenance pools consume declared auxiliary capacity.

Bulk reads share `min(4, max(1, configured_size - 1))` admission; a one-connection
pool serializes work. Point lookups and small authentication control-state reads
use ordinary admission so held bulk catalogs do not occupy their reserved path.
All connections remain subject to the same finite pool ceiling.

## Focused correctness evidence

The diff-bound selection is `.github/test-selection.json`: **217 Python cases**,
three frontend cases, 24 Playwright cases, and two frozen database journeys. No
full suite was requested or run.

The final local run passed **213 cases in 153.94 seconds**, without skips. The
four selected product-index cases require the model supplied by hosted CI. A
subsequent rerun of the strengthened mixed-recovery case passed in 6.14 seconds.
Runtime and unraisable-coroutine warnings fail the selection. Two Uvicorn
WebSocket deprecation warnings remain unrelated to database execution.

Coverage includes native SQLite/PostgreSQL transactions, competing revisions,
ordered events, claims and idempotency, pool saturation, task-owned sessions,
rollback on cancellation, repeated shutdown cancellation, CLI loop selection,
login-role limits under startup SET ROLE, and additive migration rollback/re-upgrade.
Exercised Core paths reject synchronous SQLAlchemy drivers and synchronous SQLite
connections on the event loop.

Recovery checks preserve committed tool results and one durable turn owner.
The combined recovery check resumes an interrupted turn alongside 12 foreground
chats, 12 persisted inert tools, 12 authenticated device reads and 12 indexed
harness listings. It requires exactly one recovered assistant effect and one
execution per tool, with all sessions released.

The final caller audit also covers asynchronous factories, settings-assistant and
scheduled Butler actions, fresh authorization for concurrent audit steps on both
databases, atomic harness goal creation/forks, lazy execution-policy creation in
the same transaction, and project/revision validation of evidence relations.
File verification, output scans, PDF hashing, ZIP creation, evidence validation and
workspace scans remain off the event loop after async authorization releases its
session. Actual API responsiveness and ownership replace obsolete thread-owned
SQLAlchemy-session assertions.

Recent-message reads retain 16-row async cursor batches, tombstone truthiness,
sequence order and bounded hydration, and close the cursor on early exit. Native
PostgreSQL checks inspect `pg_cursors` after the read in a reused unit of work.

Harness listing uses three batched indexed top-one lookups rather than ranking
history. The work tests include 10,000 turns and 10,000 temporary chats, with
5,000 turns matching the requested session. They check the eligible-chat partial
index, bounded PostgreSQL buffers and SQLite VM work (783 small-fixture steps,
593 large-fixture steps), pagination, title precedence and project isolation.

Ruff, compilation, the diagnostic blind-spot audit and the default mypy check of
246 source files pass. The historical extra `--check-untyped-defs` experiment
reported the same ten diagnostics as the baseline.

## Fixed-arrival HTTP performance

`scripts/benchmark_async_database.py` runs a separate fixed-arrival HTTP driver
against a real TCP Uvicorn Core. Both revisions use Uvicorn's actual auto-selected
`uvloop.Loop`, identical hardware/environment and fixtures, the system host monitor,
a 20 ms inert provider, persisted local tools, and 12 harness sessions with 2,400
complete historical turns. Setup and warmup are excluded from telemetry.

The equal-limit pair uses 15 connections and zero overflow for both revisions;
the maximum pair uses the calculated 40 for both. The baseline pool override is
external to its unchanged source. Successful latency is separate from overload
rejections. Exact SSE output, tool output, durable completion counts and shutdown
occupancy are checked. Reports retain source/harness hashes, acquisition waits,
query/transaction timing, occupancy, event-loop lag and driver issue lag.

The ten-run matrix at `8384824` passes the default mixed-load target:

| Workload | Pool | Admission p95, baseline → candidate | First token p95 | Listing p95 |
| --- | ---: | ---: | ---: | ---: |
| Low, 1 group/s, 30 chats | 15 | 150.016 → 130.692 ms | 284.544 → 192.562 ms | 148.815 → 41.335 ms |
| Saturated mixed, 8 groups/s, 120 chats | 15 | 45,760.379 → 7,684.914 ms | 45,854.884 → 7,720.517 ms | 522.296 → 196.609 ms |
| Saturated mixed, 8 groups/s, 120 chats | 40 | 59,211.562 → 18,996.529 ms | 59,295.683 → 19,033.725 ms | 5,825.140 → 226.761 ms |
| Overload, 16 groups/s, 180 chats | 40 | 66,871.360 → 35,094.038 ms | 66,968.968 → 35,132.779 ms | 8,116.612 → 5,446.631 ms |
| Five-minute soak, 2 groups/s, 600 chats | 40 | 131.472 → 134.515 ms | 252.845 → 175.106 ms | 123.021 → 42.662 ms |

Every run had zero request errors, zero rejections, exact expected completions,
and zero checked-out connections after shutdown. Candidate diagnostics recorded
zero pool timeouts or connection failures. Mixed-load gains exceed 20% for all
three target metrics at both limits; low-load latency improves. Soak admission
varies by 2.3% while first-token latency improves by 30.7%. Overload driver issue
lag reached 0.894 seconds; that limits interpretation of its instantaneous rate.
It does not establish overload rejection behavior; focused pool saturation tests
cover asynchronous waiting and controlled timeout failures.

The complete matrix is in `async-database-http-final.json`. It predates the last
caller repairs; final-source candidate rechecks remain a merge gate. The unchanged
baseline/harness provide the comparison, and final runtime hashes must match.

Earlier exploratory closed-loop results and 14 fixed-rate experiments failed the
chat target. They remain in `async-database-http.json` and are not acceptance
evidence. Diagnosis found a lost Uvicorn loop-selection boundary and repeated
queue-position scans; runtime code restores auto loop selection and shares a
short-lived single-flight position snapshot. Claims and admission never use that
snapshot, and same-transaction reads bypass it.

## Production UI, packaging and LAN

The selected journeys serve the production bundle from a disposable authenticated
Core on a non-loopback origin. Coverage includes desktop Chromium at 1440/1024,
emulated mobile Chromium and WebKit at 320/390/430, approval settlement, active
versus pending selection after refresh, explicit same-goal resume, provider
continuation, budget pause, transcript preservation and responsive geometry.
Physical devices were not used.

Current-candidate hosted checks remain required: 217 Python cases on 3.11–3.13,
three frontend cases and production build, four current product-index/digest/Chroma
checks (within the Python selection), all 24 browser cases, and two real frozen
Linux SQLite/PostgreSQL journeys. Hosted jobs install their own browser/model
boundaries; local downloads were denied and no alternate download route was used.
Frozen checks cover authentication, concurrent listing, provider completion,
graceful shutdown, restart transcript preservation and current Chroma guidance.

The real Linux PyInstaller build at `8384824` passed its archive audit: 11,918
members and zero forbidden members. Its SQLite journey passed chat/restart checks
before an external-verifier import error. The direct-invocation import path is
repaired; both complete frozen journeys still require a passing rerun.

## Promotion and rollback

Configure the deployment budgets before promotion; include rolling overlap and
all auxiliary consumers. No production instance was restarted for this work.
Follow Butler's project inventory, verified pause, health and selective-resume
procedure before replacing Core. Preserve work already paused or blocked.

The new migrations are additive. Selected SQLite/PostgreSQL rehearsals return to
`0028_harness_session_title_lookup`, retain entity payloads/revisions/events and
upgrade again. Use candidate migration code with Core stopped; an older binary
cannot resolve the new Alembic head. Its default pool also needs a compatible
allocation or a validated bounded-pool backport. Retain the coherent rollback
snapshot until promotion and recovery are verified.
