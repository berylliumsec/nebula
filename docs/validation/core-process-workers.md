# Core process-worker experiment

Experimental branch: `experiment/core-process-workers`, based on `bab0a989`.
The experiment keeps one Core coordinator and sends stateless PDF generation to
bounded child processes. Core retains report/session ownership, queues, approvals,
SQLite transactions, artifact persistence and event publication. The prototype is
injected explicitly by the test fixture; it does not change the shipped default.

## Acceptance contract

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover/select | Saved report IDs and revisions remain authoritative; no new UI control or setting | Core entities | Existing report revision/cache tests |
| Submit/use | An accepted render produces an equivalent PDF and durable receipt | Core coordinator; stateless worker returns bytes | Actual authenticated report API; PDF integrity/content checks |
| Concurrent work | Multiple child PIDs consume more than one CPU while ordinary Core reads and harness activity remain responsive | One coordinator and bounded pool | CPU-time/wall-time ratio, throughput and HTTP/loop latency |
| Saturation | Worker count and pending jobs stay bounded; excess work receives a saved explicit failure | Coordinator admission | Bursts and pool counters |
| Cancellation | Queued work does not execute after cancellation; running cancelled results cannot commit artifacts; slots stay held until work stops | Coordinator futures/receipts | Held jobs and real-render cancellation |
| Failure/retry | An exited worker fails its current job once, is replaced, and later/retried work succeeds without automatically replaying the failed job | Coordinator lifecycle | Injected child exit and retry |
| Refresh/reconnect | Durable render state, integrity and cache identity survive rereads | Core database/artifacts | API polling/download/cache/reopen |
| Shutdown | Core stops accepting work and removes owned children within a bound | Coordinator lifespan | In-flight shutdown and process cleanup |
| Delete/revoke | Existing revision and authorization rules remain authoritative; workers receive no database/service handles | Core services | Report authorization/revision tests; actual 401 check |
| Butler reads host pressure | Interactive and scheduled discovery lead to new authenticated Core reads with current sample age; missing measurements stay unknown | `/api/v1/host-load` and the shared sampler | Project/global/managed read regressions, held-sample concurrency, and live reads during stress |

No layout, browser-origin behavior, authentication boundary or operator setting
changes. Browser/device/production-UI matrices do not apply to this explicitly
injected backend experiment. All stress data, reports, workspaces and credentials
are synthetic. Actual Core runs at private non-loopback LAN origins with normal
authentication. No production process is restarted.

## Stress scope

Compare threaded rendering with 1, 2 and 3 CPU worker processes. Cover small/large
snapshots, cold/warm workers, sustained concurrency, bursts beyond the queue bound,
mixed ordinary reads/harness events, cancellation, worker loss, retry and shutdown.
Repeat throughput comparisons and run a sustained soak. Select only the affected
pool/report/harness boundaries; no full repository suites.

## Independently validated CPU boundary

An independent fresh-context agent profiled the unchanged renderer and replaced
its store/artifact owners with sentinels that fail on access. The hot paths are
Python ReportLab paragraph layout and string-width loops, not SQLite or artifact
I/O. Sixteen warmed PDFs took 3.60/3.67/4.17 seconds with 1/2/4 threads and used
about one CPU. The same snapshots took 3.65/2.43/1.51 seconds with 1/2/4 spawned
processes, using 1.00/1.98/3.89 CPUs. All PDF hashes matched. Thread runs had no
quota throttling; four processes recorded only 0.012 seconds of throttling.

The worker entry receives a plain saved snapshot and font identity and returns
PDF bytes/warnings. It never receives a service, database connection, session,
credential store, artifact writer or event subscriber. Core admits jobs by both
count and serialized snapshot bytes, runs start callbacks in the originating
request context, and commits all durable results. Queue overflow is an explicit
saved failure. Cancellation removes queued jobs; running cancellations retain
their slots until completion and discard their returned bytes. A worker exit
fails one job and starts a replacement for later work, without automatic replay.

