# Assistant Rust execution contract

This is the next implementation milestone after conversation forks. It is not a
claim that Rust executes the shipped Assistant or meets the performance targets.
Other product areas remain outside the rewrite.

The first continuous journey is a project-scoped provider conversation: select a
configured runtime, send plain text, see durable acceptance and ordered output,
switch away, reconnect, Stop, refresh and recover after restart. Implement new
and existing conversations together. This first execution configuration has no
tools, hooks, knowledge retrieval, browser/SSH/MCP resources, images, active Goal,
children or peer messaging. Those supported capabilities remain required for the
complete rewrite; unsupported selections must fail explicitly during development.
Do not route the shipped Assistant into an incomplete subset.

## Observable contract

| Journey step | Invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover | Existing runtime choices stay visible and meaningful | Stored provider profiles and UI | Real configured catalog and production browser |
| Create/send | Accepted input and its Turn exist before acceptance is published; rejected admission leaves no partial input | Entity transaction, bounded admission reservation | Source differential and crash/duplicate-send tests |
| Select/use | Returned identity, URL, sidebar and transcript agree | Durable Session and URL | Production browser through new/existing chat |
| Stream | Ordered committed events and complete transcript survive slow or detached viewers | Per-session worker, event store, replay cursor | Real HTTP simulator, slow-client and reconnect tests |
| Stop | Stop stays responsive under streaming load; a stale worker cannot append afterward | Durable execution fence and cancellation owner | Contention and Stop/completion races; production browser |
| Background/reconnect | Reattachment observes existing work and never repeats an uncertain initial POST | Durable Turn and cursor | Disconnect before/after first token, background/resume |
| Refresh/restart | Saved answers remain visible; uncertain effects are explicit before dispatch | Durable receipts, claims and startup classifier | Crash at each admission/settlement boundary |
| Failure/retry | Errors explain a valid action; transport loss alone never authorizes another effect | Provider classification and completion evidence | Refusal, throttling, EOF, timeout and retry cases |
| Fork | Retained branch data uses the existing fork service; sending creates work in that selected branch | Session lineage and new Turn | Branch/send/refresh journey |
| Delete/revoke | Existing credentials revoke access; deletion must coordinate active ownership | Device auth, Turn and Session records | Auth regression; full deletion remains a separate required service |

React state owns unsent input and presentation only. Workspace files retain their
existing project identity; instructions are read from that workspace. Provider
connections and helper processes never own Core scheduling or mutate its database.
No database transaction or global lock spans provider/network waits.

## Implementation boundaries

Port admission, claim acquisition, fenced answer persistence, completion and claim
release as narrow writer commands. Preserve the source's separate answer,
completion and release commits. Use trustworthy committed message/Turn references
to classify crash gaps; do not replay an answer merely because its final pointer
or claim release was interrupted. Ambiguous or mismatched evidence must remain
explicit. Source sequence allocation occurs before some retries, so its comments
alone are not proof of uniqueness under concurrent transcript mutations.

Connect the existing fair queue to durable admission and per-session ownership.
Reserve bounded capacity before input persistence, dispatch only after commit, and
rebuild from durable state after recovery has classified it. Keep legacy status
enums; scheduling visibility may be added as optional metadata. Whole-workflow
shutdown must join detached forks and turn workers before closing the writer.

Use pooled provider connections and bounded request/response/parser memory.
OpenAI-compatible transport is the first adapter; normalized contracts must retain
the other provider shapes without claiming those adapters are implemented. Parse
tool-shaped output as inert protocol data in this configuration. Preserve source
framing, reasoning, error and retry semantics, with explicit limits for previously
unbounded parser paths. SDK-specific helpers remain narrow dependency adapters.

Provider token history is currently volatile in Python; harness activity has a
durable operation ledger. A new durable provider replay store requires an explicit
additive schema, retention, rollback and cursor contract before implementation.
Do not reuse an unrelated ledger without defining ownership. Notifications follow
commit; a lagged viewer replays by cursor instead of losing transcript data.

