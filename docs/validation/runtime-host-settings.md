# Stable-host Settings validation

The authenticated stable listener owns external Settings effects and local host
observations without creating a project Kernel. Provider health/discovery/model
verification/account credits, runner and harness probes, credential CRUD, host
CPU/pressure policies, Butler file/check interval, diagnostics configuration, and
shared automation image preparation are available while the public API worker is
absent. VPN, TypeSafe and Web Search reads use canonical host observations.

Host mutations require the ordinary client Idempotency-Key and use encrypted
RuntimeCommandRow intents and delivery frames. Acceptance shares the global SQL
sequence lock; external effects run after acceptance commits. A disconnected
viewer can recover its actor-scoped receipt. A previous host's unfinished effect
becomes uncertain and is never replayed automatically. Browser diagnostic events
are bounded authenticated direct telemetry, with explicit development opt-in;
they do not create command receipts.

Session-only provider credentials remain in the host CredentialStore. Execution
workers resolve them over signed private RPC using the exact current project
grant and the enabled provider's current saved reference. Secrets are absent from
SQL, worker environment snapshots and response receipts. Every provider operation
resolves the current secret, so deletion applies to retained workers.

Host pressure is observed over the same exact-grant private channel, with a cache
bounded to one second per grant. Saved thresholds affect new provider, harness
and mission starts in participating workers. CPU policy remains saveable; its
status reports an inactive executor and unavailable activation because no
supported memory-managed PDF executor is supplied. No affinity-only pool is
started by Settings.

Focused backend evidence:

- Provider/credential slice: 37 distinct cases passed across an initial 25 passes
  and 12 independently reviewed rechecks. SQLite and disposable PostgreSQL covered
  replay, actor isolation, crash uncertainty, shutdown fencing, SQL concurrency,
  and an actual retained Kernel's first chat with a synthetic saved credential.
- Host observation slice: 18 distinct cases passed across an initial 15 passes
  and three reviewed fixture rechecks. They cover CPU/pressure save/readback,
  signed worker pressure/cache/fencing, Butler file and interval revisions,
  diagnostic settings/files/errors/direct events, synthetic image preparation and
  invalidation, canonical integration reads, retained first chat, and existing
  embedded diagnostics/terminal cache compatibility.
- Integration write slice: 16 focused host ASGI cases passed on SQLite and
  PostgreSQL. VPN import/delete validation, reference/use/revision blockers,
  TypeSafe save/test/replace/delete, lost responses and encrypted replay, and
  synthetic Web Search lifecycle/configuration all pass. Existing public
  contracts are retained; no new VPN update endpoint is introduced.
- Integration credential slice: 22 distinct focused cases passed across 16
  initial passes and six independently reviewed fixture rechecks. TypeSafe and
  VPN session references resolve through a signed private exact-grant channel.
  TypeSafe use requires the project's current opt-in and privacy policy. VPN
  preselection exposes availability only; secret use requires that project's
  committed selected VPN and enabled Docker networking. Existing environment
  fallback remains unchanged. Real retained Kernel consumers used both saved
  integrations, then observed TypeSafe revocation and local fallback. Per-action
  Search capability tests cover supported initialization versus unavailable
  provisioning/reconfiguration without performing OCI work.
- Ruff passed; targeted mypy reported no issues in the 11 observation modules.
- Actual retained API composition requires the independently validated shared
  database ownership change in commit `17ec988e`.

The image preparer, provider and runtime observations were synthetic. No provider
network, real credential, container image build, live installation or deployment
was used. Browser/device, production bundle, non-loopback LAN, installed desktop,
and final Settings-assistant/product-index acceptance belong to the integrated
candidate and are not established by this backend receipt. Web Search capability
is read from its current managed adapter per action. The legacy direct OCI
initializer is never treated as a supported memory-managed adapter. The managed
Search adapter/binding and verified daemon image-build enforcement have their
own runtime validation requirements.

The credential slice's seven-module focused mypy check passed. Including the
existing terminal module also reports the preexisting
`_TerminalReservation.engagement_id` attribute error outside the changed VPN
method; it remains with the terminal owner. Ruff passed. The repository's fresh
causal gate reviewed invalid inert image fixture input and discarded async test
helper calls before those fixture repairs; product validation was preserved.
