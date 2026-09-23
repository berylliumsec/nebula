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

Pending: assistant request validation and historical migration coverage,
authenticated API/stream integration,
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

## Canonical record port and Figma review (continuation)

`nebula-assistant-domain::records` now implements immutable validation for every
captured assistant entity kind. It preserves metadata, large JSON integers,
lineage, archived/retracted history, timing, read cursors, bookmarks, claims and
delivery state. Schema checks are compiled once; cross-field checks are explicit
Rust code. This does not execute, resume, or authorize an agent.

The oracle generator constructs records using the existing Python models and
asserts that their schemas still match the captured baseline. Its 147 cases
contain 52 accepted canonical records and 95 rejected mutations. Six selected
Rust checks cover that corpus, retracted-message interpretation, missing identity
fields, privacy-preserving errors, byte limits and concurrent readers. The
Python selection also regenerates the corpus to detect oracle drift.

The contract is canonical `model_dump(mode="json")` output, not arbitrary API
inputs. The subsequent SQLite port adds deterministic historical field defaults;
missing identities and timestamps still fail closed. All 85
inventoried assistant routes remain unported; the records module has no HTTP
listener and is not wired into shipped Core. There is still no Rust assistant
production journey or whole-assistant performance evidence.

The requested [Figma journey review](ASSISTANT_RUST_FIGMA_REVIEW.md) inspected live
Assistant designs, including desktop/mobile exports, and records superseded
recovery and concurrency designs. The committed review receipt explicitly marks
production workflow, prototype clickthrough, and Rust journey parity unverified.

Continuation validation: all six selected record tests passed, including all 147
oracle cases; all four selected Python contract tests passed. Workspace Clippy
with warnings denied and formatting passed. The cumulative CI selection is 30
exact Rust tests plus 41 Python tests. The earlier journal measurement belongs to
its recorded binary hash; no performance claim was made for this continuation.

## SQLite persistence port contract

The persistence layer targets the existing `entities` envelope at application
schema 5 / Alembic `0016_chat_session_lookup`. Python-created isolated databases
are the compatibility oracle. No live state is used or activated during this work.

Journey: create a conversation, retain its transcript, change its metadata, list
and select it again, and reopen its data after switching implementations. Entity
payloads/revisions are authoritative; `chat_session_id` and `search_documents`
are transactional projections. Acknowledgment follows commit. A stale revision
rejects the entire batch. Admission bounds both queued entries and queued bytes;
read pages have a size bound and explicit continuation rather than dropping rows.
Provider dispatch, hooks, commands, and other product entities are not executed
by this storage layer. Historical optional fields may receive only deterministic
schema defaults; missing identities/timestamps must not be invented on read.

The fixture captures Python-produced DDL, thirteen entity envelopes and thirteen
legacy-default cases. Nine exact storage cases exercise atomic rollback,
simultaneous revision conflicts, session lookup past 1,000 unrelated rows,
byte/queue bounds, cancelled callers, draining shutdown, process exit after commit,
schema refusal and retained search projections. A domain regression compares all
thirteen defaulted records with Python, including float normalization; opaque
metadata and large integers stay unchanged.

The separate Python interoperability test bootstraps a real database, closes it,
runs Rust mutations, then reopens it in Python with bootstrap disabled. Python
reads the changed records and search projection, checks schema markers and an
unrelated row, performs another update/delete, and closes before Rust verifies
the result. No two implementations run against the database simultaneously.

This is current-schema storage interoperability, not historical schema migration,
PostgreSQL support, safe execution recovery or mission-checkpoint compatibility.
SQLite uses the existing WAL/NORMAL durability setting. Its writer lock excludes
other Rust stores but cannot exclude an unmodified Python Core. HTTP, production
browser/LAN and actual execution acceptance remain required for the full rewrite.

## Saved context and read-cursor port contract

Journeys: select a conversation, save selected text as an explicit decision,
edit/supersede/remove it, promote it to project context, refresh from another
device, and acknowledge catch-up through a recorded timestamp. The database owns
record identity, revisions, retained history, project membership, and device read
watermarks. Reading or acknowledging activity must not resolve an approval or
execute a turn. The authenticated device identity overrides a supplied device ID.

The service layer will preserve the existing decision/read-cursor data shapes and
cross-conversation checks, keep promotion atomic, reject stale edits and backwards
or future read cursors, and retain exact source-selection validation. Bounded
reads must fail explicitly rather than silently drop saved context. Transaction
preconditions will recheck records used to authorize/validate a mutation.

The service port includes save/edit/supersede/remove/promote and active-context
snapshots, plus cursor lookup and monotonic acknowledgment. The source/session
preconditions are checked under `BEGIN IMMEDIATE`; stale reads cannot commit a
new decision after the referenced source was edited or deleted. Promotion retains
the local revision history and creates its project copy in one transaction.

Evidence: 27 deterministic Python service-oracle transitions; seven exact Rust
service tests, including history retention, promotion collision rollback,
concurrent revisions, a queued source-change race, 10,001 retained context rows,
Unicode limits, paired-device identity and database reopen. These and the nine
affected storage tests passed locally, along with 23 selected Python oracle and
test-selection checks. The cumulative CI selection is 47 exact Rust / 44 Python
tests. The fixture compares complete success payloads except labeled server
creation/update timestamps, and error statuses rather than full HTTP envelopes.

HTTP/authentication middleware, context fork orchestration and prompt assembly,
catch-up pending projections, production browser/mobile/LAN journeys and final
packaging remain required integration gates. No shipped routes are activated.
This step does not run providers or tools.
