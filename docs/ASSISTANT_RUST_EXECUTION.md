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
