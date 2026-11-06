# Recovery candidate scan validation

The periodic recovery tick now selects potential recovery IDs from the existing
status projections before loading turn payloads. Historical interrupted turns
with no pending recovery no longer pay JSON decoding and model validation costs
every 15 seconds. The scheduler interval and its other jobs are unchanged.

Main was fetched and verified as `beeca9a8e9e73c8485beedb15bee6546fed48d8b`
before editing and again before the PR. Relevant recovery/storage code was
unchanged from the last reported live revision, `831d0d0c646a31c184bca7c868034034484c9308`.
Live access was unavailable; the operator explicitly substituted a disposable
instance. No live revision verification or production deployment is claimed.

## Contract and implementation

The journey is an interrupted provider conversation returning to its saved chat
and continuing safely after Core restarts. Core owns turns, receipts, recovery
flags, goals and subagent state; the URL identifies the selected conversation.

| Journey step | Observable invariant | Evidence |
| --- | --- | --- |
| Discovery/create/select | Existing chat entry and identity remain usable; no new controls or settings | Production browser opens the saved conversation |
| Interrupt/restart | The same saved turn resumes once | Real Core startup, retry and subagent cases |
| Receipts/callbacks | Late trusted results are adopted; unknown effects retain their safeguards | Receipt, untrusted-result and callback cases |
| Goals/subagents | Existing goal and subagent checks still govern resumption | Focused lifecycle cases and recovery fencing |
| Refresh/reconnect | Saved answer returns without another provider execution | Authenticated HTTP reload and production browser reload |
| Failure/retry | Required recovery and automatic retries remain discoverable; corruption does not block other candidates | SQLite/PostgreSQL candidate and retry cases |
| Deletion/concurrency | A deleted or changed turn is not resumed from an old ID selection | Deletion, routing, completion and cleared-recovery races |

Two covering-index queries select ID and creation time: interrupted rows whose
visibility projection is true or null, and rows with a missing status projection.
Candidates retain their chronological order and are hydrated in existing
1,000-record pages with the existing unreadable-record handling. Hydrated status,
provider backend and pending recovery are rechecked. A missing projection never
means recovery is finished. Existing receipt, goal, subagent and resume checks
remain in place.

Focused validation found five failures on untouched main. Independent diagnosis
confirmed stale async test doubles, an unjoined cancellation fixture, and a
subagent transaction callback that discarded its receipt update coroutine. The
callback is now awaited inside the same transaction as its message. The browser
journey also exposed existing queue routes calling async get/exists/write methods
without awaiting them. Those route boundaries now await their existing service
contracts; the LAN case checks absent-queue revision/ETag, conditional polling and
saved pause state. These are prerequisite async recovery fixes, not benchmark
shortcuts. No other queue callers were changed.

## Causal evidence and limits

A fresh-context subagent diagnosed the fault before the optimization: the old
scan hydrates every interrupted row before `_turn_is_pending` rejects historical
ones. Its independent PostgreSQL probe decoded and validated all 620 records on
the event-loop thread. A projection-only control read no payloads and took about
12 ms. A flat-text control at the same byte size was much cheaper than structured
history. This rejects database waiting alone as the explanation for the synthetic
stall and shows why row count and byte size alone cannot establish the live cost.
The projection and pending predicates also agreed on 36 recovery flag combinations.

The old PostgreSQL payload query produces `db.execute.select.7ece61422b43`; in the
final structured workload its cursor spans were 458–1,083 ms, followed by more
synchronous validation. Interrupted and waiting-callback scans intentionally
share this compiled shape and hash. The reported live telemetry hash/caller link
remains **unconfirmed**. Cadence is compatible with the scheduler's 15-second wait
plus pass work, but does not uniquely identify a caller.

## Same-workload measurements

[Raw receipt](recovery-candidate-scan.json) contains source hashes, fixture hashes,
all samples, SQL shapes, independent probes, plans and LAN/build evidence.
[The harness](../../scripts/benchmarks/recovery_candidates.py) ran sequential
baseline/candidate processes with the same Python 3.12 interpreter. Each scenario
contains 620 interrupted turns, zero pending recoveries and about 21 MiB of JSON;
the fixed fixture SHA-256 agrees across both revisions and both database engines.
There are five complete `recovery_tick` invocations per scenario, including its
other jobs against empty synthetic work tables, and a 5 ms heartbeat. The
scheduler sleep is excluded. These are small synthetic samples, not live traces.

