# Assistant Rust migration

Status: **incomplete; assistant development foundations**. This is not a Rust
replacement for the assistant yet. The existing application and all other product
areas retain their current implementation. The development executable is named
`nebula-assistant-lab`, never `nebula-core`.

The user narrowed the rewrite to the assistant: conversations, streaming,
provider/harness integration, follow-ups, subagents, and restart recovery.
Whole-Core migration, missions, browser research, reporting, and other workbench
areas are excluded. Shared changes are limited to focused build/test wiring.

Baseline: `b5bab4372871a0fa3fe91968c95d73903796ab56`, verified against origin/main.
Branch: `codex/rust-core-rewrite`.

## Operator acceptance contract

| Journey | Invariant | State authority | Required evidence |
| --- | --- | --- | --- |
| Discover/create/select | Conversations and runtime selections remain usable | Existing Core records + URL | Differential API + production UI |
| Stream/interrupt | Ordered output; interruption preserves saved content | Turn, transcript and event records | Rust integration + real Core |
| Background/reconnect/refresh | Replay never repeats execution | Durable records and cursor | Replay + real-Core browser |
| Fork/retry | Lineage remains intact; unknown effects are not replayed | Claims and receipts | Fault injection + real Core |
| Subagents/waits | Waiting parents cannot exhaust child capacity | Durable ownership and admission | Fairness + lifecycle tests |
| Delete/revoke | Stale selection clears; authorization is enforced | Core database | Auth + browser |
| Upgrade/rollback | Existing assistant records remain readable | Schema versions and snapshots | Isolated migration rehearsals |

All product journeys remain required and unverified for Rust. Browser/device/LAN
matrices from the product-quality skill remain mandatory before activating the
replacement. This foundation changes no assistant route, UI, provider, or harness
execution. No changes to other product areas are needed for its use.

## Development boundaries

`assistant-rs` isolates domain contracts, storage, scheduling, and a fixture-only
laboratory. Accepted work must eventually be durable before acknowledgment;
bounded in-memory queue selection by itself does not establish that guarantee.
The fair queue is not an execution engine and never dispatches tools or agents.

The laboratory journal owns a new fixture database, not Nebula's database. It uses
bounded single-writer admission, transactional sequence allocation and idempotency,
pooled reads, and commit notifications. Replay is authoritative from SQLite; a
slow observer cannot block the writer or silently lose durable events. This is
not yet migration compatibility with legacy provider streams (which are currently
buffered in memory). Old application state must never be inferred from lab events.

`scripts/capture_assistant_contract.py` inventories mounted assistant routes and
their referenced schemas without starting Core's lifespan or invoking endpoints.
It uses temporary storage. An inventory is not evidence that routes were ported.

## Remaining work and release gates

Pending: assistant entity/validation parity, authenticated API/stream integration,
provider and harness adapters, persistent admission and ownership, goals,
follow-ups, collaboration, safe recovery, historical migrations, PostgreSQL,
helper RPC, binary packaging and production UI acceptance. Python remains the
assistant authority. There is no runtime feature flag or hidden Rust fallback.

The requested performance gates remain whole-assistant targets, not measured
results: 100 active agents on 8 cores / 16 GiB; 2,000 events/s; p99 read <=100 ms,
mutation <=200 ms, delivery <=100 ms; >=2x baseline throughput; <=60% baseline CPU
per operation; <=1 GiB Core RSS. Require matched Python/Rust workloads, three
repetitions, a 60-minute soak and bounded overload on isolated instances. A
synthetic journal measurement does not establish these product targets.

The PR stays draft while required assistant gates remain incomplete. No merge,
publication, live deployment, or live database access is included.

## Foundation validation (2026-09-23)

- 24 exact Rust integration tests passed: 13 journal cases, 8 scheduling cases,
  and 3 executable CLI cases. These cover committed-event crash recovery,
  idempotency conflicts, cancelled callers, bounded admission, per-turn replay,
  100 concurrent viewers, subscription cleanup, fairness and waiting parents.
- 40 selected Python checks passed across `test_assistant_contract.py`,
  `test_assistant_rust_selection.py`, and the existing test-selection policy file.
- Rust 1.94.0 compilation, Clippy with warnings denied, formatting, and the
  throughput-oriented release build passed.
- The captured Python assistant inventory contains 85 mounted route entries and
  13 assistant entity schemas, including generated conversation CRUD routes.
- A short release-build journal measurement committed/replayed 2,000 events
  across 100 synthetic streams: 4,975 events/s, 41.811 ms p99 append latency,
  3.452 ms p99 replay-batch latency, and 9,184 KiB peak RSS. There were no capacity
  rejections in that sample. The raw receipt and binary SHA-256 are committed in
  `assistant-rs/compatibility/journal-measurement.json`.

This sample ran on the shared development host, without the agreed CPU/memory
isolation or a Python comparator. It is not a sustained-load, provider, API,
event-delivery, or whole-assistant result. No production UI, LAN, mobile browser,
physical device, PostgreSQL, full-scale fixture, or 60-minute soak was tested.
