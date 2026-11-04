# Core performance telemetry acceptance evidence

## Contract and scope

The developer or Butler reads current Core timings through authenticated
`GET /api/v1/diagnostics/performance`. The existing live diagnostics export adds
the same snapshot as `performance.json`. No UI controls, saved preferences,
provider calls, or durable telemetry tables are added.

| Journey | Observable invariant | State authority | Evidence |
| --- | --- | --- | --- |
| Discover | Butler's Settings API schema advertises the read-only performance route | Core route registry | focused API regression |
| Read/use | Snapshot exposes bounded counts, recent percentiles and slow request phases | process-local collector owned by Core's database | collector, SQL, worker and real-Core tests |
| Correlate | Slow response request ID matches `X-Request-ID`; phases include worker and SQL work | ASGI request context propagated to workers | focused API and LAN regressions |
| Refresh/reconnect | A fresh client sees the same running process's counters | Core process | real-Core LAN regression |
| Failure | Missing/wrong credentials return 401; failures and cancellations preserve normal behavior | Core authentication and owning operation | LAN, SQL, worker and provider admission tests |
| Stream | An idle SSE lifetime does not become a slow response | ASGI stream lifecycle | deterministic regression and existing chat gzip/timing tests |
| Export | Snapshot bytes are included in the existing ZIP and SHA-256 manifest | Core diagnostic exporter | API and LAN regressions |
| Stop/restart | Monitor is stopped with lifespan; a new collector starts empty | Core lifespan | timer unit test, isolated collectors and disposable-Core shutdown |

Create/select/delete settings and durable telemetry recovery are inapplicable:
this is a read-only diagnostic surface and measurements deliberately reset with
the process. Existing UI presentation, browser layout, authentication protocols,
cookies, service workers and native code are unchanged. Browser/device matrices
and a production frontend bundle are inapplicable to these API-only changes.

## Selected verification

The exact reviewed selection is `.github/test-selection.json` (31 collected
Python 3.12 cases). The receipt is generated with:

```console
.venv/bin/python scripts/test_selection.py --baseline origin/main --candidate WORKTREE --output /tmp/nebula-performance-selection.json
```

Results: 28 distinct selected backend cases passed. The telemetry files were
rerun after correcting two test fixtures to use the existing
`NebulaStore.list_entities` API; all 11 telemetry/LAN cases passed. An independent,
fresh-context reviewer validated that both fixture failures arose before worker
execution from the absent `store.list` attribute, ruling out authentication and
instrumentation as their cause. Other selected diagnostics, chat gzip/timing and
provider admission contracts passed without changes.

Ruff lint/format on touched files, mypy on the seven touched source files,
`git diff --check`, and the Python/Rust diagnostic blind-spot audit passed.
A warmed single-thread collector benchmark took approximately 1.85 microseconds
per completed measurement (100,000 records, best of three). This is a collector
microbenchmark, not an end-to-end overhead guarantee.

## Non-loopback Core journey

Command:

```console
.venv/bin/python -m pytest -q tests/v3/test_performance.py tests/v3/test_performance_core.py --capture=tee-sys --tb=short
```

Observed origin: `http://172.26.91.247:38967`.
Build: source development Core, version `3.0.0-beta.3`, target `linux-x86_64`;
the development build metadata reports commit `unknown` and timestamp
`development`. Tests execute this branch's source directly; no frontend bundle
or packaged-release identity is claimed.

Fixture: isolated temporary database, diagnostics directory and synthetic
project; normal bearer authentication; no provider/harness calls or operator
data. The journey rejected missing/wrong authentication, created/listed a
project, matched the list response's request ID and SQL phases, refreshed the
snapshot through a new HTTP client, verified the exported snapshot's manifest
hash, deleted the synthetic project and shut down Core. Temporary export ZIPs
were removed. Test temporary directories remain under pytest's normal retention.

## Missing required gate

Product-help generation and the three selected source-digest, telemetry-search,
and Chroma synchronization checks are blocked. The official ONNX embedding
model download from `chroma-onnx-models.s3.amazonaws.com` receives HTTP proxy
403. The existing bundled index has not been relabeled or supplied with fake
embeddings. Rebuild it with `scripts/build_product_knowledge.py`, verify it, and
run those three checks once the model is available. Until then the change is
partially verified and must remain unmerged.