| Workload | Returned candidates before → after | Full records/pass before → after | Median tick ms before → after | Maximum lag ms before → after |
| --- | --- | --- | --- | --- |
| SQLite, structured history | 0 → 0 | 620 → 0 | 1,542.052 → 17.461 | 2,294.639 → 1.097 |
| PostgreSQL 16, structured history | 0 → 0 | 620 → 0 | 1,331.472 → 30.230 | 2,031.561 → 7.414 |
| SQLite, flat text | 0 → 0 | 620 → 0 | 130.198 → 20.995 | 256.897 → 12.268 |
| PostgreSQL 16, flat text | 0 → 0 | 620 → 0 | 227.008 → 25.455 | 239.873 → 16.000 |

The after-runs still contained a 143.953 ms pass and up to 16 ms timer lag. No
second-scale lag appeared in those after-runs; this does not establish that live
stalls are resolved. Genuine pending work and legacy rows with missing projections
still require hydration. PostgreSQL EXPLAIN after VACUUM showed index-only scans
with zero heap fetches for both candidate queries. SQLite tests assert covering
status-index plans for both queries.

To reproduce, point `PYTHONPATH` at the baseline or candidate `src` tree and run:

```sh
.venv/bin/python scripts/benchmarks/recovery_candidates.py \
  --root /tmp/owned-recovery-fixture --output /tmp/recovery-result.json \
  --label candidate --passes 5
```

For PostgreSQL, add `--url "$NEBULA_TEST_POSTGRES_URL"` using an explicitly owned,
disposable database. The benchmark seeds only synthetic rows. Never use a live DB.

## Acceptance evidence

- Python: 27 collected and 27 passed in 30.12 seconds, including seven PostgreSQL
  cases, with RuntimeWarning and PytestUnraisableExceptionWarning treated as errors.
  Two dependency deprecation warnings remained. Exact selectors and exclusions are
  in [the diff-bound selection](../../.github/test-selection.json).
- Lint: `.venv/bin/ruff check src/nebula/v3 tests/v3 scripts/benchmarks/recovery_candidates.py`
  passed. `npm --prefix ui run build` passed, with existing chunk/import warnings.
- Real Core: isolated SQLite data, inert local provider, authentication enabled,
  HTTPS and normal device pairing. LAN origin was `https://172.26.91.248:39965`.
  Unauthorized transcript access returned 401; startup recovered the original turn,
  saved one assistant message, and repeated reads agreed. The provider ran once.
- Production browser: Chromium 154.0.8037.57, desktop 1440×1000, paired through the
  real controls and reopened the saved answer after reload. The same Core's
  loopback HTTPS origin was `https://127.0.0.1:39965`. The environment proxy rejected
  browser requests to the container LAN IP, so this is not browser-at-LAN evidence.
  HTTP clients trusted the temporary fixture certificate; Chromium pinned its
  exact public key. Core's HTTPS pairing requirement remained enabled.
- Build identity: `beeca9a8e9e73c8485beedb15bee6546fed48d8b`, dirty candidate checkout,
  built `2026-11-06T22:44:50.330Z`; served metadata matched the production bundle.
  Frontend source was unchanged. The raw receipt includes its asset hashes.
- Interface viewport/mobile/WebKit and physical-device gates do not apply to this
  backend-only change; no interface, layout, browser transport or settings changed.
  Hosted Python runs exercise the LAN API case; local optional UI assertions supply
  the production browser evidence. Disposable servers stopped after the journey.

Local acceptance set `NEBULA_TEST_UI_DIST` to the built `ui/dist`,
`NEBULA_TEST_CHROMIUM_EXECUTABLE` to the installed Chromium, and
`NEBULA_TEST_POSTGRES_URL` to the disposable PostgreSQL instance, then ran the JSON
selection with pytest. Unrelated suites were not run.

The slow session-activity endpoint remains a separate investigation:
[issue #942](https://github.com/berylliumsec/nebula/issues/942).
