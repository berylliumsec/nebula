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
  revision history, atomic project promotion, per-device read cursors, transcript
  navigation, project message search, durable bookmarks, conversation catalogs,
  catch-up and retained Results/context-source reads. Source
  and session revisions are rechecked inside the bounded writer transaction.
- `nebula-assistant-transport`: experimental Axum routes for saved-context GET/PUT
  and read-cursor PUT, transcript/search GET, bookmark GET/PUT, conversation
  catalogs, catch-up/summary and Results/context-source GET, with
  bearer/paired-device authentication. No shipped entry
  point mounts this router yet. Typed request coercion and error envelopes have
  Python-oracle coverage; complete route and production parity are open.
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
limits, streaming protocols, malformed JSON/content-type/method edge cases, CORS,
diagnostic persistence and shipped lifecycle integration remain pending. The library does
not start a listener automatically; its TCP test binds only an isolated loopback
port and uses disposable data.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_auth --output assistant-rs/compatibility/python-auth.json
```

`python-http.json` captures 120 real Python API requests and the 23 final saved
records. Rust matches normalized response bodies, error codes/features, field
locations, operation identities, defaults, ignored extra fields, Unicode bounds,
revision/datetime coercion and duplicate/stale-write messages. Expected revisions
retain arbitrary precision until compared with stored revisions. Timestamp
parsing uses pinned [Speedate](https://docs.rs/speedate/0.17.0/speedate/), with
explicit rejection of year zero to preserve Python readability. The shared
diagnostic catalog is compiled into Rust; no Python runtime is invoked.

Error and success serialization both stop at the 16 MiB response bound. A focused
regression supplies a large invalid body that would amplify into multiple field
errors, verifies a bounded 413 response, then successfully submits a corrected
request. The corpus uses a documented repeat-string encoding for large boundary
values, expanded only in tests. This is finite compatibility evidence, not proof
of every malformed-input or production UI behavior.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_http --output assistant-rs/compatibility/python-http.json
```

`python-navigation.json` captures 112 transcript, project-search and bookmark
requests against disposable Python Core state, including literal wildcard queries,
Unicode excerpts, replaced-message paging, temporary sessions, inactive bookmarks,
duplicate submissions and stale revisions. The Rust HTTP comparison reopens the
database before comparing all 30 final records. Dedicated service tests retain
raw bookmark timestamps and check competing and queued writes. Full case folding
uses a generated Unicode 15.0 table from CPython 3.12; runtime is entirely Rust.
The services crate retains the [Unicode data notice](crates/services/LICENSE-UNICODE.txt)
and declares its combined code/data licenses in Cargo metadata.

Complete transcripts and bookmark collections use the same 10,000-row / 16 MiB
explicit bounds as saved context. Search reads at most 101 stored candidates and
fails oversized pages rather than changing the cursor or truncating history.
Query strings are bounded at 64 KiB; offsets beyond SQLite's representable range
receive an actionable 422. These resource bounds extend Python's behavior.
Reader prefetch is one row per connection, so SQLx cannot queue its default 50
large payloads ahead of the collection byte checks. Invalid missing identities
retain named 404 responses rather than becoming retryable storage failures.
Shared project lookup currently checks identity/kind only; full Engagement schema
validation remains outside this port. Search keeps the legacy query costs,
including correlated bookmark lookups and sorting; no speedup is claimed.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_navigation --output assistant-rs/compatibility/python-navigation.json
```

Generated conversation/message catalogs return a complete requested page or an
explicit size error; a short byte-truncated array would incorrectly stop the UI's
pagination. Catch-up retains pending actions independently of the device cursor.
Its read-only snapshot includes dependency records, source messages and predecessors
under one aggregate 10,000-row / 16-MiB budget. Base entity timestamps normalize to
UTC during hydration; optional cursor timestamps preserve legacy offset semantics.

Results/context-source reads preserve stored-message offsets before role/retraction
filtering. They inspect immutable ToolCall/Artifact dependencies and retain output
ordering, null fields, policy text and opaque context metadata. Retained project
filters are session values, not newly validated project identities. Missing diff
files leave other results visible. The captured oracles use isolated state only.

`HttpConfig.artifacts` optionally supplies a read-only `ArtifactPreview`. The host
configures an existing root, concurrency and deadline; requests cannot select it.
Previews use directory-relative Unix handles, reject symlinks/non-regular files,
and read at most 8,192 bytes per diff after the database snapshot commits. A timed
out or cancelled caller cannot release a blocking reader's admission prematurely.
Symlink rejection and resource limits are explicit extensions to legacy behavior;
previewing does not verify the entire blob hash. Non-Unix construction fails closed.
The current installer release target remains Linux x86_64; packaging and production
acceptance of this library remain incomplete.

Regenerate the retained-read oracles from the repository root:

```sh
PYTHONPATH=src python -m scripts.capture_assistant_catalog --output assistant-rs/compatibility/python-catalog.json
PYTHONPATH=src python -m scripts.capture_assistant_catchup --output assistant-rs/compatibility/python-catchup.json
PYTHONPATH=src python -m scripts.capture_assistant_results --output assistant-rs/compatibility/python-results.json
```
