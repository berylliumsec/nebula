# Assistant Rust migration

Status: **incomplete; assistant development foundations**. This is not a Rust
replacement for the assistant yet. The existing application and all other product
areas retain their current implementation. The development executable is named
`nebula-assistant-lab`, never `nebula-core`.

The user narrowed the rewrite to the assistant: conversations, streaming,
provider/harness integration, follow-ups, subagents, and restart recovery.
Whole-Core migration, missions, browser research, reporting, and other workbench
areas are excluded. Shared changes are limited to focused build/test wiring.

Compatibility capture baseline: `b5bab4372871a0fa3fe91968c95d73903796ab56`.
Current integration base: `d2d1ba62e3ad96c966a6612f1a446f6798e96c37`, freshly
verified against origin/main. The intervening queue-controls change affects only
UI files and its test receipt; the Assistant Python/Rust sources are unchanged.
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
replacement. The experimental Rust router has eighteen Assistant handlers; shipped
routes, UI, provider and harness execution have not switched to Rust.

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
missing identities and timestamps still fail closed. The inventory records 85
mounted Assistant route entries. Three now have experimental Rust HTTP handlers;
none is activated in shipped Core and full route parity remains unverified.
There is still no Rust assistant production journey or whole-assistant
performance evidence.

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

Full HTTP integration, context fork orchestration and prompt assembly, catch-up
pending projections, production browser/mobile/LAN journeys and final packaging
remain required integration gates. The initial authenticated HTTP handlers below
are experimental; no shipped routes are activated. Providers and tools are not
run by this layer.

## Assistant HTTP authentication and first routes

Journey: reach saved context/read acknowledgments through `/api/v1` with the same
local bearer token or paired-browser cookies. Paired mutations require the current
Host/Origin and double-submit CSRF checks. Authenticated device identity owns the
cursor; request bodies cannot impersonate another device. Revoked/expired devices
must fail on the next request, and idle refresh must never undo revocation.

Authorities: existing paired-device records (hashes only), configured Core token,
trusted listener scheme, and the Assistant entity store. Capture Python API
responses on temporary databases with deterministic clocks; exercise valid and
invalid credentials, non-ASCII bytes, malformed origins/hosts, revocation, expiry,
lookup beyond 1,000 records, and concurrent idle refresh. HTTP requests get bounded
body/admission limits. No production listener or shipped entry point changes until
transport and product gates pass. Other areas retain their routes and logic.

First transport integration targets saved-context GET/PUT and read-cursor PUT.
Required evidence includes response/error shape checks, authenticated service
mutations and reload, dependency-specific schema validation, and selected Rust
HTTP tests. Full request coercion, remaining routes, WebSocket/SSE, desktop launch,
production UI/mobile/LAN and performance acceptance remain gates for the rewrite.

Implemented: bearer/cookie fallback, double-submit CSRF, Host/Origin checks,
revocation and both expiry bounds, paired-device cursor ownership, bounded request
admission/bodies/response retention, request identities and deadlines. Idle refresh
uses revision checks and revalidates revocation inside the bounded writer lane.
The listener scheme is trusted configuration, never inferred from client-supplied
forwarding headers. Some malformed Host forms are rejected more strictly than
Python; all captured valid Host forms and authentication responses match.

Local evidence: nine affected storage regressions, one device-contract test and
seven exact HTTP tests passed. The HTTP corpus compares 33 Python cases including
complete normalized error bodies and device state; separate tests cover 1,001
older devices, simultaneous refresh, queued revocation, response retention,
deadline recovery, durable context/cursor updates and an isolated TCP listener.
All 24 selected Python contract/selection checks passed; the auth oracle check was
rerun after correcting its canonical starting records. Clippy (warnings denied),
Ruff and formatting passed. Cumulative CI selection: 55 exact Rust / 45 Python.

