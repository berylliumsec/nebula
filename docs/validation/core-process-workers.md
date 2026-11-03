# Core process-worker experiment

Experimental branch: `experiment/core-process-workers`, based on `bab0a989`.
The experiment keeps one Core coordinator and sends stateless PDF generation to
bounded child processes. Core retains report/session ownership, queues, approvals,
SQLite transactions, artifact persistence and event publication. The prototype is
injected explicitly by the test fixture; it does not change the shipped default.

## Acceptance contract

| Journey | Observable invariant | Authority | Evidence planned |
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

Measurements and final outcomes will be added after execution.
