# Rust assistant laboratory

This workspace contains **assistant-only development foundations**, not a
replacement for the shipped assistant. Other Nebula areas are out of scope.
See `../docs/ASSISTANT_RUST_MIGRATION.md` for the acceptance contract and gaps.

- `nebula-assistant-domain`: bounded stream-event types and immutable validation
  of all 13 canonical persisted assistant record kinds, plus deterministic
  historical field defaults. General request coercion remains incomplete.
- `nebula-assistant-storage`: SQLite journal with a bounded single writer,
  append-only records, idempotency, commit notifications and bounded replay.
  Its separate `entities` module reads and updates an isolated copy of Nebula's
  current SQLite schema, preserving revisions, lookup and search projections.
- `nebula-assistant-runtime`: fair project/parent/session queue selection;
  waits release execution slots while retaining session ownership. Its admission
  supervisor reserves the same queue before persistence and owns accepted store
  work through caller disconnect/shutdown. Provider dispatch is not connected.
- `nebula-assistant-integrations`: bounded OpenAI-compatible HTTP and SSE,
  pooled connections, explicit retry boundaries and cancellation. Parsed tools
  remain inert. See its README for supported protocol shapes and remaining gaps.
- `nebula-assistant-services`: saved-context mutations, active decision snapshots,
  revision history, atomic project promotion, per-device read cursors, transcript
  navigation, project message search, durable bookmarks, conversation catalogs,
  catch-up, retained Results/context-source reads, activity, saved queues, hook
  summaries, goals, child goals, schedules, retained subagent views and durable
  session display revisions. Source and session revisions are rechecked inside
  the bounded writer transaction.
- `nebula-assistant-transport`: experimental Axum routes for saved-context GET/PUT
  and read-cursor PUT, transcript/search GET, bookmark GET/PUT, conversation
  catalogs, catch-up/summary, Results/context-source, activity, saved queue,
  hook-summary, goal/children, schedule, subagent-view and session-state GET, with
  bearer/paired-device authentication. No shipped entry point mounts this router
  yet. Typed request coercion and error envelopes have
  Python-oracle coverage; complete route and production parity are open.
- `nebula-assistant-lab`: fixture generation, replay and journal measurements.

The provider ledger engine borrows the existing SQLite writer transaction. It
binds immutable stream/attempt identities, watched record epochs, typed settlement
receipts and exact replay bytes; returned handles are staged until the caller
commits. Receipt/event idempotency and logical storage reservations survive retry.
Ordinary deltas cannot spend the terminal reserve. Definitive terminal settlement
allows an explicit, idempotent release of unused reservation; uncertain recovery
retains it. These counters measure logical retained bytes, not SQLite pages or RSS.
The engine does not yet compose the actual execution mutations, authorize dispatch,
bound aggregate returned buffers or reconcile physical locators after maintenance.
Its preceding experimental schema is refused rather than silently migrated.

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

Provider execution components now include full completion-request and Turn model
hydration, fenced SQLite admission/claim/answer/completion/release commands,
read-only recovery classification, and lifetime-accounted result records. These
admission commands are now connected to queue reservations through a bounded
supervisor. Replay receipts, complete preparation, provider workers and HTTP
completion routes remain separate integration work. See
`../docs/ASSISTANT_RUST_EXECUTION.md`; a successful component test does not
establish a running Rust conversation.

`FairQueue::reserve/commit/abort` now separates pending capacity from dispatchable
work, preserving Session order even when admission commits arrive out of order.
`provider_ledger::install/inspect` defines an explicit additive SQLite extension;
normal store opening never installs it. `provider_stream` retains bounded JSON/SSE
bytes for future committed replay. These APIs do not create execution authority.

`execution_context` ports pure text history merging, stored-context reconstruction,
context limits and token estimates. Its 99-vector oracle explicitly targets
CPython 3.12 / Unicode 15.0.0, including numeric descriptors outside Unicode 14.
Inputs must already be hydrated and authorized; image/tool projections, compaction,
privacy resolution and the preparation orchestrator remain separate work.

Passive ScopePolicy reads now preserve retained validation, ownership and
local-only refusals, including historical grant timestamps. They do not authorize
tools or create grants. Preparation still must resolve the referenced policy in
the source's order and use the normalized provider locality.

The project-instruction reader rereads a trusted absolute workspace each turn.
It preserves source trimming, 64-KiB raw-prefix hashes, truncation and prompt bytes,
including permitted ancestor AGENTS.md links. Unix directory-relative opens refuse
substituted symlink components. Other platforms require their verified reader
implementation. Defaults allow eight blocking reads and 16 MiB of retained-result
credits; timed-out OS work keeps its slot until it exits. These are bounded logical
ownership limits, not measured peak allocation/RSS or a cancellable-kernel promise.
The workspace resolver and full preparation path are still required.

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