## Real-Core comparison

All variants use the same candidate source, interpreter and synthetic fixture.
The control leaves `cpu_workers=None`, preserving the ordinary threaded render
path. The environment provides four CPU-equivalents, five affinity CPUs and a
16-GiB memory limit. Every server uses an available private non-loopback port,
normal Core authentication and disposable data; all recorded successful servers
and their children exited and their temporary directories were removed.

Each large-report trial completes eight PDFs at concurrency four while an inert
harness produces 40 start/completion pairs and the driver polls ordinary reads
and Butler's live host-load operation. Two trials run per variant, with cold and
small-report phases measured separately. Every variant matches all 33 comparable
PDF hashes. Full results, PIDs, source hashes, timing, queue counters and CPU/RSS
data are in [core-process-worker-results.json](core-process-worker-results.json).

| Renderer | Mean time for 8 large PDFs | Total effective CPUs | Ordinary read p95, two trials | Mixed harness duration |
| --- | ---: | ---: | --- | --- |
| Threaded control | 19.04 s | 1.05 | 2,389 / 3,466 ms | 19.22 / 19.84 s |
| 1 process | 13.78 s | 1.38 | 12.62 / 12.61 ms | 1.46 / 1.33 s |
| 2 processes | 8.23 s | 2.39 | 22.58 / 19.37 ms | 1.79 / 1.41 s |
| 3 processes | 6.41 s | 3.11 | 33.91 / 33.66 ms | 1.82 / 2.00 s |

Two workers provide 2.32× large-report throughput versus the threaded control;
three provide 2.97×. Two are the recommended next activation candidate for this
four-CPU host: three spend another CPU and about 100 MiB for additional throughput
while increasing ordinary-read latency. This is a measured PDF boundary, not a
claim that all Core workloads scale by these factors.

Workers also have startup cost. Authenticated Core readiness was 4.60 seconds for
the threaded control, 4.59/5.37/5.61 seconds for 1/2/3 processes. The first small
render batch took about 0.60 seconds with processes versus 0.13 seconds for the
threaded control, including child renderer/font initialization. Persistent warm
workers are needed; creating a process per request is not the tested design.

## Overload, fault injection and soak

A 32-request burst with six queued slots accepted 7/8/9 jobs for 1/2/3 workers
and saved explicit queue-full failures for 25/24/23. The bound held at 7/8/9
admitted jobs, and all slots and byte reservations returned to zero. All process
variants completed this burst without ordinary-read timeouts.

The earlier threaded overload attempts are retained as limitations, not included
in the throughput comparison: the first driver stopped on an ordinary-read
timeout at 15 seconds; a repeat stopped on an interrupted polling connection.
Its final Core state was not captured, so that connection failure is not
attributed to a particular Core mechanism. No cgroup OOM events were recorded.
The final threaded comparison omits overload and completes normally. The driver
now retains each completed phase and counts read timeouts/transport failures;
safe polling GET retries are recorded separately from render submission.

Across process variants, eight worker exits were injected into owned active
children. Each affected render saved one failure, explicit retry produced a valid
PDF, and a replacement PID handled later jobs. The focused tests also cover
queued/running cancellation, capacity retained by cancelled running work,
unexecuted cancelled jobs, startup timeout, shutdown during startup, loop
ownership, ordinary job exceptions, request-context callbacks and forced bounded
shutdown. Late cancelled bytes cannot create a PDF artifact.

The two-process soak requested 180 seconds and completed 23 batches over 186.71
seconds of batch work: 184 large PDFs, 6,126 ordinary reads, 776 fresh Butler
host-load reads and 920 inert tool calls. No HTTP timeout, transport error,
unexpected job failure or stuck admission occurred. Every batch's ordinary-read
p95 was at most 31.67 ms; the median batch p95 was 19.83 ms. Worker RSS stayed
near 98 MiB; Core RSS moved from 286.3 to 299.0 MiB while durable history grew.
This short soak does not establish absence of leaks over days of operation.

