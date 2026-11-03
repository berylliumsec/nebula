# Harness tool-event history lookup

The November 3, 2026 synthetic reproduction confirms a history-dependent SQL
lookup inside synchronous harness event bookkeeping. Baseline source is
`1951d3397cb12260629c3e65f4d3ebf3b2e4f4c4`. Both builds use the same Python
3.12.14 interpreter, SQLite 3.53.1 and SQLAlchemy 2.0.37. Production was neither
accessed nor restarted.

## Acceptance contract

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Send/use/stream | Native and MCP events reuse the exact turn's approved/running call and settle one row | ToolCall JSON and harness adapter identity | Chat/mission native/MCP regressions and actual completion API |
| Concurrent chats | Settled history does not determine active-call query work | Core storage | Actual SQL plan plus VM instruction regression with 0/4,500 historical calls |
| Refresh/reconnect | Durable events retain ordered sequences, exact call IDs and reconnect cursors | Operation/run ledgers | Authenticated real-Core replay after concurrent turns |
| Interrupt/failure/retry | Existing interruption, transport closure and restart recovery still settle turns | Harness runtime and durable turn state | Selected stopped-turn and approval tests |
| Create/update/replace | All supported writes maintain or clear the turn projection alongside status | Authoritative entity payload | Common storage write-path regressions |
| Upgrade/retry/downgrade | Projection backfill preserves envelopes, revisions and timestamps; failed index creation rolls back | SQLite/Alembic | Migration roundtrip and injected failure/retry regressions |
| Authentication | Ordinary reads require normal Core authentication | Core auth | Invalid synthetic bearer token receives 401 |

Discovery, selection, deletion and revocation retain their existing interfaces and
ownership. The change adds no setting, UI control, browser storage, route,
authentication rule or origin-sensitive behavior. Frontend, browser/device,
production UI build and physical-device checks do not apply to this internal SQL
repair. The actual Core journey runs on a private non-loopback LAN origin with
isolated synthetic storage and an inert adapter; it executes no external tools.

## Causal evidence and repair

The measured path is `_record_tool_event` → `_record_native_tool_event` →
`_turn_tool_calls` → `list_session_entities`. The baseline seeks the conversation
index and evaluates `JSON_EXTRACT(payload, status)` over its historical rows.
Filtering the resulting few active calls by harness turn happens in Python.

A fresh-context diagnostic subagent independently reproduced the mechanism and
validated the cause before implementation. Ten existing-running `tool_started`
events issue ten SELECTs and zero writes. Returned-row JSON decoding remains
four calls per event; with 4,500 historical calls, SQLite cursor execution takes
172.5 ms of 192.3 ms total. Captured SQL alone grows from 0.041 ms to 16.71 ms.
This separates the demonstrated lookup cost from synchronous writes, quota work
and Python model materialization.

The repair projects `metadata.harness_turn_id` for ToolCalls through the common
write helper and migration `0026_harness_tool_call_lookup`. Its index covers kind,
turn, project, status and stable ordering fields. Both chat and mission event
lookups seek the exact turn/project/status subset. Only that result is sorted in
Python by `(created_at, id)`: an SQL order over several statuses otherwise causes
SQLite to choose a broad ordered project index. The regression checks the actual
plan and VM work through complete result consumption. Empty status lists return
without querying. No in-memory activity cache is introduced.

The migration performs a one-time backfill/index build. It preserves saved JSON,
revisions and timestamps, projects only string IDs, and leaves non-tool entities
unprojected. Normal startup applies it; a production rollout still needs the
usual verified backup and coordinated Core deployment. This PR does not deploy.

## Reproduction

Run the candidate driver against both source trees using the same interpreter.
Substitute the executor's private LAN address. Each invocation creates, stops and
removes its own temporary Core and data, retaining only JSON evidence and logs.

```sh
git worktree add --detach /tmp/nebula-harness-baseline 1951d3397cb12260629c3e65f4d3ebf3b2e4f4c4
PYTHONPATH=/tmp/nebula-harness-baseline/src .venv/bin/python scripts/benchmarks/harness_tool_events.py --host 172.26.91.252 --label baseline --output /tmp/harness-lan-before-final.json
PYTHONPATH=/workspace/nebula/src .venv/bin/python scripts/benchmarks/harness_tool_events.py --host 172.26.91.252 --label candidate --output /tmp/harness-lan-after-final.json
```

The driver seeds three conversations with 4,500 completed 4-KiB results each,
submits three concurrent `/api/v1/chat/completions` requests through an inert
native-tool adapter, and processes 50 start/completion pairs per chat. It measures
300 actual bookkeeping operations, polls the authenticated assistant-defaults
route concurrently, and monitors a 10-ms Core timer. It verifies exactly one
ledger identity per tool, ordered durable replay, and replay after the last tool
cursor. Normal Core lifespan and authentication remain enabled. Baseline origin:
`http://172.26.91.252:57245`; candidate: `http://172.26.91.252:50807`. Both processes
exit successfully and both temporary data directories are removed.

## Results

Full SQL, query plans, source hashes and measurements are committed in
[harness-tool-event-results.json](harness-tool-event-results.json).

| Measurement | Baseline | Candidate |
| --- | ---: | ---: |
| Lookup-only median, 4 active / 4,500 historical calls | 18.49 ms | 0.70 ms |
| Lookup-only 200-event burst | 3,705.61 ms | 195.66 ms |
| LAN bookkeeping median, including writes | 25.23 ms | 5.11 ms |
| LAN ordinary HTTP median | 101.54 ms | 27.46 ms |
| LAN ordinary HTTP p95 | 183.41 ms | 77.45 ms |
| LAN turn duration median | 9,437.31 ms | 3,492.11 ms |
| LAN loop lag median | 46.86 ms | 7.71 ms |
| LAN loop lag p95 | 90.98 ms | 19.26 ms |

The lookup-only baseline with no history is 0.75 ms; the candidate with no history
is 0.53 ms. The independent baseline rerun measures 0.71 ms without history and
19.54 ms with history. These are synthetic measurements, not production latency
guarantees. The LAN test did **not** reproduce the reported 5.1-second ordinary
HTTP response or prove the origin of earlier 6–8-second timeouts. Synchronous
event writes, artifact persistence and quota/rate observers remain separate costs.

## Focused verification

`.github/test-selection.json` selects 52 collected Python cases: the new lookup
and migration files, `test_entity_hot_lookups.py`, three exact existing harness
stream/approval cases, and `test_harness_stopped_turns.py`. All 52 selected cases
pass locally: the initial run passed 51; the corrected metadata expectation and
strengthened reserved-call projection assertion pass on focused rerun, and the
four bounded-work cases pass again with the progress hook active through fetching.
The receipt records the exact selectors. No full suite was run.

`ruff check src/nebula/v3 tests/v3 scripts/benchmarks/harness_tool_events.py` passes.
`mypy src/nebula/v3` passes for 231 source files. Migration tests exercise SQLite;
a live PostgreSQL server is unavailable, so no PostgreSQL runtime claim is made.
CI must validate the committed selection and selected checks before merge.