The three route implementations are listed in
[`rust-routes.json`](../assistant-rs/compatibility/rust-routes.json). This manifest
does not claim route parity: request coercion/validation lists, general service
error envelopes, CORS, diagnostics persistence, streaming and launch integration
are outstanding. No production desktop/mobile/LAN, physical-device or performance
gate is satisfied by the loopback fixture. The requested full rewrite is incomplete.

## HTTP input and failure compatibility contract

Journey: save/edit Context or acknowledge activity, receive an actionable error
for invalid fields or a stale revision, correct the request and retry without
losing history. The existing request schemas own coercion/defaults and validation
locations; durable entity revisions own conflicts and successful mutations.
Authentication must remain authoritative, rejected requests must not mutate
Assistant records, and operation/request identities must stay correlated.

Capture the real Python API's request validation and service failures on isolated
data, including missing/wrong types, bounds, Unicode, numeric revision/datetime
coercions, cross-conversation references and duplicate/stale writes. Compare full
normalized JSON error/success bodies and final persisted state in Rust. Keep body
and response limits explicit. Production UI retry/refresh/reconnect, mobile/LAN,
stream/interrupt and other route coverage remain separate acceptance gates; this
step does not activate shipped endpoints or execute providers/tools.

The transport now validates DecisionWrite/CursorWrite fields in schema order,
preserves Python's defaults/coercions and structured field-error locations, and
retains oversized integer expectations until revision comparison. Duplicate
creation, missing records and stale revisions carry their original diagnostic
features/messages. The shared guidance catalog is embedded at build time.
Negative-zero revisions normalize to zero; year-zero timestamps are rejected
before they can enter durable state. Response serialization stops at its byte
bound, including amplified validation-error lists.

Evidence: 120 Python API cases compare complete normalized response bodies and
23 final Assistant records. Nine affected storage regressions, seven service
regressions and nine HTTP tests passed locally; the oracle and Python regeneration
were rerun for the timestamp/negative-zero boundaries. Cumulative CI selection is
57 exact Rust / 46 Python tests. Large repeat-string fixture values are encoded
compactly and expanded under test bounds. JSON syntax, content-type/method edge
cases, unusual validation-guidance ordering, CORS, diagnostic persistence,
remaining routes and production journey gates still require integration evidence.

## Conversation navigation contract

Journey: open an existing Assistant conversation, read retained messages, search
the current project, jump to a matching message, bookmark it, refresh, and remove
the bookmark. This follows the inspected Figma Assistant transcript (4:73) and
compact search/bookmark controls (16:2), including mobile transcript (7:113).
Core entity rows own messages, project/session relationships and bookmark
revisions; URL selection and transient search controls belong to the existing UI.

Required invariants: project boundaries remain authoritative; temporary sessions
stay out of project search; replaced messages remain retrievable as history but
do not appear in current transcript/search results. Search pagination counts the
same stored rows as Python, including replaced rows. Literal wildcard queries,
Unicode excerpts, ordering and empty pages retain their current meaning.
Bookmark updates are durable, revision checked and scoped to the selected
message/session; inactive records survive refresh. Complete reads must either
return all eligible rows within explicit bounds or an actionable limit error.

Lifecycle coverage for this step: discover/select/read/search, bookmark creation,
refresh/reopen, remove, invalid input and stale-revision retry are required in
isolated differential API and storage/service tests. Authentication/revocation
reuse the selected HTTP regressions. Streaming, interruption, fork and execution
are unchanged and retain their separate rewrite gates. Production UI navigation,
reconnect, desktop Chromium, mobile Chromium/WebKit and LAN-origin acceptance
remain required before claiming the operator journey complete.

Parallel implementation ownership: storage queries, navigation services, and
Python compatibility capture are separate agent tasks; HTTP integration, receipt
selection and cross-layer review remain with the primary agent. Only the
Assistant Rust workspace and its scoped compatibility/validation files change.