Activity, saved queue and retained turn-hook GETs do not reconcile effects or dispatch
work. Absent queues remain response-only revision-zero values. Hook summaries
retain recorded outcomes while omitting raw process output. Schema-corrupt hook
records currently return a sanitized failure rather than the exact legacy 422
validation detail; complete malformed-record error parity remains open.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_status --output assistant-rs/compatibility/python-status.json
```

Goal responses add observed active time without changing saved elapsed time,
usage, claims or revisions. Child-goal and raw catalog reads retain stored elapsed
values. Goal ambiguity remains a conflict; duplicate schedules return the oldest
after validating every candidate. Schedule run timestamps hydrate to UTC without
rewriting storage. Generated goal, usage-charge, schedule and subagent catalogs
retain their API error namespace and return complete bounded pages. These routes
do not start, pause, resume or execute goals, schedules or children.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_plans --output assistant-rs/compatibility/python-plans.json
```

Subagent views use one complete snapshot with batched conditional dependency
reads. They retain restart-recovery display, approval and question details, saved
versus live usage, source ordering and model fallbacks. Raw turn JSON preserves
insertion order where Python renders opaque history values as text. Rendering
matches Python 3.12 / Unicode 15 printability and retains arbitrary-size integer
counters. Raw history copies, repeated dependencies and expanded responses count
toward explicit limits. Views return `Cache-Control: no-store`; reads never deliver
messages, settle receipts, or start/stop children.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_subagents --output assistant-rs/compatibility/python-subagents.json
```

The experimental session-state GET combines retained turns, pending approvals and
questions, interruption capability and the first qualifying immutable ledger
progress event. A trusted, synchronous observer supplies existing harness transport
liveness; it must never create a transport or perform I/O. Absent observation is
`unknown`. The projection cannot dispatch work, reconcile receipts or change entities.
Shared authentication preserves its existing paired-device idle refresh.

Changed display state writes only the existing `session_projections` row through
the bounded writer. It rereads all inputs after acquiring `BEGIN IMMEDIATE`, then
atomically assigns the revision. An unchanged digest stays on pooled readers.
The digest uses Python's sorted compact ASCII JSON, allowing existing Python
watermarks to survive switching implementations on isolated copies. Input records
and lookup references share the 10,000-row / 16-MiB budget; expanded JSON and its
ASCII hash stream each have a 16-MiB bound. Overflow or invalid retained watermarks
fail explicitly. These bounds are compatibility extensions.

Regenerate the inert session-state fixture from the repository root:

```sh
PYTHONPATH=src python -m scripts.capture_assistant_state --output assistant-rs/compatibility/python-state.json
```

The fixture captures 81 HTTP cases, a writer-phase fault, and 85 immutable
harness-profile validation vectors. Scripted changes model expiry, decisions, first progress, connection
observations, deletion and reopen. No lifespan, adapter, provider, process or tool
execution runs during capture. This remains an experimental API port; production
UI and transport lifecycle acceptance are separate outstanding gates.


Pending-turn and session-hook reads may adopt already-recorded terminal tool
receipts and successful hook outcomes. They preserve unresolved evidence and never
execute or resume work. Strict receipt validation, provider invocation identity,
terminal-status agreement and absence of background results are required for tool
adoption. A hook requires a retained complete zero-exit outcome. Tools and hooks
remain two ordered commits, each with at most three optimistic retries; late hook
changes and their Turn repair commit atomically. Concurrent polls retain counters,
history and receipt identities without duplication.

These snapshots batch ordered references under the shared 10,000-row / 16-MiB
budget. Queued writes retain byte admission through completion or cancellation;
trusted clocks are sampled after the writer lock. Unchanged opaque callback
fragments keep their original dictionary ordering when another effect is adopted.
Provider-result text follows Python's sorted UTF-8 JSON and 8-KiB bounded-result
fallback. Arbitrary-precision validation handles retained large integer counters.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_recovery --output assistant-rs/compatibility/python-recovery.json
```

The isolated fixture contains 194 HTTP cases, 132 receipt validation vectors,
13 serialization vectors and 123 phase captures. Python capture checks raw
entities, event ledgers, relations, display watermarks and search projections.
The Rust HTTP harness checks every entity envelope, exact changed records,
canonical full-state hashes, search/watermark purity and reopen. Production
reconnect, actual recovery dispatch and the browser/device matrix remain open.


Conversation PATCH and schedule create/configure actions preserve saved choices,
null-versus-omitted fields, pending-effect repair before refusal and separate
archive/schedule commits. Immutable MCP configuration is validated without
resolving secrets or launching processes. Dedicated bounded writes retain original
opaque metadata ordering, compare fresh revisions before hydration, sample clocks
inside the writer, and update search documents atomically. Enabling a schedule
makes its configuration durable; schedule dispatch remains unimplemented.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_settings --output assistant-rs/compatibility/python-settings.json
```

The settings fixture records 173 HTTP cases, 74 MCP hydration vectors and 57
request vectors, with exact entity/search changes and protected ledger, relation
and watermark tables. Direct Schedule hydration now returns structured model
diagnostics, including the three previously excluded historical schedule cases.
Wrapped model reads retain sanitized storage errors. Invalid merged schedules
roll back their own writes without undoing an earlier archive/search commit.
Historical missing identity/revision timestamps and noncanonical raw MCP datetime
inputs also remain outside retained-profile parity. Production Assistant settings,
archive and recurring-work journeys require the remaining integration gates.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_model_validation --output assistant-rs/compatibility/python-model-validation.json
```

