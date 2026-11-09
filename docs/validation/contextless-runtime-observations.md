# Contextless runtime observations

The Work overview and runtime Settings can read global state without an
execution owner or a synthetic global project. The first slice mounts static
harness descriptors and SQL Work items, updates and saved active-agent status
on `RuntimeGlobalApi` through `mount_global_work(app, store, actor)`.

Public authentication remains at the API adapter, including paired-device
revocation. Work items and updates keep their existing array response, newest
ordering and bounded offset/limit parameters. Every explicit `engagement_id`
and `project_id`, including repeated parameters, must agree and name a real
project; those values constrain SQL reads. A `source_session_id` by itself is
only an exact update-source filter, so a parent conversation can still find
its attributed updates in a child project. These reads return `no-store` and
never create ownership, accepted commands or execution services.

The independent causal review reproduced the missing routes through actual
first-hop/API routing and identified the erroneous sentinel-owner fallback.
Work writes remain project-fenced. The separate batch-write ownership issue
is outside this slice.

Remaining slices must add cross-process Work invalidation and host-owned
runtime usage/diagnostic effects. Work's process-local feed cannot observe
retained execution workers, and the buffered host JSON proxy cannot carry an
indefinite event stream. Shared SQL observations must capture their baseline
before `ready`, bound scan memory/time/concurrency, publish only complete
snapshots and release SQL connections before yielding to clients. Runtime
usage and diagnostic harness turns must use the canonical host credentials,
observations, memory policy and accepted host-effect receipts.

Focused local development receipts are recorded under
`/tmp/nebula-global-work-*`. The first SQL slice collected and passed 10 public
API cases across SQLite and PostgreSQL, including ordinary and paired-device
authentication/revocation, project-query conflicts, pagination, source attribution
and saved active-agent filtering. No live provider, CLI, browser or deployment
acceptance is claimed. Root integration owns the existing Work/Settings UI
journey and the final product-guidance update.
