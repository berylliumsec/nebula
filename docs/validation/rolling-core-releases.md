# Rolling Core release rehearsal

This file is the evidence for [the rolling release design](../design/rolling-core-releases.md).
All runs used a disposable scratch Core on this development host (November 14,
2026). It ran in supervisor mode on PostgreSQL 16 with one execution worker per
project. The model was a local OpenAI-compatible stub
(`scripts/deploy_rehearsal/stub_model.py`) streaming 40 chunks 500 ms apart,
so each reply took about 21 s. No live or shared Core was involved.

## Method

`rehearse.py` sets up each run:
- It opens a fresh chat per selected project with a marked message
  `[rh:<run>:p<i>:m0]`.
- It queues marked follow-ups through the durable chat queue
  (`POST /chat/sessions/{id}/queue`) while the reply streams.
- It runs the disruption.

Viewers behave like the UI: a cut stream resumes from its last cursor via
`/chat/turns/{id}/events`.

After every turn and queue item settles, the run reads PostgreSQL and the
stub's request log. A message passes only if all of these hold:
- exactly one user message;
- exactly one provider request;
- one completed provider stream;
- one `complete` turn;
- every queue item `complete`;
- no `error` frame on any viewer stream.

`prober.py` calls `/health` every 100 ms for the whole deploy, recording status
and latency.

Every run had two accommodations; neither is part of the change:

1. **The chat-queue `await` fix** (three lines in `chat_queue.py`). On `main`
   since #939, every follow-up dispatched from the durable queue is dropped,
   because `start_provider_turn`, `harness.prepare_chat` and
   `harness.start_chat_turn` are not awaited. The worker's stderr shows
   `RuntimeWarning: coroutine 'ChatService.start_provider_turn' was never
   awaited`, and the turn stays `queued` at revision 1. `main` fixed this
   independently in #1017 while this work was in progress; the rehearsal
   builds carried an equivalent patch. `build_release.py` skips it when the
   source already awaits.