Implemented: transcript history GET, project message search GET, and bookmark
GET/PUT. Literal SQLite matching and stored-row pagination retain legacy behavior;
complete transcript/bookmark reads reject more than 10,000 rows or 16 MiB rather
than silently truncating. Search pages retain at most 101 candidates under the
same byte bound. Full Unicode case folding is generated from CPython 3.12's
Unicode 15.0 data. Search dictionaries preserve `+00:00` timestamps while typed
transcript records preserve `Z`, matching the Python responses.

Local evidence: four storage navigation tests, four service navigation tests and
ten HTTP tests passed, plus Python regeneration of the 112-case navigation oracle.
The HTTP comparison verifies all 30 final records after database reopen; service
tests check raw immutable creation times and nondecreasing update times through
bookmark removal/reactivation, competing writes and queued source/session edits.
CI selection is 66 exact Rust tests and 47 Python tests. Shared project lookup
currently validates identity/kind only; its full schema decoder remains a gap.
The search queries retain legacy sorting/correlated lookup costs, with no speedup
claim. Query strings above 64 KiB and offsets beyond SQLite's range fail explicitly.
These limits are deliberate extensions, not silent compatibility claims.

Reader prefetch is limited to one row per SQLite connection before collection
accounting. Nine existing entity-store regressions are also selected locally for
this shared reader change, bringing the focused run to 27 Rust tests. Empty and
overlong nonexistent identities preserve named 404 errors. The six additional
Python navigation cases cover these failures, including multibyte identifiers.

Remaining Assistant dependencies include the shared structured-results GET/list
routes used by Figma's Agent view (97:3); that panel does not use the separate
chat Results drawer endpoint. Their read contracts require acceptance mapping,
while shared result producers and deletion remain outside this Assistant rewrite.

## Conversation catalog and catch-up contract

Journey: discover a saved conversation in the Assistant sidebar, select its URL,
refresh the catalog, review unseen results or failures, inspect a response summary,
and acknowledge activity while pending approvals or questions remain visible.
This extends the inspected main chat (4:73), archive (52:3), nested chat (120:85)
and compact controls (16:2) Figma journeys. Durable entities own catalog membership,
revisions, transcripts and pending requests; the device cursor owns only what was
read. The URL and existing UI own selection and presentation. No read may execute
work, resolve an approval, or advance the connection-state watermark.

Catalog invariants: generated session/message list endpoints retain their bare
array shape, creation-time ordering, project filter and fixed pagination. Session
lists exclude temporary records before paging but include archived/subagent
records for existing UI filtering. Raw message lists retain replaced, orphan and
temporary-session messages. An oversized requested page must fail explicitly;
returning a short byte-truncated array would make the UI stop fetching too early.

Catch-up invariants: pending actions stay visible before initialization and after
acknowledgment. Only active owning turns and unexpired pending requests contribute;
terminal harness receipts suppress stale owners and secret prompts are redacted.
Paired-device identity overrides the supplied cursor owner after query validation.
Preserve strict timestamp comparisons, source-message fallbacks, candidate limits
before retraction filtering, greeting suppression, ordering and truncation flags.
Shared Approval/HarnessInteraction/HarnessTurn records are read dependencies only.

Lifecycle coverage: discovery, selection data, refresh/reopen, empty state, unseen
results/failures, acknowledgment and invalid/stale request recovery are required in
isolated differential HTTP and storage/service tests. Request revocation reuses
authentication tests. Creating sessions, streaming, interrupting, branching,
deleting and actual approval execution remain separate rewrite gates. Required
production desktop/mobile Chromium/WebKit, LAN, reconnect, keyboard/touch and
physical-device evidence remain outstanding until a shipped Rust-backed workflow
can exercise them. Python fixtures never enter Core lifespan or access providers.

Implemented: generated conversation/message list and get routes, catch-up and turn
summary reads. Requested catalog pages return complete arrays up to 16 MiB;
oversized pages fail rather than ending UI pagination early. Catch-up reads one
bounded SQLite snapshot with an aggregate 10,000-row / 16-MiB budget, including
source messages, predecessors and pending dependencies. Shared dependencies are
validated by immutable Rust codecs; these reads never write projection watermarks
or resolve approvals/questions. No provider or model call is involved.