## Butler host-load capability

`nebula.work.host_load` is discoverable through interactive Butler search,
describe/read, the managed work catalog and Settings destination guidance.
Scheduled Nebula-wide Butler also exposes `nebula.global.host_load`. Each
invocation calls authenticated `GET /api/v1/host-load`; neither action stores its
own result cache or launches work. Returned fields include CPU capacity/ratio,
available-memory ratio, busy reason, measurement availability and sample age.
Core intentionally retains its shared one-second sampling cache and admission
budget. Eight simultaneous expired-sample reads share one off-loop refresh while
an unrelated authenticated request remains responsive.

The model-backed help resource was rebuilt and verified in
[the isolated CI build](https://github.com/berylliumsec/nebula/actions/runs/41706743110)
because the local environment denies the model download host. The generated
resource is intentionally gitignored and regenerated by the existing package
build hook. The local resource verifies 346 passages at source digest
`86db445c8a6ec67b68f4fb9fae3d827e60104921ab106b068f2bbbda46ba8895`.
Focused verification includes packaged digest, help search and Chroma mirror
sync, plus an actual Core startup that replaces stale Chroma guidance metadata.
The startup regression prepares its generated resource explicitly: CI's Poetry
source install does not invoke the package build hook. An independent clean
checkout reproduced stale Chroma metadata with a missing-resource diagnostic;
restoring the generated resource synchronized all 346 passages. Preparing the
resource in this test removes its dependency on a later module's build fixture.

## Commands and gates

Run with an executor's private LAN address; the script always launches its own
Core and cannot target a running installation:

```sh
PYTHONPATH=src .venv/bin/python scripts/benchmarks/core_cpu_workers.py --host 172.26.91.252 --workers 0 --rounds 2 --jobs 8 --burst 0 --faults 0 --output /tmp/core-cpu-threaded-comparison.json
PYTHONPATH=src .venv/bin/python scripts/benchmarks/core_cpu_workers.py --host 172.26.91.252 --workers 1 --rounds 2 --jobs 8 --burst 32 --faults 2 --output /tmp/core-cpu-process-1.json
PYTHONPATH=src .venv/bin/python scripts/benchmarks/core_cpu_workers.py --host 172.26.91.252 --workers 2 --rounds 2 --jobs 8 --burst 32 --faults 3 --soak-seconds 180 --output /tmp/core-cpu-process-2.json
PYTHONPATH=src .venv/bin/python scripts/benchmarks/core_cpu_workers.py --host 172.26.91.252 --workers 3 --rounds 2 --jobs 8 --burst 32 --faults 3 --output /tmp/core-cpu-process-3.json
```

The initial CPU-worker validation selected 72 Python cases and one frontend
metadata case. The [shared-resource follow-up](core-resource-budget.md) adds 13
model-boundary cases; the current diff-bound receipt therefore selects 85 Python
cases. Python selectors cover pool/report/harness contracts and Butler host-load,
catalog/help and product-index integration; CI selects Python 3.11/3.12/3.13.
Frontend collection/execution uses only `src/settingsCatalog.hostLoad.test.ts`.
The final local selection passes all 72 Python cases in 58.72 seconds and the
single frontend case. Mypy passes all 232 Core source files, Ruff lint passes,
formatting passes all 527 selected source/test files, and the Python diagnostic
audit reports zero unclassified handlers or ignored results. The generated help
resource verifies, including its packaged digest, direct Butler help search,
Chroma synchronization and actual Core startup checks.
The production UI build and workflow-registry prebuild check pass. Browser
geometry/device testing is outside this backend/read-discovery change; no new
interactive controls or browser-origin behavior were introduced.

The prototype remains explicitly injected in the stress fixture, with no shipped
default change or production deployment. Frozen Core sidecar startup and native
macOS/Windows process behavior are not validated. Those are required before
activating this pool in packaged product builds; the experimental branch does
not claim that rollout is ready.
