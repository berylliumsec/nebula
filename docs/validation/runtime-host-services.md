# Shared host service lifetime

The supervisor now owns a private Unix listener for shared Butler memory and
initializes the persistent host-capacity policy. Workers derive the stable private
endpoint from their retained control endpoint and use their own per-incarnation
credential, without adding credentials or new fields to bootstrap arguments.
Both live execution identity and the authenticated MAC are required; API roles
and expired peers cannot call the service. The existing public listener never
exposes the private route. Capacity initialization preserves an existing policy.

Eight concurrent calls and sixteen live peer authentication windows bound the
listener. Accepted memory operations join SQL/index completion through cancellation;
supervisor detachment drains that work before releasing exclusive host ownership.
Detachment preserves child workers. A replacement service uses the same endpoint,
SQL memories, monotonic cursors and saved capacity. Socket startup refuses to
replace a regular file or symlink, and cleanup verifies its owned socket identity.

The 26 selected SQLite/PostgreSQL cases now have passing evidence across focused
runs: 12 new real-socket/detached inert-worker cases, 10 affected stable-listener
cases, and four exact shared-memory fence/identity cases. The initial run passed
18 cases but exposed eight socket failures before authentication. Fresh independent
source diagnosis traced Uvicorn 0.34's bound-method autodetection selecting ASGI 2
for a valid ASGI 3 method. Explicit interface selection repaired the adapter; the
eight affected cases passed in 21.14 seconds. They include a blocked index operation
proving that supervisor detachment retains its lock until the operation finishes.
Ruff and targeted mypy passed. Raw outputs/receipts are
`/tmp/nebula-host-services-*`.

No existing installation, operational provider/service, SSH, deployment or compute
workload was used. The [compatibility memory import](shared-memory-import.md) is
now validated, as are [shared knowledge-index operations](shared-knowledge-index.md).
Production execution-factory wiring remains pending. These are
backend lifetime checks; production LAN/reconnect, mobile WebKit and packaged
desktop acceptance evidence are still required for deployment continuity.

## One owner and guidance synchronization

A further 34 collected cases passed in 51.43 seconds on SQLite/PostgreSQL: 28
private-service cases, two supervisor replacement cases and four inert-worker
retirement cases. Fresh independent source diagnosis identified two lifecycle
gaps: a second service using the same host did not acquire another flock, and
both installed Unix loop implementations unlink an existing socket before bind;
direct process-host close also omitted service draining. The host now registers
one service owner, serializes service initialization/detachment, rejects calls
after host detachment, and drains its service even through direct host close.
Regressions cover duplicate/concurrent starts, socket identity, accepted index
work and close during initialization. Empty explicit capacity fails closed.

The host also mirrors its current packaged product guidance into Chroma without
downloading a model. Actual collection count and source digest were verified;
restart reused them without rewriting. A simulated mirror failure reports
`degraded` while packaged product search remains available. This validates the
current host bundle's guidance; promotion-time guidance versioning remains part
of the deployment integration. Ruff and targeted mypy passed. Raw evidence is
`/tmp/nebula-host-owner-*`. No operational installation or service was contacted.
