# Synthetic capability saturation study

The contract is that operators can inspect saved project state, read chat
history, download evidence, and save notes and workspace files while other
operators load the same Core. Saved revisions, conflicts, deletion, transcript
order and artifact bytes must remain correct. Cheap project-settings reads must
remain responsive during expensive work and recover after saturation.

Core SQLite/WAL owns records, revision checks, search projection and event
history. The artifact store owns immutable evidence bytes; the workspace owns
files. The workload uses real HTTP, pairing and device cookies. It owns a fresh
temporary database, files and server process; no installed Core is replaced.

| Journey boundary | Invariant | Evidence to capture |
| --- | --- | --- |
| Discover/read | Large lists, overview, search and event replay remain correct | Per-scenario saturation and settings probes |
| Create/edit | Independent concurrent writes persist; a stale revision is rejected | Note create, patch, conflict and fresh read |
| Select/use | History is ordered, bounded and owned by the requested conversation | 50-message page from a large transcript |
| Refresh/reconnect | A new authenticated connection reads the saved note | New connection after load |
| Failure/retry | Every failed operation is counted; recovery is measured after load | Error samples, totals and recovery phase |
| Delete | Deleted notes are absent; upload bytes match before removal | Full synthetic record/file lifecycle |
| Evidence/export | Artifact bytes and bundle CRCs remain valid | Download and bounded concurrent export |
| Stream/interrupt | Existing scheduler/replay/cancellation contracts remain intact | Focused backend regressions; live runtime load remains unverified |

`scripts/stress_capabilities.py` exercises 26 explicit scenarios and reports its
exclusions. It accepts no destination URL or existing data directory. Start with
one client to validate the fixture, increase concurrency on identical data, and
run a sustained mixed workload. Each report records source and harness hashes,
fixture counts, origin, percentiles, errors, settings probes, SQL timing, loop
delay, CPU, peak RSS and process/data cleanup. Latencies include failed
operations, whose counts are reported separately. These are closed-loop waves,
not a fixed-arrival-rate production capacity estimate.

## Findings and repair

On November 3, 2026, the generic Notes, Findings, Evidence and Reports list
handlers enriched each entity with its own relationship query and Session.
A 100-row page therefore issued 100 extra queries even when it had no edges.
Independent component profiling measured 156–200 ms for that enrichment versus
0.6–1.6 ms for JSON serialization. Warm load phases rule out cold serializer
construction as the principal cause.

The repair groups relationship reads by owner and entity type, chunks entity
IDs below SQLite's bind limit, and streams all matching edges. It preserves
reciprocal finding/evidence relations, authoritative arrays, ordering, project
isolation and more than 500 edges. A 100-row page now uses one relationship
query: 128 queries for 128 responses instead of 12,800.

Overview and mission-event replay performed synchronous storage work inside
async handlers. Both now use the existing four-worker reader with worker-owned
Sessions. Artifact download preflight synchronously hashed the full blob on the
request loop; the complete handler now runs in FastAPI's worker pool. Integrity,
authorization and acknowledgement checks remain enforced.

The separate diagnostic agent independently confirmed these mechanisms before
product edits. Its component receipt is
`/tmp/nebula-capability-study/independent-cause-components.json`.

## Matched small-fixture study

Linux, Python 3.12.14, SQLite/WAL, one Uvicorn process, four CPU quota and 16 GiB
memory limit. Three synthetic projects contain 3,000 notes, 600 tasks, 6,000
messages in 30 chats, 600 assets, 300 findings, 30 reports, 12 blobs, 30 browser
sessions and 600 workspace text files. The only provider is disabled. No model,
command or security tool executes. Both builds use the identical harness hash.

There are 26 scenarios, two waves at 1/16/64 clients (exports bounded to 1/4),
a 20-second mixed phase and ten recovery reads: 79 phases per build. Settings
probes run on a separate connection every 20 ms. Authentication and pairing are
active; anonymous reads returned 401 and pairing returned 200.

All values below are milliseconds at 64 clients. Percentiles include failed
operations; the observation baseline includes 52 requests that reached the
60-second client timeout.