Separate buffered JSON response credits, streaming connection/buffer credits and
short control-request capacity. The current four 16-MiB response reservations
cannot support 100 streams. Per-record limits also do not bound 100 concurrently
hydrated histories: charge aggregate context, event and pending-work bytes. Keep
overload errors actionable and measure fairness, cancellation and recovery.

## Validation and design evidence

Capture actual Python preparation, normalized provider requests/events and durable
phase changes with deterministic providers and harmless isolated workspaces. No
live service, model account, browser automation, research tool or external effect
is part of these fixtures. Select and collect exact tests before execution, with
diff-bound receipts and explicit runtime limits. A parser test or synthetic done
frame does not establish a functioning turn.

Required product checks use the production UI with isolated Rust and local provider
simulators: desktop Chromium 1440/1024, mobile Chromium and WebKit 320/390/430,
non-loopback LAN, keyboard/touch/focus, long content, loading/error states,
background/reconnect and revocation. Label emulation and physical testing
separately. Performance still requires the agreed 1/25/100-agent measurements,
historical fixture, repetitions, soak and overload tests.

On September 24, revisited live Figma desktop chat `4:73` (1440×1121), mobile chat
`7:113` (320×844), and restart recovery `7:5` (560×286). Interrupt and consequential
approval state remain visible on both sizes. The recovery frame's old manual
resume wording does not override the later receipt-driven recovery contract.
These are design inspections, not interactive prototype or production acceptance.

## Component validation recorded during implementation

The source execution fixture captures 44 HTTP cases, six phase/restart cases,
35 request vectors and 15 Turn observations. Its source recapture passed once
on local Python 3.11 in 9.69 seconds. Ten exact Rust request/shared-model/diagnostic
checks passed across two corrected runs. The first failures exposed a real
arbitrary-precision integer conversion in the request wrapper and generic HTTP
decoder; canonical values now move directly instead of being deserialized again.
This evidence does not establish preparation, dispatch or browser execution.
The Rust run receipts are retained under `/tmp/nebula-assistant-completion-*`;
final cumulative CI will bind the finished increment.

Execution result memory uses lifetime accounting, not only per-command size
checks. The writer extension reserves aggregate result bytes until the
last returned record owner drops; clones share storage. Runtime preparation,
validation-error inputs, provider buffers and viewer replay need separate budgets.
The isolated debug artifact cache was regenerated after host disk exhaustion;
local checks use zero debug symbols and disabled incremental compilation. Release
optimization and the agreed benchmark profile remain unchanged.

The replay design review found that Python's numeric per-Turn cursor can restart
from one after a rollback. Unchanged Rust lineage may continue its durable cursor;
a Python-mutated Turn cannot concatenate the old Rust stream with new output.
A current authoritative final answer remains available as the source-compatible
unsequenced terminal snapshot. Otherwise recovery must expose discontinuity until
an epoch-aware resume contract is implemented. Claim absence alone never proves
that a provider request was not sent.

The integrated component selection passed 39 exact Rust tests and 24 Python
tests. The initial run passed 12 Rust and 23 Python checks before two test-only
assertions failed: Python's generated missing-created-at value can itself fail
chronology, and splitter tuples become arrays in JSON. Neither correction changed
the source fixture or runtime implementation. The remaining 27 Rust checks passed
under digest `5739a3b8591fa8b4aa96eacc92bd692d9b14d298e406f324c1665aea016539e5`;
the corrected provider recapture passed in 0.63 seconds. The first run used digest
`524832f096a39f6784b1c5e40cb8f1a8ed1123354c9fe5118af7d148a490367e`.
These historical run digests precede final documentation and cumulative selection.

