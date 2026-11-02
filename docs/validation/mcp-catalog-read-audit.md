# MCP catalog read performance

Baseline: `fc98996f1f372436dcfccf624add537fad24b250`.

## Acceptance contract

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| MCP discovery | Every eligible external server is discoverable beyond 1,000 Notebook profiles; selected, disabled, unprobed and foreign project servers are omitted | Core profiles and scope | SQL materialization and catalog regressions |
| Supervisor capability discovery | Provider and harness gateway capability reads yield while storage is busy | Core snapshots and bounded read executor | Held-read regression and real gateway IPC |
| Child start | An inherited or empty MCP selection needs no global catalog scan; explicit selections still validate against fresh project state | Core parent snapshot and profiles | Child-start regressions |
| Permission changes and retry | The next read observes disabled, denied, updated or removed profiles | Core store | Mutation and repeat-read regressions |
| Unrelated requests under load | Authenticated reads, 404s and static assets remain responsive during 34 concurrent gateway workloads | Core event loop | Disposable LAN Core comparison |
| Authentication and cleanup | Pairing/bearer auth remains enabled; only synthetic data is used; test Core and data are removed | Core auth and probe lifecycle | LAN receipt |

This changes backend reads and scheduling, without interface, settings, origin,
provider, or harness execution changes. Browser geometry and device gates do not
apply. Async runtime services remain on their owning loop. The LAN probe uses
source Core and synthetic static assets; it does not claim a production UI build,
installed release, live provider or physical-device validation. No production
Core is accessed or restarted.

## Independently validated cause

A fresh-context diagnostic subagent reproduced 2,535 MCP profiles, including
2,526 real 16-tool Notebook definitions. The first 1,000 rows contained 992
Notebook profiles and about 9.13 MB of JSON. Python filtering discarded those
profiles after SQLAlchemy JSON decoding and Pydantic validation, and omitted the
ninth usable external server after the page horizon.

Provider chat preparation already offloads its catalog. Harness
`subagent.capabilities` calls synchronous `available_capabilities` on the gateway
loop, and async child start also performs an inline catalog read. The provider
capability tool has the same synchronous read boundary. An actual Unix gateway
request into the capability service stalled a 5 ms ticker for 384 ms. Eight
inline calls delayed a 10 ms timer by over four seconds. Warm raw SQLite reads
took about 25 ms; catalog CPU time closely matched its elapsed time (530 ms).
This reproduces the shared stall without competing disk writes, swap, or vendor
latency. Those environmental factors may still worsen live latency.

## Related gateway amplification

Filtering and offloading the catalog alone left 1,530 synchronous `get` calls,
204 updates, 102 reservations and 102 receipt completions across 102 gateway
capability requests. An independent instrumented run measured 1.881 seconds
of synchronous gateway/store work on the loop in a 2.653-second burst. The
worst static request overlapped 1.409 seconds of that work. Warming IPC and
removing the diagnostic HTTP trigger both retained the delay.

The fixed capability offer now reads current availability without rebuilding
unrelated mutable tool maps. Its planning guards, profile discovery and receipt
reservation/attachment/completion run as a complete synchronous operation in the
existing four-worker executor. Async execution and mutable gateway tool maps stay
on Core's loop. Explicit child MCP eligibility is read before taking the child
admission snapshot, preserving sibling limits and idempotency after the await.
Inherited and empty selections avoid catalog discovery. No stale profile cache,
database migration, or extra pool is introduced.

## Verification

The final focused plan collects and passes **49 Python cases** (17.96 seconds
locally). It covers SQL materialization, the old page horizon, fresh permissions
and deletion, project visibility, real authenticated Unix IPC, worker-owned store
calls, concurrent child idempotency, provider/harness delegation and the accelerated
gateway's disabled, analysis-only, planner, invalid-argument and enforced-plan
guards. Ruff lint/formatting, mypy over 228 source files and the diagnostic
blind-spot audit pass. No full suite ran. CI reads the same diff-bound selection;
the PR checks are the authoritative CI and distribution-build receipt.

Three existing selected tests failed unchanged on the baseline. A separate
fresh-context investigation found a stale exact tool-set assertion, a hook test
that waited for rows before their subprocesses completed, and a command-runtime
fixture whose default 8,192-token model window was too small for current help
instructions. Repairs are limited to those three test bodies: assert the relevant
tool namespaces, wait for terminal hook executions, and specify a 32,768-token
fixture window. Product behavior was not changed to accommodate those failures.

