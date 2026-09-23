# Rust assistant laboratory

This workspace contains **assistant-only development foundations**, not a
replacement for the shipped assistant. Other Nebula areas are out of scope.
See `../docs/ASSISTANT_RUST_MIGRATION.md` for the acceptance contract and gaps.

- `nebula-assistant-domain`: bounded stream-event types and immutable validation
  of all 13 canonical persisted assistant record kinds. Request validation and
  historical default migrations are not implemented.
- `nebula-assistant-storage`: SQLite journal with a bounded single writer,
  append-only records, idempotency, commit notifications and bounded replay.
- `nebula-assistant-runtime`: fair project/parent/session queue selection;
  waits release execution slots while retaining session ownership. No execution.
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