| Loaded boundary | Request p95, baseline → candidate | Settings p95, baseline → candidate |
| --- | --- | --- |
| Global overview | 1940.16 → 993.67 | 1226.96 → 135.87 |
| Project overview | 1885.14 → 2063.28 | 504.83 → 624.52 |
| Notes (100 rows) | 60974.99 → 1444.59 | 661.54 → 76.40 |
| Findings (100 rows) | 34218.87 → 1511.64 | 110.04 → 156.88 |
| Event replay (100 events) | 1058.52 → 905.34 | 593.58 → 164.92 |
| Artifact download (>1 MiB) | 1231.75 → 1440.90 | 158.79 → 112.15 |

The baseline recorded 53 failed operations out of 4,518: 52 note-read timeouts
and one unresolved note-lifecycle transport ReadError. The candidate recorded
one pre-response search transport failure out of 4,582 operations; every settings
probe succeeded. These failures are retained, and neither run is described as
error-free. Operation totals differ because the mixed phase is duration-based.

Timed-out baseline note handlers continued after their clients disconnected;
the next Assets phase contains 2,301 leftover note relationship queries. Do not
interpret that phase as an isolated Asset regression. Its backlog contributes
to total study time. Findings began after the backlog had cleared.

The mixed-phase request/settings p95 was 3431.90/365.22 on baseline and
2725.66/456.70 on candidate. Recovery read p95 was 14.11 and 24.35 respectively.
Not every metric improved: project overview and artifact request p95 increased,
and saturated mixed settings p95 increased. No repeated-trial significance or
production capacity claim is made. Peak RSS was 585.57 and 599.51 MiB; the largest
peaks occurred around eager bundle exports. This does not establish a leak or a
memory improvement.

A separate local transport fixture reproduced HTTPX/Uvicorn h11 EOF-before-header
failures on 3/64 reused connections when both idle expiry limits were 50 ms;
a 5 ms client expiry versus 50 ms server expiry produced 0/64 failures. Ports
and timestamps corroborate the close/reuse mechanism. The original candidate
incident had only 127 authentication/search executions for 128 attempts, but
lacks socket traces, so its attribution remains unconfirmed. Raw traces and
analysis are `independent-transport-probe.json` and
`independent-disconnect-diagnosis.json` in the study directory.

An initial 256-client diagnostic also encountered transport errors and was
stopped; its retained receipt records cancellation and cleanup. The final
harness explicitly retires client sockets after one idle second, earlier than
the unchanged Uvicorn five-second limit. No request retries or error filtering
were added. The four harness checks passed again after that change.

## Extreme profile

The larger run uses four times the seeded data (about 42,000 records), 256
clients per wave, every scenario, and a 120-second mixed phase. Exports remain
separately bounded at four clients. It completed 14,868 operations with 146
failures, all note-lifecycle timeouts; all 6,141 settings probes succeeded.
The mixed phase completed 2,048 operations without errors, followed by ten
successful recovery reads. Peak RSS was 1,128.09 MiB. Process and data cleanup
succeeded. The receipt is `candidate-extreme-final.json`; despite its filename,
it predates the additional repairs below and is not an error-free final result.

Independent profiling traced the write bottleneck to delete validation, which
hydrated all 1,200 Findings and 120 Reports on every note deletion. At 16 clients,
this consumed 75.4% of measured server CPU; connection-pool wait and diagnostic
logging were not material contributors. Storage now filters scalar and array
references in SQL before hydrating possible matches. Validation still checks
exact references, including legacy cross-project references and corrupt rows.
Engagement deletion retains its complete ownership scan. The equivalent
PostgreSQL path guards non-array JSON values and passed an actual PostgreSQL 16
regression on an isolated synthetic database.

The overview aggregate also counted the text primary key through a secondary
index, requiring an unconditional table lookup for each row in SQLite. Counting
rows instead removes that lookup without changing counts. It does not eliminate
the project overview's grouping and filtering work. These causes were validated
independently before repair. A focused repeat of both overview scenarios and
the full note lifecycle uses the same scale and 256-client concurrency.

This profile stresses one owned Core on the same host, not a distributed
deployment.

## Validation and limits