Local distribution building reached the product-help embedding download, which
this environment's HTTP policy denied. The first draft CI successfully built the
sdist and wheel; the final head must also pass CI before merge. No network-policy
or packaging gate was bypassed.

## Disposable LAN Core load comparison

The same Python 3.12 interpreter and probe exercised baseline and candidate source
Core, with full application lifespans, isolated temporary SQLite data and synthetic
workspaces. Each fixture contained 2,535 MCP profiles, including 2,526 actual
16-tool Notebook definitions and nine external profiles. The ninth external
profile is deliberately beyond the old page horizon. Thirty-four inert harness
bindings each used an authenticated Unix gateway connection, calling production
`_gateway_call`, its planning/policy guards and durable receipts three times.
An authenticated diagnostic HTTP trigger drives those clients. No vendor process
or external MCP service is executed. Concurrent production HTTP routes exercise
ordinary authenticated reads, missing API paths and an actual static file.

The probe requires zero candidate timeouts/transport errors, correct catalogs,
zero on-loop MCP hydration, and sub-second maximum ordinary HTTP and loop lag.
It retained this gate when catalog-only offloading failed it, then passed after
the related preflight/receipt repair. Source SHA-256 values and full measurements
are in [mcp-catalog-read-results.json](mcp-catalog-read-results.json).

| Measurement | Baseline | Candidate | Candidate + 50 ms per catalog query |
| --- | ---: | ---: | ---: |
| Profiles materialized per catalog | 1,000 | 9 | 9 |
| Isolated catalog median (before injected delay) | 717.7 ms | 39.6 ms | 38.4 ms |
| Late external server visible | No | Yes | Yes |
| MCP profiles materialized on loop, 102 calls | 102,000 | 0 | 0 |
| Completed gateway calls | 102 | 102 | 102 |
| Whole concurrent workload | 58.713 s | 3.637 s | 4.158 s |
| Ordinary HTTP p95 | 10,292.2 ms | 13.8 ms | 10.3 ms |
| 404 p95 | 8,327.3 ms | 25.2 ms | 20.7 ms |
| Static-file p95 | 8,330.6 ms | 27.7 ms | 21.0 ms |
| Maximum ordinary HTTP / 404 / static | 10,292 / 8,327 / 8,331 ms | 128 / 137 / 136 ms | 137 / 151 / 151 ms |
| Ordinary / 404 / static 10-second timeouts | 3 / 5 / 5 | 0 / 0 / 0 | 0 / 0 / 0 |
| Maximum 5 ms heartbeat lag | 6,417.9 ms | 41.2 ms | 16.3 ms |

Latency summaries include a minimum of 20 completed lightweight requests per
category; baseline timeouts are counted separately. The candidate completed many
more ordinary requests while the burst ran, as the raw sample counts show. This
is synthetic stress evidence, not a measurement of the operator's host swap or
every storage path. Other gateway tools retain their existing async execution;
this repair targets repeated MCP discovery and capability reads.

Both accepted candidate runs and the baseline exited with code 0. All temporary
Core data and 34 gateway directories per run were removed and their absence
rechecked. LAN origins and cleanup paths are recorded in the JSON receipt.
Authentication stayed enabled and an invalid credential returned 401.

Reproduce with the executor's private non-loopback address:

```sh
git worktree add --detach /tmp/nebula-mcp-baseline fc98996f
PYTHONPATH=/tmp/nebula-mcp-baseline/src timeout 180 .venv/bin/python scripts/benchmarks/mcp_catalog_reads.py --host 172.26.91.237 --label baseline --output /tmp/nebula-mcp-evidence/baseline.json
PYTHONPATH=/workspace/nebula/src timeout 180 .venv/bin/python scripts/benchmarks/mcp_catalog_reads.py --host 172.26.91.237 --label candidate --output /tmp/nebula-mcp-evidence/accepted-candidate.json
PYTHONPATH=/workspace/nebula/src timeout 180 .venv/bin/python scripts/benchmarks/mcp_catalog_reads.py --host 172.26.91.237 --label candidate --slow-read-ms 50 --output /tmp/nebula-mcp-evidence/accepted-slow-candidate.json
```
