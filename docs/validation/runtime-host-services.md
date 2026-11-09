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