Turn hydration covers 113 retained/writer vectors, ten constructor vectors and
four strict missing-base observations. Provider protocol capture covers 134
vectors: 29 payloads, 28 completions, 36 streams, 36 framing cases and five reasoning
cases. Seven execution writer tests exercise separate source commits, reopen,
contention, Stop/ownership fencing, preserved operator settings, terminal-note
deduplication, cancellation, result-capacity rollback and historical Turn coercion.
Actual local TCP tests prove keep-alive reuse with separate credentials, refused
redirects, early retry boundaries, decompression limits and cancellation. They
do not contact a configured provider or run a tool.

The OpenAI-compatible adapter has explicit unresolved advanced replay modes;
serialized tool-control output remains inert, and future execution must withhold
possible control-prefix deltas from plain-answer viewers. Source preparation,
credential resolution, substantive-chat naming, durable event/dispatch receipts,
per-session workers and the public completion/Stop/reconnect routes remain open.
No additional mounted handler, production Rust journey or measured speedup is
claimed by this increment.

CI for `bdb1003` passed (run `35971438240`): 210 unique exact Rust checks and
67 Python checks on Python 3.12, the latter in 132.05 seconds. Downloaded selection
and Rust execution receipts match digest
`ee17c4adc54d1d4d9f603e0ef9ab2ad73103696cce891a6bf6638b0649993bcc`.
Python 3.11/3.13 were explicitly omitted. Browser plan/gate run `35971438234`
passed with Playwright execution skipped. These checks bind the committed
execution components, not the following uncommitted integration work.

## Next integration boundaries

The next change connects the prerequisites for durable queued work: explicit
schema installation on isolated recognized databases, non-dispatchable queue
reservations, pure context/history preparation, and bounded source-shaped SSE
encoding. Schema recognition and a scheduler reservation alone never authorize
provider dispatch. The operator invariants above remain the acceptance contract.
SQLite owns atomic evidence/epoch changes; FairQueue owns ordering and capacity;
source preparation owns prompt/history semantics; immutable encoded events own
the bytes later committed and replayed. Public diagnostics receive identities
once, before commit. Slow viewers never own a provider receiver.

Planned focused evidence covers migration-marker preservation and refusal of
unknown schemas, update/restore/delete epoch tracking, out-of-order admission
commits within a Session, stale reservation tickets, Python context/history
vectors, exact public event bytes, bounded encoding and unsequenced terminal
snapshots. Collect and review exact selectors before execution. Runtime dispatch,
crash-safe receipt transitions and production browser journeys remain separate
required work.

Twenty-one exact checks passed for these three foundations under digest
`dc871b9aab1c7450fe5dc9d4f6fa9e7e23172e15429123bb3bcefe9a1138900c`:
14 reservation/fairness checks, four schema checks and three SSE codec checks.
The schema preserves legacy markers/rows and refuses unknown or partial owned
DDL. Seven owned tables use WITHOUT ROWID; watched legacy identities use an
indexed physical locator to detect replacement even when recursive triggers are
disabled. Tests cover negative rowids, automatic allocation, replacement of two
watched identities and rollback on epoch exhaustion. Physical locators remain
derived: maintenance/rebuild/cutover must reconcile them before the future
runtime uses evidence. No production extension was installed.

The event codec preserves raw JSON order/number spelling and committed SSE bytes,
assigns only JavaScript-safe positive cursors, rejects ambiguous/injectable frame
fields, and leaves terminal snapshots unsequenced. It owns neither database
commits nor permission to publish. The reservation policy holds pending capacity
without making uncommitted work runnable; queue-bound generations prevent stale
tickets from affecting a reused work identity.

Four additional exact Rust preparation checks and one Python source recapture
passed under digest
`885d59616db75bcde61c6a454a0d194af53dcb40eb99032086e01bebbf3a9baa`.
The Python check ran in 2.21 seconds using the existing read-only CPython 3.12
environment. The 99-vector corpus covers context limits/catalog lookup, UTF-8
estimates, visible/replaced/outcome history, selected-context reconstruction and
assistant-message joins. Capture requires Unicode 15.0.0; explicit Kawi and Nag
Mundari digit cases distinguish that baseline from Python 3.11 / Unicode 14.
Static tables are compared in full. No source comparison is skipped.