The ordered Entity/Schedule contract captures 145 validation vectors and eight
separate factory-default observations. Reports share original inputs, redact
Debug/Display, and bound both retained data and repeated-input wire expansion.
The HTTP serializer adds the error envelope within its existing response budget.
This detailed diagnostic implementation covers Entity, Schedule, Goal and token usage;
other retained model contracts remain separate migration work. Trusted writer
datetime provenance and top-level input ordering are preserved. Goal/usage
diagnostics retain nested dictionary order through a bounded streaming capture
and indexed path lookup. Other models' nested display ordering remains a limitation.

Existing-conversation goal POST/PATCH save and replace goal configuration without
starting execution. Creation preserves the provider-backend guard, ordered
candidate validation and lazy identity/two-clock allocation. Editing preserves
revision/terminal/budget refusal order, exact integer comparisons, active elapsed
precision, execution claims and opaque saved data. Only the seven configuration
fields may change through this writer; a fresh revision and writer-clock failure
roll back atomically. Concurrent create retains Python's separate absence check
and insertion rather than promising singleton admission.

The complete finite Goal/ChatTokenUsage model contract preserves typed string
trimming, nested diagnostics, datetime offsets and validator precedence. Wrapped
reads accept the same coercions while retaining sanitized errors. Positive
infinity in historical elapsed values remains an explicit JSON representation
boundary, not a claimed parity case. Goal lifecycle actions and actual execution remain separate integration work.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_goal_drafts --output assistant-rs/compatibility/python-goal-drafts.json
```

Its HTTP oracle compares all entity envelopes, raw unchanged rows, search data,
protected ledgers, UUID/clock observations and reopened state. The settings and
goal fixtures share that durable-state harness. Production goal-panel behavior,
mobile/LAN/reconnect/device testing and throughput measurements remain required.


New goal-conversation POST creates the provider Session, search projection and
Goal draft in one bounded transaction. It preserves project/provider/MCP read
order, lazy identities and constructor clocks, composer choices and Unicode
titles. Full immutable project/provider hydration uses trusted host context; no
provider, hook, tool, workspace or secret is opened. Account resolution releases
database permits and uses a shared blocking lane (four slots, five-second default
deadline). Abandoned work retains its slot until completion. Unix home expansion
is lexical; other platforms currently reject requests needing home expansion.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_goal_conversations --output assistant-rs/compatibility/python-goal-conversations.json
```

The fixture captures 120 HTTP cases, 78 service cases, 82 project/provider
vectors, 12 Session constructor vectors and 57 request vectors. Fourteen separate
observations document strict retained-base and envelope boundaries. Canonical
dependency rows are temporarily used during inert app construction, then original
raw rows are restored before each request and snapshot. This isolates request
compatibility; it does not establish malformed-history startup parity.

Conversation fork POST copies a retained provider or harness conversation at a
message boundary. It preserves exact sequence ties, parent and source-message
lineage, shared workspace references, private-metadata removal, active conversation
decisions and draft goal configuration. Harness branches create a fresh vendor
record without launching a process. Pending receipt repair runs before the fork
guard; acknowledged repairs remain durable when an active turn prevents branching.

Each source stage commits separately. A later collision or invalid goal can leave
a partial conversation. Harness cleanup deletes only its new vendor record;
cleanup failure replaces the original error and can leave that vendor record too.
The fork has no idempotency guarantee. Complete collection reads fail above
10,000 records or the aggregate 16 MiB bound instead of truncating the branch.
Four shared workflow slots bound fork operations. Accepted operations retain their
slot and finish copying or cleanup after a caller disconnects or times out. This
lane is separate from the four blocking hydration slots. Whole-application shutdown
must drain workflows before closing the writer; that lifecycle is not wired yet.

```sh
PYTHONPATH=src python -m scripts.capture_assistant_forks --output assistant-rs/compatibility/python-forks.json
```

This fixture captures 89 HTTP cases, 69 service cases, 78 direct model vectors,
12 constructor vectors and 27 request vectors. Twelve separate observations
document strict retained-base boundaries. It includes raw request ordering,
nested model-instance diagnostics, UUID/clock order, committed prefixes, failed
harness cleanup and reopened state. Figma's conversation fork action has been
reviewed; checkpoint restoration, workspace isolation, actual execution and the
production browser journey remain separate integration gates.
