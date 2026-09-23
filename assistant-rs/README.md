# Rust assistant laboratory

This workspace contains **assistant-only development foundations**, not a
replacement for the shipped assistant. Other Nebula areas are out of scope.
See `../docs/ASSISTANT_RUST_MIGRATION.md` for the acceptance contract and gaps.

- `nebula-assistant-domain`: bounded stream-event types and immutable validation
  of all 13 canonical persisted assistant record kinds, plus deterministic
  historical field defaults. General request coercion is not implemented.
- `nebula-assistant-storage`: SQLite journal with a bounded single writer,
  append-only records, idempotency, commit notifications and bounded replay.
  Its separate `entities` module reads and updates an isolated copy of Nebula's
  current SQLite schema, preserving revisions, lookup and search projections.
- `nebula-assistant-runtime`: fair project/parent/session queue selection;
  waits release execution slots while retaining session ownership. No execution.
- `nebula-assistant-services`: saved-context mutations, active decision snapshots,
  revision history, atomic project promotion, and per-device read cursors. Source
  and session revisions are rechecked inside the bounded writer transaction.
- `nebula-assistant-transport`: experimental Axum routes for saved-context GET/PUT
  and read-cursor PUT, with bearer/paired-device authentication. No shipped entry
  point mounts this router yet; full request/error and production parity are open.
- `nebula-assistant-lab`: fixture generation, replay and journal measurements.

The lab refuses existing output directories and refuses to open databases without
its private application/schema markers. It has no server, provider, harness, or
command-execution entry point. No production database should be supplied to it.

From this directory:

```sh
cargo build --locked --release -p nebula-assistant-lab
./target/release/nebula-assistant-lab measure --directory /tmp/assistant-measure-unique --streams 100 --events-per-stream 20
./target/release/nebula-assistant-lab fixture --directory /tmp/assistant-fixture-unique --turns 10000 --events-per-turn 100
```

The million-event generator specifies deterministic event content/layout; UUIDs
and timestamps differ per generation. Measurements describe the journal only.
They do not prove 100 real-agent throughput, UI latency, or a Python/Rust speedup.
Use the diff-bound test receipt and `scripts/run_assistant_rust_tests.py` to run
selected exact tests; do not run an unscoped workspace test suite.

Journal admission is acknowledged only after commit. Capacity rejection means
nothing was admitted. If a caller disconnects after admission, the write may still
commit: retry with the same idempotency key to resolve the result. SQLite WAL uses
`synchronous=NORMAL`, matching the current local Core setting: process-crash
recovery is supported, but power-loss durability is not upgraded to `FULL`.

The fair queue is an in-memory policy, not durable admission. Pending/parked work
blocks new admission at 2,048; 128 already-running turns have reserved parking
capacity, so at most 2,176 entries can remain tracked. Removing cancelled work
eagerly clears queue indices. Work identifiers and grouping fields are bounded.

`records::StoredAssistantRecord::decode` preserves opaque JSON metadata and large
integers, checks the captured storage schemas, and enforces the Python model's
cross-field invariants. Validators are compiled once and shared across readers;
schema resolution has no network/file-fetch features. It rejects missing fields
instead of creating identities or timestamps while reading. Its 16 MiB input
limit is a development bound, not a claim that larger historical records have
been migrated. Errors contain no transcript/metadata values.

Reproduce the 147 Python-oracle record cases without starting Core:

```sh
PYTHONPATH=src python scripts/capture_assistant_records.py --output assistant-rs/compatibility/python-records.json
```

Run that command from the repository root using Nebula's Python environment.
See `../docs/ASSISTANT_RUST_FIGMA_REVIEW.md` for the live design review and the
remaining operator journey gates. Design inspection is not product acceptance.

The entity store requires application schema 5 and Alembic
`0016_chat_session_lookup`; it refuses missing, older or future databases without
modifying them. It does not bootstrap/migrate schema or implement session-level
authorization/deletion policy. A file lock excludes other Rust stores only:
the caller must stop Python before any future cutover. The shipped application
does not launch or call this layer.

Entity transactions have at most 64 mutations and 16 MiB each of request and
returned records. Defaults bound queued plus active request bytes to 16 MiB,
queued requests to 128, read admission to 128 and read connections to four.
Read pages return at most 1,000 records, normally at most 4 MiB; a first oversized
record may return alone up to the 16 MiB per-record bound. Continuation offsets
follow legacy ordering and do not claim a stable snapshot across multiple pages.
Capacity errors mean the mutation was not queued. Cancelled callers must inspect
record identities/revisions to resolve an uncertain result; accepted requests
drain on shutdown and acknowledgments follow commit.

`tests/test_assistant_storage_interop.py` creates a real isolated Python database,
writes in Rust, reopens in Python without bootstrap/repair, writes again in Python,
then verifies in Rust. The companion example refuses ordinary databases lacking
the test's dedicated marker. Regenerate its schema/default oracle from the root:

```sh
PYTHONPATH=src python -m scripts.capture_assistant_storage --output assistant-rs/compatibility/python-storage.json
```

Saved context is read in one SQLite statement, retaining a consistent projection
during concurrent promotion. Complete collections are capped at 10,000 records
and 16 MiB and fail explicitly above either limit. The existing active-context
limit remains 100 entries / 40,000 Unicode characters. This is separate from
queue admission; oversized history is not reported as a transient queue failure.

The 27-transition `python-context.json` fixture covers the existing Python
decision/cursor service behavior. It compares complete success payloads with
server creation/update timestamps labeled explicitly, and compares error statuses.
Timestamp/history retention and concurrent writes also have dedicated Rust tests.
This service fixture does not establish HTTP error-envelope parity, fork
orchestration, prompt assembly, catch-up projection or production UI parity.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_context --output assistant-rs/compatibility/python-context.json
```

The separate `python-auth.json` fixture captures 33 requests through Python's real
authentication middleware, with identical canonical paired-device records on both
sides. Rust compares status, bearer challenge, normalized error/success bodies and
device state. Device lookup filters by token hash in SQL, without the legacy
1,000-row list limit. Activity refresh rechecks revocation/expiry and revision in
the single writer transaction; it cannot restore a revoked device. Credentials
are omitted from device debug output and storage-error responses.

The host supplies a trusted HTTP/HTTPS scheme; forwarded headers do not change it.
By default, request bodies are capped at 1 MiB and handlers at 30 seconds. Admitted
responses reserve 16 MiB each from a 64 MiB budget, so at most four buffered
responses can be retained under the default configuration even though the request
limit is 128. Permits remain held until the body is consumed or dropped. These are
conservative development bounds, not measured throughput settings. Connection
limits, streaming protocols, full Pydantic coercion/error lists, CORS, diagnostic
persistence and shipped lifecycle integration remain pending. The library does
not start a listener automatically; its TCP test binds only an isolated loopback
port and uses disposable data.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_auth --output assistant-rs/compatibility/python-auth.json
```