Python hydration normalizes base entity timestamps to UTC. Persisted Assistant
and dependency decoders now do the same while retaining optional timestamp offsets.
Catch-up deliberately preserves SQLite's legacy wall-clock binding of an offset
read cursor; instant comparisons for failures and pending notices remain aware.
This documents compatibility behavior, not a change to the legacy time semantics.
Normal query validation precedes rejection of offsets outside SQLite's range.

Local validation: all 45 selected Rust tests passed (33 affected regressions and
12 new catalog/catch-up cases), plus 11 selected Python checks. One dependency
assertion was updated to the Python UTC expectation after the raw-offset fixture
was added; the implementation was unchanged on that retry. The HTTP oracles
compare 64 catalog cases with 22 final records and 76 catch-up cases with 598 final
Assistant records after reopening the database. Shared dependency bytes and state
projection watermarks remain unchanged in pure-read tests. Cursor creation times
remain immutable and updates nondecreasing without response normalization.

Cumulative CI selection: 78 exact Rust tests and 49 collected Python tests. Shipping,
browser/device/LAN acceptance and whole-Assistant performance gates remain
incomplete. These queries retain legacy query shapes; bounded allocation alone is
not evidence of a throughput improvement.

## Retained Results and context-source read contract

Journey: open the selected conversation's Results drawer, page retained outputs,
follow a source message, review a bounded recorded file-diff preview, and add an
excerpt to the existing unsent context pack. The separate context-sources API
reports retained attachments/decisions and the current policy display; it has no
standalone production UI consumer. These projections never execute a tool, fetch
an external source, or synthesize evidence from model claims.

Authorities: the existing session/project identity, stored message sequence and
retraction markers, retained ToolCall/ChatTurn association, and project-owned
Artifact records. Offsets count stored messages before filtering; preserve output
ordering, null fields, opaque context metadata and exact policy strings. A missing
or mismatched diff blob yields its existing unavailable-preview text while other
results remain visible. File previews read at most 8,192 bytes from the configured
digest-addressed artifact store, with bounded blocking work outside SQL snapshots.
Complete response expansion is bounded explicitly; never silently drop outputs.

Planned evidence: an isolated Python HTTP oracle, immutable dependency codecs,
bounded SQL snapshot tests, exact fence/citation/tool association cases, artifact
path/preview tests, recorded-context policy variants, HTTP validation and database
reopen. No Core lifespan, provider calls, external commands, or live state. Shared
result producers, source-document extraction and artifact download APIs remain
separate dependencies. Production desktop/mobile Chromium/WebKit, LAN-origin,
refresh/reconnect, source navigation, draft preservation and physical-device gates
remain required before claiming the complete Results journey.

Implemented: Results and context-source GET routes, immutable ToolCall/Artifact
codecs, and a complete bounded snapshot before file I/O. Projections retain
Python output order, raw-message cursors, null values, context metadata and policy
strings. Calls are indexed once by their retained turn's final-message reference;
legacy cross-session references are preserved rather than silently re-scoped.
The shared 10,000-row / 16-MiB snapshot includes dependencies and lookahead rows.
Expanded output has its own row and encoded-byte checks and fails as a whole.

Artifact previews use directory-relative Unix handles and an existing host-owned
root. They refuse symlinks and non-regular files; each read is capped at 8 KiB.
The blocking admission permit survives caller cancellation or timeout until the
read finishes. Missing files retain the existing fallback text. Symlink refusal,
capacity/deadline errors and resource bounds are explicit compatibility extensions.
No entire-blob hash verification is claimed. Current Linux x86_64 packaging is
unchanged; this library has not passed packaging or production gates.

