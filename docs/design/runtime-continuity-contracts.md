# Runtime continuity contracts

The host owns the stable listener, installation identity, retained releases and
shared services. API workers authenticate and serve SQL reads. A project worker
owns only its exact project incarnation; replacement never widens that authority.

`GET /api/v1/runtime/identity` requires normal Core authentication and returns
nonsecret `core_id` and `principal_id`. The Core ID is persistent SQL state, not a
URL, build ID or credential hash. Operator credentials share the `operator`
principal; paired sessions use `device:<id>`. Re-pairing creates a new namespace.
The response is private and never cacheable. Tokens are never storage keys.

Every submitted mutation has one immutable client-generated `Idempotency-Key`.
`GET /api/v1/runtime/requests/lookup?idempotency_key=...` resolves a lost response
without submitting another command. Lookup, status and response reads are scoped
to the authenticated actor. Unknown and other actors' keys return the same 404.
The existing command ID, status, response and stream cursor formats remain valid.
Some routes answer 202 as their final response, so status alone never means
waiting. Every answer to a mutation or response read carries
`X-Nebula-Request-State`. `pending` is a receipt or status envelope: the request
is saved and waiting, so clients honor Retry-After and never interpret it as an
event stream. A chat turn's saved 202 acceptance is also `pending` until its turn
stream is ready. `complete` is the request's own response, replayed with its
original status, headers and body. `needs-reconciliation` is the 409 for an
uncertain outcome, which requires reconciliation, not replay. Without the marker
(older Cores), only Core's receipt, `{state, command_id}` status and chat
acceptance envelopes are pending.

The host can report authenticated health with `api_ready: false` and recovery
path `/settings#memory-budget-settings`. This proves recovery availability only.
API readiness, prepared project restoration and activated execution are distinct.
Only verified restoration permits new producers. Unsupported live resources stay
with their existing owner without an expiry-based kill deadline.

Provider request snapshots with exact instruction ownership require
`provider.tools-context-ownership/v1` in addition to historical tools knowledge.
The turn retains a bounded version projection when large request values move
into immutable parts. Admission also recognizes existing unprojected proofs;
an older reader must not discard the proof and reinterpret operator text.

The authenticated management interface accepts an operation ID, action and
parameters. Its SQL ledger binds that exact request and actor to an observed
outcome. A lost external response is uncertain until authoritative reconciliation.
Shutdown is separate from closing a desktop viewer and refuses unresolved effects.
Code rollback selects compatible retained code and preserves current SQL data.

Desktop attachment uses a canonical data directory and verified retained assets.
Explicit data-directory settings take precedence. Existing ambiguous roots are
not merged or relocated automatically. Closing, replacing or restarting a viewer
does not terminate the supervisor or execution workers.

Pending attempts and unsent drafts have separate storage. Normal submitted
requests retain immutable bytes and key; large non-replayable uploads expose that
limitation. Pending records are bounded to 8 MiB total and 1 MiB each. Drafts are
bounded to 128 records, 2 MiB total, 256 KiB each and 30 days. Unresolved attempts
are never silently evicted. Browser storage failures are visible. Reopening reads
authoritative state and never submits a draft or resumes a producer automatically.
