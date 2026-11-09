# Durable API delivery boundary

This is repository implementation and isolated-fixture evidence. Compatibility
launchers still use the legacy in-process factory; overlapping legacy Cores must
remain disabled. No existing installation, operational provider or deployment was
used. The execution factory is deliberately required before enabling that role.

Implemented:

- A separate API factory with shared bearer/cookie/CSRF authentication, SQL
  command acceptance, client idempotency keys, bounded delivery viewers and
  independent control capacity. It imports neither the execution kernel nor chat.
- Host-private authenticated encryption for request and response content. SQL
  holds keyed content references rather than request bodies or authentication
  credentials. Retries retain their original receipt; changed intent conflicts.
- Immutable ordered HTTP delivery frames. The terminal marker and command
  outcome commit in the same fenced transaction. A cancelled or abandoned effect
  remains uncertain and is never automatically replayed.
- SQL routing by project, conversation and controller resource references. Missing
  or conflicting project references never fall through to another project owner;
  viewer routing verifies the current role, ownership lease and private key.
- A real API worker lifecycle validating committed PID binding, retained asset
  hashes and schema compatibility before opening its private socket. Heartbeats
  remain independent of public viewers. Graceful retirement requires SQL fencing,
  no owned projects/resources/unfinished commands, and verified OS process exit.

Focused evidence on SQLite and disposable PostgreSQL schemas:

| Selection | Passing cases | Evidence |
| --- | ---: | --- |
| Authenticated encrypted delivery | 20 | Lost response, replacement reader, conflicts, revocation, stale fences, uncertain effect/control bypass, atomic terminal publication |
| Content vault | 7 | Concurrent initialization, stable keyed references, ciphertext/tamper/purpose checks, permissions, size and path bounds |
| API factory | 9 | Real ASGI auth/CSRF/admission, response replay, cancellation, cookie headers, cursors, migration/import boundaries |
| Decision/control routes | 14 | Harness input, browser decisions, device revocation and steering bypass older running work |
| Project/role routing | 18 | Durable references, ambiguous/missing scope, live-owner checks, resource ownership and promoted API selection |
| Graceful retirement | 4 | Real inert API/execution processes, both database backends |
| Real API worker processes | 4 | Both transport and public auth required; candidate readiness/promotion preserves the old process until retirement |

Fresh independent causal validation identified and preceded repairs to terminal
publication and missing control classifications. Initial fixture-only failures
were also independently validated: one assertion expected five frames for a
four-frame response, and synthetic ChatSessions omitted their required title,
model and provider binding. Exact failing cases were rerun; unaffected passing
selections were excluded. Ruff and targeted mypy checks passed.

Local raw outputs and collected selections are `/tmp/nebula-delivery-*`,
`/tmp/nebula-api-worker-*`, `/tmp/nebula-routing-*`, and
`/tmp/nebula-real-api-*`; the latest reviewed receipt is diff-bound in
`.github/test-selection.json`.

Remaining integration is material: stable public supervisor/CLI wiring; complete
execution construction and task fencing; shared host services and overlap
capacity; admission/turn creation integration; producer stream persistence;
handoff resource restoration; content retention cleanup; client keys/reconnect;
desktop attachment, packaging and staged deployer/rollback. Fixture scripts import
repository libraries and do not certify immutable packaged dependencies. The
production LAN matrix and packaged desktop update/reopen remain required. Mobile
WebKit downloads are policy-blocked in this environment. No deployment-continuity
or complete architecture claim is made by these boundary checks.

## Authenticated actor replay

A further 35 selected cases passed in 31.20 seconds: eight new paired-principal
cases across SQLite/PostgreSQL, 20 affected delivery cases and seven exact normal
authentication/pairing/WebSocket regressions. Fresh independent diagnosis found
that the internal transport bearer erased device identity before kernel execution.
Server-only context now binds the already-authenticated durable actor, rechecks
the live device, populates request identity and supports current-device lookup
without retaining authentication credentials. Capability updates and both cookie
deletions on self-revocation work through the real kernel routes. Bearer-only
dependencies still reject device replay. Concurrent device contexts stay isolated;
public actor headers do not create or change identity. Local evidence is
`/tmp/nebula-principal-auth-*`. Execution-role construction is still pending.
