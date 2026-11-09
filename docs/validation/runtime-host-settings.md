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
- Ruff passed; targeted mypy reported no issues in the 11 observation modules.
- Actual retained API composition requires the independently validated shared
  database ownership change in commit `17ec988e`.

The image preparer, provider and runtime observations were synthetic. No provider
network, real credential, container image build, live installation or deployment
was used. Browser/device, production bundle, non-loopback LAN, installed desktop,
and final Settings-assistant/product-index acceptance belong to the integrated
candidate and are not established by this backend receipt. Session-only
integration credentials still need exact-grant resolver delivery to their project
consumers. Web Search initialization reports its unsupported memory-enforcement
capability explicitly until a memory-managed persistent adapter is supplied;
the legacy direct OCI initializer is not enabled by this write slice. Verified
daemon image-build enforcement is a separate runner capability requirement.
