# Global Settings without an execution owner

`mount_global_settings(app, store, actor)` mounts operator-profile lifecycle,
runner-profile configuration and stored model metadata on the global SQL API.
It must receive the same `GlobalSqlStore` and actor context as the durable
request adapter. Mount it before legacy read routes with the same paths.

Operator writes use the existing profile invariant and attribution checks on
the caller's current SQL connection. The adapter never opens, commits or closes
an independent transaction. PostgreSQL keeps the legacy empty-profile-set lock;
SQLite uses the caller's write transaction. Profile changes and their durable
HTTP response therefore commit or roll back together.

Runner list/get/PUT-upsert/POST-create/DELETE validate the existing profile model
and optimistic revisions. A changed executable, runtime, socket, context,
platform, isolation or seccomp configuration invalidates stored health. A name
change preserves existing verification. New profiles are explicitly unverified;
the UI distinguishes saving that configuration from a positive host probe.
Profiles still referenced by saved policy or work cannot be deleted.

`GET /api/v1/providers/{id}/models` pages bounded display metadata from the saved
profile and labels it `source: stored_profile` and `live_probe: false`. It does
not resolve credentials, call inference, grant tool capabilities, or report
current provider health. More than 1,000 models or 512 KiB of projected metadata
is explicitly partial.

`GLOBAL_SETTINGS_HOST_ROUTES` enumerates operations requiring a separate
authenticated host service: local-provider discovery, provider health and
capability probes, OpenRouter directory/credits, runner verification and harness
health. They are intentionally absent from the SQL-only mount. Their external
effects cannot be represented as an atomic SQL mutation. The module never
constructs an execution kernel, project, producer or controller.

Focused verification: 15 tests passed across isolated SQLite and PostgreSQL
schemas, plus five Runner Settings component cases. The SQL fixture forbids
independent `Database.connect()` calls, exercises concurrent idempotent creation,
and injects failure after profile mutation but before response recording to
verify that both state and receipt roll back. The fixture used the unchanged
global API/delivery interfaces from root commit `19915fd7`.

Ruff, targeted mypy, and the production UI build passed. The initial mypy return
type mismatch was independently reviewed before preserving the typed profile
returned from `StoreTransaction.add`. Raw local evidence is in
`/tmp/nebula-global-settings-tests.log`, `/tmp/nebula-global-settings-tests.xml`,
`/tmp/nebula-global-settings-ui-tests.json`,
`/tmp/nebula-global-settings-build.log`, and the diff-bound
`/tmp/nebula-global-settings-selection-receipt.json`.

Full product parity still requires the host bridge for those external routes
and the combined real-Core, production/LAN and browser acceptance journeys.
No provider, credential store, OCI command, execution controller, or live Core
service was exercised by this slice.
