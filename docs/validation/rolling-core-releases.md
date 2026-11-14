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

## Not verified

- The browser UI resuming viewer streams across a host swap, and a POST stream
  that ends without a terminal frame (E6), on desktop and mobile.
- Harness, goal, mission, approval, schedule and callback journeys during a
  roll.
- Frozen onefile bundles, the systemd socket unit, `--listen-fd` under
  `mercury-run`, and the deployer's readback against a socket-activated unit.
- More than four projects, parallel handoffs, and a third resident version.
