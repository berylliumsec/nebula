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

The next preparation increment preserves packaged operator-help retrieval when
an operator sends relevant product or failure text, including when engagement
knowledge is disabled. The release-bundled corpus is the authority: ranking,
token budgeting, citation fields and prompt bytes must match Python. This is a
pure preparation step before durable admission, with no provider or workspace
access. Planned evidence uses the captured corpus/search/projection vectors,
overlapping keyword and non-overlapping occurrence cases, bounded inputs and
redacted diagnostics. Actual completion/stream/reconnect UI evidence remains open.

Four exact Rust help checks and one Python source recapture passed under
`3c520c71b344a3abd1f4fc3324dc2d89fed5d2ae782bd8f363d536c5587760c1`.
The Python check passed in 4.80 seconds. The corpus contains 14 articles; the
fixture contains 37 search, 17 projection, seven integer-budget and six substring
count observations. Capture exhaustively compares the existing 1,530 casefold
mappings against CPython 3.12 / Unicode 15. The port reuses that map and a pinned
keyword matcher, with a static occurrence index. No throughput claim follows
from the algorithm choice. Input/output bounds are per operation; aggregate
preparation-memory ownership remains the runtime's responsibility.

Provider preparation next binds an already hydrated profile to metadata from
the same detached SQL row, preserving opaque dictionary order without repeating
factory clocks. Trusted injected credential/environment access follows source
precedence; the bridge itself performs no ambient lookup or network operation.
Catalog adapter identity, locality/privacy, model allowance, capabilities and
header configuration must match source vectors. Native adapters remain explicit
missing capabilities. Focused validation covers the profile vectors, metadata
binding/secret limits and the shared empty-schema capability correction, plus
the existing exact protocol projection test affected by that correction.

Those four exact Rust checks passed under
`1efe9d34893898cd45da6d927b58c5ce502dd160d663dc0d4346d3b7264c91c3`;
the Python recapture passed in 1.62 seconds. Its 130 vectors cover 25 catalog
entries, injected access traces and metadata ordering. No live provider request
was made. The shared adapter now agrees with Python that an empty response-schema
object does not require structured-output capability.

The next integration preserves saved operator decisions on the first send before
a durable Session exists. An absent Session selector uses SQL IS NULL; it must not
be replaced with a fabricated Session identity. Full selected-row validation
precedes active filtering and context budgets, including invalid inactive rows.
The existing decision list and mutation APIs remain unchanged. Three exact Rust
checks and one isolated source recapture will cover optional selectors, validation
precedence, prompt bytes, unchanged rows/reopen and explicit retained-data bounds.

Two decision Rust checks and the Python recapture passed under
`7cb5f01e2842159dae6cf8160b3baad01ce4968b2bbebaa592c8b697f3bf92d6`;
the Python check took 1.70 seconds. The formatter check exposed serde Value's
floating-point conversion of a large integer power of ten. Revision decoding now
captures raw JSON spelling first. The unchanged formatter fixture passed under
`309945810d46dafb8dafd70bde299619c0d0c5f92d09a6f6eaa9ab4622c35cd4`.
Only that failed check was repeated. The corpus has 25 source scenarios, three
format vectors and a separately identified strict-envelope observation. This
increment passed 11 unique exact Rust checks and three Python source recaptures.

The preceding committed increment `7e01619` passed CI run `35981730825`: 246 exact
Rust tests and 70 Python tests on Python 3.12, the latter in 131.14 seconds.
Downloaded execution/selection receipts match
`616ff5272acdad2352b0d28c0fcf997ef9b51ae9dc193a187f5b8b4de3e8d357`.
Python 3.11/3.13 were omitted. Browser plan/gate run `35981730853` passed with
Playwright execution skipped. This CI evidence covers that committed increment,
not the subsequent help/profile/decision preparation changes.

CI for `f7fc383` found that provider-profile recapture compared interpreter
provenance as behavior: Python 3.12.14 differs from the captured 3.12.11 version
string, while every other captured field matches. The source oracle already
requires CPython 3.12 / Unicode 15. Preserve the fixture's complete interpreter
version, require the same major/minor family when comparing, and continue exact
comparison of all behavioral vectors, source hashes, catalog and static tables.
This changes test metadata comparison only; the send/credential journey, durable
state and provider configuration remain unchanged. Rerun only the failing exact
Python recapture under a fresh diff receipt; no Rust or browser expansion follows.

That exact recapture passed locally in 1.61 seconds under
`698255737ae1328e8f0a19298050846b1a85dbfe07eff8824db6a0b4ca106c12`.
The completed `f7fc383` CI run `35984273210` executed and passed all 256 selected
Rust checks. Its downloaded execution and selection receipts match
`a7474e91ef36d101ab5d2aad13836ae62d7894a9068a85002e739cd7b3cb74f4`.
Python had 72 passes and the single interpreter-metadata failure in 142.28 seconds.
No application code or fixture vectors changed in the correction; the next CI
selection repeats only that failed Python recapture. Browser planning passed
with Playwright skipped; no production Rust UI or performance gate ran.
