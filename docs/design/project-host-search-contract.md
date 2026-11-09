# Project search through the host owner

Operators enable public research in Project > Policy and select upstream engines
in the existing host Web Search settings. A project query uses that single host
service. Project execution processes cannot create, adopt, configure or stop a
metasearch container, and an unavailable host never triggers a project fallback.

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover and opt in | `web.search` appears only for opted-in, non-local projects | Saved project scope and existing tool catalog | Existing registration and policy checks |
| Query | The selected host engines answer through one host-owned service | Host settings and managed search owner | Signed RPC plus real tool broker checks |
| Wait or cancel | Admission waits outside the SQL ownership transaction; cancellation propagates | Host memory admission and execution lease | Blocked-query and cancellation checks |
| Failure and retry | Refusal is visible in the tool receipt; retry cannot start a project service | Typed host failure and durable memory job | Failure receipt checks |
| Revoke | A changed scope, engine selection or execution lease rejects the pending answer | Core database and exact fenced project grant | SQLite/PostgreSQL race checks |
| Handoff or refresh | A fresh execution process receives a query-only adapter bound to its new grant | Retained execution construction and host capabilities | Combined retained-worker acceptance required |
| Service shutdown | Closing a project adapter never stops the shared search owner | Host lifecycle | Borrowed-client close check |

The private operation is `host.web-search.query/v1`. The signed peer's immutable
runtime identity, current project lease, scope identity and revision must match.
The host validates opt-in, local-only, policy prohibitions and time windows, and
the existing target-disclosure guard. It checks the same grant and scope again
after admission and I/O, and rejects an answer if host engines changed. No query
is kept in the protocol's errors or receipts. Normal search tool receipts retain
their existing redacted query and bounded results.

Requests bound the query to 256 characters and results to ten. Responses carry
the exact grant and scope, consecutive ranked HTTP/S results, and bounded typed
failure data. When available, the failure preserves the canonical memory job ID,
state, outcome and validated enforcement evidence in the tool execution receipt.
Missing host capability, failed transport and invalid responses fail closed.

`RuntimePlatform` and `default_runtime_platform` accept a query adapter. Their
default is unavailable, so constructing a project platform never constructs a
service owner. `HostObservations` owns the managed runtime and registers it with
the host service; the execution worker injects `RemoteProjectSearch`. This
adapter borrows the existing signed client and does not close it on project
shutdown. Native harness behavior and upstream engine selection semantics stay
unchanged.

This branch's focused checks use inert search responses and synthetic data. A
final combined source pin must additionally exercise a real retained execution
worker against the private host listener, then the authenticated production LAN
journey. Browser/device and installed-runtime acceptance are separate gates; the
focused protocol checks do not claim those outcomes.

The retained-worker regression is
`tests/v3/test_runtime_host_search_process.py::test_retained_worker_queries_one_host_owner_for_two_project_grants`
and collects exactly two cases for SQLite and PostgreSQL. It archives and hashes
the candidate package, deletes its staging source, and starts that immutable
execution bundle. The child uses an execution-scoped database and the actual
platform broker, checks the host query capability, and sends queries through the
private host listener for two separately fenced project grants. The host's only
query owner returns inert results; no upstream engine or container is started.
Assertions read each completed durable tool receipt and require that both calls
hit the same host object after each borrowed project adapter closes. Run this on
the combined HostSharedServices/worker binding source, not an older branch that
does not advertise the query capability.