The new oracle covers 110 HTTP cases, 133 retained Assistant records, 30 shared
dependencies and ten harmless blobs. Catch-up now has 78 cases with 606 final
Assistant records, adding empty and overlong historical project scopes. Retained
project values are not entity IDs: catch-up, bookmarks, decisions and Results now
keep those values while still applying exact project filters.

Local validation: all 44 selected Rust tests passed (nine new and 35 affected),
plus three selected Python oracle checks. The Rust fixtures initially contained
an invalid decision scope, an extra turn field, zero-based message sequences and
oversized message text; these were corrected to canonical values, using opaque
metadata for aggregate-byte tests. The implementation was unchanged on these
retries. Tests cover raw record purity, unchanged projection watermarks, reopen,
large collections, explicit expansion failure, bounded file paths and deterministic
preview timeout/admission recovery. This is library/isolated-HTTP evidence only.
Cumulative CI selection is 87 exact Rust tests and 50 Python tests. The production
Results journey and all whole-Assistant performance gates remain incomplete.


## Activity, saved queue and retained hook read contract

Journey: select or refresh a conversation, understand the sidebar's idle/working/
waiting indicator, inspect the saved follow-up queue, and expand recorded native
hook outcomes for a known turn. These reads support the inspected main chat,
compact controls, nested-chat and recovery designs. Core entities own activity,
queue contents and hook receipts; the existing UI owns expansion and selection.
A read must never start a queue runner, invoke a hook, reconcile an uncertain
execution, dispatch a turn, or turn a temporary default into a saved record.

Preserve activity's project-wide conflict detection before session filtering,
all historical project values, canonical temporary-session filtering, and the
string-valued subagent marker (including empty strings). A missing queue returns
an ephemeral revision-zero response; an existing canonical-ID queue retains its
stored scope and opaque item order. Hook summaries retain timestamp ordering,
late outcomes and reconciliation while omitting raw snapshots and process output.
Read every same-session hook before turn filtering, matching legacy validation.
Use complete snapshots with shared row/byte bounds and explicit limit failures.

Planned evidence: a fixed-clock isolated Python HTTP oracle; immutable hook codec;
scoped storage/projection/HTTP cases for retained values, invalid records, limits,
missing identities, default queues and reopened state. Discovery/selection data,
empty/error states, refresh/reopen and revocation are required in this slice.
Queue writes/dispatch, pending-turn and session-hook reads that reconcile receipts,
full recovery and the production browser/device/LAN matrix remain separate gates.
A consistent Rust queue snapshot will replace the legacy GET's racy second
existence lookup; document this as a consistency extension. No speedup claim is
established by these read ports.

Implemented: three read routes for project activity, saved queue state and a known
turn's retained hook summaries. Their immutable snapshots share row/byte bounds;
1,001-session/hook reads verify completeness beyond the Python page size. Activity
checks pending conflicts before reporting session decode failures, and queue GET
never creates a record. Hook summaries preserve aware/naive timestamp semantics,
late outcomes and opaque reconciliation while omitting raw process output.
The 40-case Python oracle retains all 97 Assistant records and 14 hook dependencies
exactly across reads and database reopen. It includes canonical conflict, malformed
opaque recovery, timestamp-comparison and ephemeral queue-validation errors.

Known limits: malformed hook schemas currently receive a sanitized Rust 500 failure
instead of Python's detailed 422 model-validation envelope. Missing recorded late
observation timestamps are refused rather than generated during hydration. Complete
snapshot limits and the atomic queue existence check are explicit extensions.
These observations do not establish shipped UI or full recovery behavior.

Local validation: 25 exact Rust tests passed (eight new status cases, 14 HTTP and
three dependency regressions), plus the one status-oracle Python test. The latter
passed in 6.10 seconds; the cumulative Python selection collects 51 tests. Raw-row,
envelope, projection-watermark and ephemeral-queue assertions passed before and
after reopening. Cumulative CI now selects 95 exact Rust tests and 51 Python checks;
no browser, execution, migration or performance gate is inferred from these runs.