Together these local selections executed 25 unique Rust tests and one Python
test for this increment. They do not connect admission to workers or establish
stream publication/recovery. The ledger command engine and supervisor-owned
admission controller are the next integration steps. Production UI, historical
migrations, full provider/harness coverage and performance gates remain open.

CI for `b47771d` passed (run `35975820117`): 227 exact Rust tests and 68 Python
tests on Python 3.12. Downloaded selection and Rust execution receipts both bind
`a5390c74d8a0e6594a75793da5afcdac54d2a4a24bc733c5e4165914f5442564`.
Python 3.11/3.13 were omitted. Browser planning passed with Playwright skipped.

The following integration work owns admission through caller disconnect and
shutdown, preserves immutable receipt/event identity inside the SQLite writer,
and resolves project privacy and root instructions before any future dispatch.
ScopePolicy reads use the same bounded dependency reader; malformed or missing
referenced policies cannot become unrestricted access. Instructions come from
the host-authorized linked workspace and are reread every turn, with the exact
source 64-KiB prefix, UTF-8 replacement, receipt and prompt formatting. No new
untrusted path becomes a filesystem capability. Blocking reads, returned content
and admission error materialization each require their own bounded ownership.
Focused evidence will use isolated database/workspace fixtures, exact selected
Rust tests and source recaptures. Continuous completion/Stop/reconnect journeys
and the production UI/performance gates remain open.

Local integration checks passed 24 unique exact Rust tests and two Python source
recaptures. Five admission checks passed under
`3f6a372a054d13089959523907415368608994358279fec9250fddf28fee14d0`;
the two Python recaptures also passed in 5.86 seconds. A scope vector then exposed
an implementation error: CPython's CIDR prefix parser requires ASCII digits,
unlike generic Python integer conversion. The fixture was preserved and the Rust
parser corrected. Two scope model checks passed under
`b0fff42e4ebb90c1ad28957e11471c8d6033d4dcf348ff54984f97b4571b912a`.
The reopen test next needed an awaited writer shutdown before reacquiring its
file lock; this was a test-only correction. The remaining 17 checks passed under
`bf1cb2da8740f0b28fe110af575fc825bffc71987b7b4fc2d2acfca7524b562c`.
Passed selections were not repeated or broadened during either correction.

The policy corpus has 148 direct model vectors and 12 isolated-store privacy
cases; four separate observations retain the strict missing-base-field boundary.
Project instructions have nine content and 14 path cases, plus isolated tests of
retained result credits, changed content, special files, substituted symlinks and
blocked-work ownership after timeout/caller cancellation. The source permission
fixture requires an unprivileged POSIX user. Neither module resolves provider
secrets or grants any execution capability. The ledger writer remains excluded
from these runs and has its own pending transaction/replay selection.

The subsequent ledger selection passed eight exact Rust checks under
`d30ff9ea668451b3e01eb04c0a543a04db26160d15569e7181dea95473504410`:
four new transaction-engine checks and four existing schema checks. They cover
outer-transaction rollback and reopen, lost-ack deduplication, identical adjacent
deltas with distinct identities, exact replay bytes, corrupt/future cursor
refusal, immutable attempt/watch fences, and logical quota conservation. Terminal
events link typed settlement evidence. Explicit release returns unused reservation
only after definitive settlement; recovery uncertainty retains its reserve.
The preceding experimental schema is explicitly incompatible. No live schema
was installed, and no automatic migration is supplied.

Across this increment, local selections passed 32 unique exact Rust tests and
two Python source recaptures. The engine still requires composition with actual
execution entity mutations on the authoritative writer connection. Staged results
never authorize publication or dispatch. Aggregate replay-result credits, startup
reconciliation and the continuous production workflow remain required.
