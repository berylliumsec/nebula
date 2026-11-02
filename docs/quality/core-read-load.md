# Core search and Work load check

November 2, 2026. Linux, Python 3.12, SQLite/WAL, one Uvicorn process.

The operator journey is reading saved project search results and Work while
another operator reads project settings. Core owns the saved entities, search
projection, resource revisions and device authorization. The browser continues
to own its selected project and navigation identity.

## Reproduction and cause

On `55408f259c1b894aff1f41e3d6c2194b3be35dca`, search and Work list handlers
ran synchronous SQLAlchemy reads and domain processing inside `async def`
handlers. Increasing concurrency delayed unrelated settings reads by seconds.
An independent diagnostic agent reproduced the mechanism and challenged pool,
network, projection-write and serialization explanations. Its warm profile
observed one checked-out connection, no repair writes, 34 SELECTs per 30-result
search and approximately 1 ms of response serialization/compression. Search
materialized 2,000 ORM objects and resolved each result's actions separately.

Moving these reads to the shared four-worker reader exposed a second defect:
concurrent missing-index repairs planned duplicate inserts. Three independent
16-request waves each returned three HTTP 500s. SQL instrumentation identified
`SQLITE_CONSTRAINT_PRIMARYKEY` on `search_documents.id`, rather than lock or
connection failures. A separate regression also reproduced an older repair
overwriting a concurrently saved note.

The repair now shares admission with existing Core readers, reads candidate
projection fields without ORM identities, and validates result actions in a
fresh batched metadata lookup. A store-owned mutex covers the repair decision
through commit across HTTP and separate Butler search services. Atomic projection
upserts check the authoritative source revision and retain newer documents;
cleanup also checks the current owner before deleting a projection. Ranking,
the existing 2,000-candidate bound, pagination and action guards are preserved.

## Disposable LAN evidence

`scripts/stress_core_reads.py` always launches its own Core with a fresh temporary
database, artifacts and data root. It uses 12,000 synthetic notes and 2,000 tasks,
an unavailable OS vault backend, and no providers or executable tools. Normal
pairing and authorization remain enabled; HTTP pairing is allowed only in this
test fixture on the isolated LAN. Anonymous search returned 401 and pairing
returned 200 on both builds.

Each build received two waves at 1, 8, 32 and 64 clients for each of three routes:
630 load requests plus independent settings-read probes every 20 ms. Both
verified runs completed all 12 phases with zero load or probe errors. The
candidate serves source Core; this change has no interface bundle changes.

At 64 clients, in milliseconds unless marked otherwise:

| Loaded route | Request p95, main → candidate | Unrelated settings p95, main → candidate | Maximum loop lag, main → candidate | Requests/second, main → candidate |
| --- | --- | --- | --- | --- |
| `/api/v1/search` (30 results) | 6018.58 → 3542.29 | 5918.61 → 12.68 | 4785.64 → 279.81 | 10.77 → 17.70 |
| `/api/v1/work/items` (500 tasks) | 3266.86 → 1961.57 | 2970.54 → 68.85 | 2420.01 → 290.00 | 21.35 → 32.57 |
| `/api/v1/engagements/{id}/work` (500 tasks) | 2075.29 → 1915.12 | 1735.36 → 65.52 | 1102.69 → 271.34 | 33.99 → 33.01 |

Peak process RSS across the phases was 328.7 MiB on main and 281.1 MiB on the
candidate. Project-list throughput is effectively unchanged in this bounded
run; the principal improvement is preserving unrelated request responsiveness.
Search remains CPU-bound when its queue is saturated. These observations do not
establish a production capacity limit or live-provider performance.

Origins were `http://172.26.91.240:47931` (main) and
`http://172.26.91.240:56199` (candidate). Main was the clean commit named above;
the candidate was its working-tree implementation. The harness fingerprints
`api.py`, `search.py`, `action_registry.py`, `storage.py`, and `read_workers.py`:

- Main source SHA-256: `3948f9789eed8909da6968757c2ea740eca971bf66c0d61a7c482fb6d24296ff`.
- Candidate source SHA-256: `620145016398d002936b27f4114eeb989cd11654f9a85d8538512481c5b8cabc`.

Receipts are `/tmp/nebula-load-study/main-verified.json` and
`/tmp/nebula-load-study/candidate-verified.json` in the execution workspace. Each
records `temporary_data_removed: true`; both owned server processes exited.
An earlier intermediate candidate run had one `httpx.ReadError`; it did not
recur in the verified candidate run. Its transport cause remains unconfirmed.
The harness records failures and does not retry or exclude failed requests.

Exact benchmark commands used:

```sh
git worktree add --detach /tmp/nebula-load-study/baseline-source 55408f259c1b894aff1f41e3d6c2194b3be35dca
.venv/bin/python scripts/stress_core_reads.py --source-tree /tmp/nebula-load-study/baseline-source --output /tmp/nebula-load-study/main-verified.json
.venv/bin/python scripts/stress_core_reads.py --output /tmp/nebula-load-study/candidate-verified.json
git worktree remove /tmp/nebula-load-study/baseline-source
```

Use `--lan-address` for another host's own IPv4 test-LAN address. The script
offers no URL option for targeting a running installation.

## Acceptance and focused checks

| Journey boundary | Observable invariant | Evidence |
| --- | --- | --- |
| Discover/read | Slow search and all five Work list/filter paths leave unrelated settings reads responsive | Six gated real-Core ASGI cases and paired LAN load |
| Create/edit | Saved Work refreshes durably; a search repair preserves a newer note edit | Existing Work persistence test and concurrent edit regression |
| Select/use | Search scope, ordering, privacy, pagination and revision/device action guards remain correct | Search, action registry and Butler search regressions |
| Refresh/reconnect | Reopened Core returns saved Work; repeated search services share repair authority | Work reopen and HTTP/Butler repair cases |
| Failure/retry | Bad cursors retain their errors; a cancelled reader retains physical capacity until its work ends | Cursor, worker-failure and 24-request mixed-burst cases |
| Delete | An overlapping repair does not resurrect a deleted note | Concurrent deletion regression |
| Stream/interrupt | Cancellation of reads is safe; stream implementation is outside this change | Mixed-burst cancellation case |

The reviewed selection in `.github/test-selection.json` contains 25 Python 3.12
cases. No full suite is selected. The changes affect backend read scheduling,
projection writes and metadata lookup; interface, mobile geometry, origin policy,
native integration and live-provider journeys are excluded. Browser/device and
production-UI build gates do not apply to this backend boundary. PostgreSQL load,
physical devices and live providers were not tested.

All 25 selected cases passed locally in 30.73 seconds. Ruff lint and formatting,
the diagnostic blind-spot audit and mypy (228 source files) passed. The local
Poetry package build could not download Chroma's embedding model because the
environment proxy denied its host. The repository CI package build must pass
before merging; this local failure is not reported as a successful build.

Run the exact selected tests with:

```sh
.venv/bin/python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path
plan = json.loads(Path('.github/test-selection.json').read_text())
subprocess.run([sys.executable, '-m', 'pytest', '-q', *plan['python']], check=True)
PY
```
