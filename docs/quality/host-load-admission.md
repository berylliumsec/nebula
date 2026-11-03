# Host capacity admission contract

The confirmed policy is to defer new chat and Butler jobs when load is **high**.
Core saves the work, keeps it queued with a visible reason, and automatically
starts it when capacity returns. Cancellation must prevent a deferred job from
reaching its runtime; existing work retains its lifecycle.

One Core-owned standard-library sampler serves provider, harness and mission
admission. Sample at most once per second, share refreshes and cached results,
consider CPU quota and available memory, use hysteresis, and cap starts per sample
so a burst cannot all use one stale reading. No subprocess or perpetual sampler.

| Journey | Invariant | Authority | Required evidence |
| --- | --- | --- | --- |
| Send/Butler start | Busy host leaves saved work queued with its reason | Core queue and calibration | unit, real Core, browser |
| Capacity returns | Same work starts once without resubmission | Core producer | integration |
| Refresh/reconnect | Work and queue state remain discoverable | Core durable state | real Core/browser |
| Cancel | Deferred work never reaches provider/harness | Core cancellation | integration |
| Existing work | Admission does not interrupt active work | Runtime owner | integration |
| Missing counters | Unknown measurements cannot permanently strand work | Sampler | unit |
| Cost | Cached reads do no I/O; concurrent callers share a refresh | Sampler | deterministic checks, microbenchmark |

Automatic thresholds have no permission override. Butler discovery and product
guidance must explain deferral. Record unavailable quality gates honestly.

The automatic policy enters pressure at CPU utilization 90% or available memory
below 8%, and recovers below 75% CPU with at least 15% memory available. Linux
CPU accounting uses the visible cgroup quota/usage; memory uses the tighter host
or visible cgroup headroom. Host load average is normalized by host CPU count.
Missing measurements remain explicit and do not permanently strand work. The
FIFO start allowance is twice effective CPU capacity, rounded up and bounded
from 1 to 16 starts per sample, shared across new provider/harness/mission work.

On this four-CPU-quota fixture, the cold sample took 580 microseconds; 100,000
cached calls averaged 0.435 microseconds with zero filesystem reads. This is a
microbenchmark on one host, not a cross-platform latency guarantee. Concurrent
refresh is shared and shielded; diagnostic supervision observes refresh failures
even if every waiting caller cancels. No sampling happens during idle time.

The saved admission reason is returned by pending-turn/session APIs and streamed
queue activity. Reload restores that reason. Stopping a deferred turn invalidates
the interface's authoritative session snapshot so its composer can become ready
from Core's updated state. Butler's work catalog and settings assistant expose
calibration through authenticated GET `/api/v1/host-load`; start responses say
queued until runtime execution is observed.

The focused backend run passed 108 cases, including real-Core direct/Butler
recovery and cancellation, plus a separately collected sampler-failure case.
Three frontend mapping cases passed; production build, lint, formatting, type
checking and diagnostic audits passed. Product-index generation and Chroma digest
synchronization require CI because the local embedding download is denied.
The eight-profile production/LAN browser journey remains a separate acceptance
gate. Physical devices and real model throughput were not exercised.
