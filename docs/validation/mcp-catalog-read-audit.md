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

## Verification

Results and exact reproduction commands will be recorded after validation.