The reviewed selection covers 111 Python 3.12 cases: the original 57 for
performance and preserved storage/runtime contracts, host calibration and saved
queue state, SQL reference filtering, existing CRUD validation, Butler startup,
help discovery and packaged-product/Chroma digest synchronization. Eight new
deterministic performance reproductions failed on the baseline. No full suite
was selected or run. The final diff-bound receipt records exact selectors.

The host-load feature adds a durable queue explanation to the API and interface.
Its production-bundle real-Core browser journey pairs a synthetic device at a
non-loopback LAN origin, submits work under controlled pressure, reloads, releases
pressure, verifies exactly one response, then cancels a second deferred request.
Eight permanent browser profiles select this exact journey: desktop Chromium at
1440/1024 and emulated mobile Chromium/WebKit at 320/390/430. See
[the host admission contract](host-load-admission.md) for the policy and evidence.

The production UI build passed. Local package generation and build remain
blocked by HTTP 403 from the environment proxy while Chroma downloads its embedding
model. Mobile WebKit installation is likewise blocked by denied browser download
hosts. CI must supply the package/source-digest, Chroma synchronization and WebKit
evidence before merging; these are missing gates, not passing results.

Live provider/harness/model throughput, command/container/SSH/VPN execution,
voice, Chroma embedding throughput, physical devices, fixed-arrival traffic and
multi-hour soak are **not stress-verified**. PostgreSQL has focused reference
correctness evidence, not a saturation study. This study is not an exhaustive
certification of every Nebula capability.

Cookie authentication still performs a synchronous lookup on the request loop.
Search remains CPU-bound at high queue depth, eager bundle exports increase
memory, and extreme saturation may still delay unrelated requests. These are
residual investigation areas; the current fixes do not claim to eliminate them.

## Reproduce and inspect receipts

```sh
# Both builds: the baseline worktree contains the unmodified c9b3ef62 Core.
.venv/bin/python /tmp/nebula-capability-study/stress_capabilities-matched.py --source-tree /tmp/nebula-capability-study/baseline-source --concurrency 1 16 64 --waves 2 --mixed --soak-seconds 20 --output /tmp/nebula-capability-study/baseline-verified.json
.venv/bin/python /tmp/nebula-capability-study/stress_capabilities-matched.py --source-tree /workspace/nebula --concurrency 1 16 64 --waves 2 --mixed --soak-seconds 20 --output /tmp/nebula-capability-study/candidate-verified.json
# Larger run before the SQL reference/count repairs; earlier socket retirement, no retries.
.venv/bin/python scripts/stress_capabilities.py --scale 4 --concurrency 256 --waves 2 --mixed --soak-seconds 120 --output /tmp/nebula-capability-study/candidate-extreme-final.json
# Focused repeat after the SQL repairs.
.venv/bin/python scripts/stress_capabilities.py --scale 4 --concurrency 256 --waves 2 --scenario overview-global --scenario overview-project --scenario record-lifecycle --output /tmp/nebula-capability-study/candidate-extreme-repaired.json
```

The matched receipts were produced with the earlier default client expiry;
the verified original snapshot is retained as `stress_capabilities-matched.py`;
the current harness records its one-second expiry in configuration. Preserve
that distinction when reproducing the study. The small matched source hashes
fingerprint all top-level Core Python modules; the candidate is a working-tree
implementation of baseline commit `c9b3ef62`:

- Baseline source: `f80fb40f1490fd48ca99014b079ec5366ac5452f11e07e8631f97db3dc8541b1`.
- Candidate source: `bd762820bd4b943854df58f383a3233781d453e1cf49d16616530b5507967f2b`.
- Matched harness: `8420109f68d3e9d428b7469822ee74a1b2a81c12ed384744b1321bb2df9c7667`.
- Baseline origin: `http://172.26.91.242:52099`.
- Candidate origin: `http://172.26.91.242:38409`.

Machine receipts and logs are under `/tmp/nebula-capability-study`. Each completed
or intentionally stopped run records process termination and removal of its
owned temporary data. Earlier pilot receipts retain fixture mistakes and are
excluded from the numerical comparison. Review `.github/test-selection.json`
and its diff-bound receipt before running the exact selected tests.