2. **Host-pressure thresholds above 100%** (rehearsal builds only, E4–E6).
   Other work saturated this shared 8-core host (load average about 30).
   Without this, the host-pressure gate held every admission ("runnable tasks
   462% >= 100%"). The host-pressure RPC still ran on every admission.

`build_release.py` applies both to its bundle copy only.

## Results

| Run | Code | Disruption | Messages passing | Other observations |
| --- | --- | --- | --- | --- |
| Baseline | `main` + fix 1 | none | 4/4 | — |
| E1 | `main` + fix 1 | SIGTERM the supervisor only; workers untouched; restart | 3/9 | 3 streaming turns finished. 6/6 follow-ups dispatched in the gap went to `needs_review` and paused their queues (browser-bridge `FileNotFoundError`; in another run, host-pressure 503 → turn `failed`). API back after 84 s, 7 failed starts on `serve`'s TIME_WAIT probe |
| E2 | `main` + fix 1 | SIGTERM every Core process; restart | 6/9 | All 3 viewers got `chat_stream_failed`. All 3 first turns were re-requested (2 provider requests each) and the partial text was lost. API back after 85 s |
| E3 | `main` + fix 1 | Existing operations: stage, migrate, API, target, then 4 serial handoffs (76 s) | 8/9 | 1 follow-up ended `needs_review` "Core restarted during dispatch": the source claimed it just before transfer |
| E4 | This change, behind a held listener | Full roll A→B: stage, migrate, supervisor generation swap, API, target, 4 handoffs (132.8 s) | 9/9 | 6/6 queue items complete. Each viewer cut once at the swap and resumed. 1051/1051 probes OK, max 19.3 s |
| E5 | This change | Same roll B→C, started while admission was held, including one cold project; release at +75 s (204.8 s) | 6/6 | 1682/1682 probes OK, max 20.9 s. One project reported "A queued follow-up is being dispatched" for 17 s, then transferred cleanly |
| E6 | This change | C→D handoff of one cold project whose `http.invoke/v1` chat start was still queued | 2/2 | Turn queued at 13:14:50; project transferred at about 13:15:21; turn admitted on the target at 13:16:14; one provider request. The start command completed on the source. The follow-up enqueue waited 29 s behind it on the work lane, then ran on the target |
| E7 | After the review fixes | A→B host activation behind the holder; prepare_api then timed out under load average 44 | 9/9 | The supervisor generation swap took 38 s. 835/835 probes OK (max 37.6 s), none refused. The API worker missed the existing 20 s readiness bound twice, leaving `uncertain` receipts; no work was affected |
| E7c | After the review fixes | Same roll resumed with a new operation ID: API blue/green, target, 4 handoffs (152 s) | 9/9 | 6/6 queue items complete, 0 error frames; `resident_versions` is the new release only |

| E8 | Holder-owned host services (this change) | 4 supervisor generation swaps behind a holder that keeps both sockets, while 4 loops call `host.services/v1` as a retained worker with a client that never resends | — | 2151/2151 calls OK, slowest 5.6 s (queued during the swap); with the workers' resending client, 2 more swaps: 1221/1221 OK. The desktop bridge registration was refreshed with its ID after each swap: 4/4 200 with the same ID |
| E8 baseline | Same code, holder keeps only the public socket | 3 swaps, same probe | — | 1180 of 2724 calls failed: 1143 missing socket, 24 refused, 13 undispatched 503s. With the workers' resending client instead: 1556/1556 OK |
| E8 `main` | `main` (2276020) | 2 swaps, same probe | — | Bridge refresh with its ID: 409 "no longer current" after each swap, which makes the desktop drop the browser until it is reopened |
| E8 chats | This change, both sockets held | `rehearse.py`: 3 streaming chats with 2 queued follow-ups each, supervisor swap 2 s after streaming | 9/9 | 0 violations, settled 10 s after the swap; no error diagnostics in worker or Core logs during the swaps |

E8 ran on November 21, 2026 (scratch Core on PostgreSQL, stub model,
`scripts/deploy_rehearsal/host_swap.sh` and `host_services_probe.py`, the
holder from `listener_holder.py`). Before the stopping generation stopped
accepting first, the first E8 run had 11 undispatched 503s out of 1590 calls,
all answered by the old generation while it closed its services. A second run before the review fixes gave 2023/2023.

Additional detail for E4–E6:
- Host activation took 19.4 s and 21.2 s. Connections queued in the kernel
  backlog instead of being refused.
- E2's error frames and E1/E3's reviews did not recur.

## Unit and integration tests

These ran with a pidfd-capable Python 3.12, on SQLite and on PostgreSQL
(`NEBULA_TEST_POSTGRES_URL`). The interpreter was a per-worktree venv whose
`.pth` resolves `nebula` to this branch's `src`, so worker and Core processes
spawned by the tests import the branch code, not the primary checkout. The
final run used `red-suites/mkvenv.sh`. Baselines used the same kind of venv
pointed at a clean `main` worktree. At load average 35–48, the tests that start
a real API worker can miss the existing 20 s readiness bound. That happened to
two `test_supervisor.py` cases and `test_paired_browser_repairs_memory_before_real_api_admission`.
The two supervisor cases fail identically on clean `main` with the same venv
and load. All three pass in CI.
- `tests/v3/test_project_handoff_agent.py`: an in-flight follow-up dispatch
  keeps the project finishing and transfer refuses.
- `tests/v3/test_runtime_host_services.py`:
  - a host observation waits for a replacement host socket;
  - the wait is bounded;
  - calls with effects are sent once;
  - an operation's own 503 is not resent;
  - three turns queued behind a missing host fail together within one window,
    and share one call when the host answers;
  - peer-capacity and peer failures fail after one attempt, while a closed
    gateway still marks its 503 undispatched.
- `tests/v3/test_runtime_host_services.py`, holder-owned host services:
  - adoption accepts only a listening, owner-only Unix socket at an absolute
    path, and leaves a rejected descriptor open;
  - a call made while no generation serves waits in the holder's queue and is
    answered by the next generation, with a client that never resends;
  - a self-bound generation replaces a holder link with its own socket;
  - `RemoteProtocolError` is resent for read-only and idempotent calls,
    `ReadTimeout` for read-only observations only (never a Butler submit);
  - a call older than 25 s is refused as undispatched before it reaches any
    service, and a failed start answers waiting calls at once.
- `tests/v3/test_runtime_browser_bridge.py`: the bridge registration survives
  a generation swap and a real supervisor restart, keeps its remaining
  lifetime, is removed on revoke, and an expired or damaged file is
  discarded.
- `tests/v3/test_runtime_service.py`:
  - an inherited listener queues connections between generations;
  - an explicit fd must match an IP-literal `--host`/`--port`, so a wildcard
    is refused;
  - a wrong descriptor (a socketpair end, a pipe) is refused without being
    closed or modified;
  - the restart probe ignores TIME_WAIT but refuses a live listener;
  - a self-bound listener gets no drain;
  - under a real uvicorn stop with a holder, the drain closes an answered
    stream but a request still awaiting its receipt receives its 202;
  - a client that stopped reading a large response cannot hold a
    holder-backed stop open: its connection is aborted after the flush
    window, with uvicorn's own timeout disabled in the test.
- `tests/test_deployer_protocol.py`: the deployer's `deployment_post` honours
  a 503 `Retry-After` and resubmits the same operation, and still raises a 503
  that carries no `Retry-After`.
- `tests/v3/test_runtime_management.py`: a deployment operation whose request
  is cancelled answers a retryable 503 with its operation ID, and still records
  `succeeded` before the host releases authority.

Each regression test for the three defects, and the operation-completion test,
fails without its fix. Failures in the touched suites
are identical on clean `main` and this branch: the two product-guidance mirror
cases and the API-peer 404/401 case in `test_runtime_host_services.py`, each on
both databases.

## In-Core roller (November 21, 2026)

**Setup.**
- A disposable scratch Core on this host, in supervisor mode on its own
  PostgreSQL 16 database.
- It listened on the LAN address (`--host 192.168.1.155 --allow-remote`,
  port 18560) with normal bearer authentication.
- Four synthetic projects. The same slow stub: 40 chunks × 500 ms, about
  21 s per reply.
- Core environment: `NEBULA_ROLL_PROJECTS_AT_ONCE=2` and
  `NEBULA_ROLL_OPERATOR_QUIET_SECONDS=5`, so the rehearsal does not wait
  ten minutes after the operator operations.
- The supervisor ran with the scratch-only host-pressure shim
  (`NEBULA_SCRATCH_IGNORE_HOST_PRESSURE=1`); it is not product code. No
  listener holder was used and the supervisor was not swapped: this
  rehearses step 5 only.

**Method.** Each run used `rehearse.py --followups 2` on all four projects.
Its disruption was `roll.py <release> --only none-such`, which performs
stage, migrate, `prepare_api`, `promote_api` and `target_execution` and
requests no handoff. Every project move was therefore the roller's. A
poller recorded `roll` from `/api/v1/deployment/status` every 500 ms. The
verifier is the one described under Method above.

| Run | Code | Roll | Messages passing | Observations |
| --- | --- | --- | --- | --- |
| R1 | First roller version | A→B, cold projects | 6/12 checked, 2 unchecked | `deferred` until 5 s after the last operator operation, then 2 projects at a time; 4/4 projects moved and 4/4 sources retired in 50 s. p1 and p2 were perfect. For p0 and p3 the harness read no session ID from the cold-start stream, so it queued no follow-ups. Their first turns completed with one provider request each |
| R2 | First roller version | B→C, warm | 12/12 | 0 violations; roll complete 66 s after targeting. Found a defect: the B sources had been the roller's own R1 targets, so they were retired by the spare-target path as soon as they owned nothing, before verification |
| R3 | After the review fixes; Core restarted on them | C→D | 9/9 checked, 1 unchecked | 4/4 sources retired through the verified path (`roll.source_retired` 4→8, `roll.target_retired` unchanged). p3 hit the same harness gap as R1 (first turn: one request, complete) |
| R4 | After the review fixes | D→E | 12/12 | 0 violations, 0 error frames, 0 viewer reconnects, one provider request per message, every queue item complete. `deferred` for 17 s, then `rolling` with at most 2 moving (requested → finishing → transferable → transferred), each verified source retired; `complete` 61 s after targeting with only E resident |

R3 also showed that `roll` describes the roller's last pass. Read right
after `target_execution`, it can still say `complete` for the previous
target. The poller now also requires `roll.target_version` to equal
`target_execution_version`, and the design notes this.

**Tests.** `tests/v3/test_runtime_project_roller.py` (12 cases, SQLite and
PostgreSQL) covers:
- a K-parallel roll and retirement after verification;
- a pinned project skipped without a hold, then retried after its pin
  clears;
- a pin reported after the request: the handoff is withdrawn and the target
  retargeted;
- the held-limit release;
- restarts mid-roll, including one after the prepare receipt was saved
  (the operation-record fallback for a crash before that receipt is not
  exercised);
- deferral to operator operations, ignoring a stale `running` row, and
  withdrawing while deferred;
- a withdraw racing a transfer;
- prepare backoff;
- a previous roll's target retiring only after its next handoff verifies;
- the disabled setting;
- the ledger steps and authenticated status.

`tests/v3/test_admission_retry.py` adds a case: a cold project is admitted
while a handoff target is unassigned. That case fails without the
auto-admission change.

## Not verified

- The browser UI resuming viewer streams across a host swap, and a POST stream
  that ends without a terminal frame (E6), on desktop and mobile.
- Harness, goal, mission, approval, schedule and callback journeys during a
  roll.
- Frozen onefile bundles, the systemd socket units (public and host
  services), `--listen-fd` and `--host-services-fd` under `mercury-run`, and
  the deployer's readback against a socket-activated unit.
- The Electron desktop's own refresh across a swap (E8 exercised its
  registration API with curl).
- More than four projects and a third resident version. Parallel handoffs
  ran only in the roller rehearsal above (K = 2).
- A roller roll with a pinned project, a harness session or a supervisor
  swap on a real Core; these are covered only by the tests above.
