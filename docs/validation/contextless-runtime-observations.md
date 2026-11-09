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

Work invalidation now uses a shared per-API `SqlWorkObserver`, not the local
feed. It hashes bounded streamed SQL projections, including Work/project
revision tuples, saved session titles/roster flags and covering active-turn
columns. Token/checkpoint revisions and historical turns do not trigger noisy
refreshes. Separate cursors support global or explicit-project readers and
remain derived from durable state after API replacement. A complete baseline
precedes `ready`; project changes take precedence when notifications coalesce.
Each API admits at most 64 subscribers, fetches 128 rows at a time through bulk
read admission, scans at most once every two seconds after completion, and
limits each scan to five seconds. Failure/cancellation shares the cooldown;
partial scans cannot advance the cursor. There is no polling without viewers,
and no SQL session or scan lock remains held while sending a frame. SSE retains
`ready`, `change` with `work`/`projects`, 25-second keep-alive and `no-store`.
Its new `id` is a current-state fingerprint, not historical event replay.

Host Settings now owns `GET /runtime-usage`, including `refresh=true`, using a
shared canonical `RuntimeUsageService` and credential store. Reads serialize
within the existing bounded host read admission; the normal 120-second vendor
cache and explicit refresh remain. Credential-store replacement reconstructs
the service rather than keeping a stale resolver. No profiles are seeded, and
local providers keep honest unavailable vendor balances while showing recorded
SQL token usage. An inert `settings.usage_transport` supplies account fixtures.

`POST /harnesses/{id}/test-turn` now enters existing authenticated host-effect
delivery, with the same immutable key, actor-owned receipt, pending response
and lost-viewer replay behavior as other host effects. Its canonical diagnostic
body was extracted from `HarnessRuntimeService.test_turn`; the legacy method
delegates to that helper. The host constructs no project runtime service. It
passes the same host credential store and memory controller, requires the
adapter's protected opener, disables tools/MCP, bounds opens/resume to 15
seconds, close to five seconds, the fixed turn to 90 seconds and its reply to
4 KiB. The whole helper remains below the host effect's timeout. Unsupported
native adapters create a host-owned blocked memory record (`project_id=None`)
without an unprotected launch or durable diagnostic project/session. Test
fixtures may use `settings.harness_factory` with an inert protected adapter.

Work's process-local feed cannot observe
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
and saved active-agent filtering. The observation slice passed 24 cases across
those two databases: ten SQL read regressions and fourteen stream/observer
cases. These include a distinct producer process with uncommitted/commit
visibility, same-count edits, deletion, project precedence, scoped signals,
checkpoint-noise suppression, subscriber/cooldown bounds and reconstruction of
the API app against existing SQL. No live provider, CLI, browser or deployment
acceptance is claimed. Root integration owns the existing Work/Settings UI
journey and the final product-guidance update.

The host slice collected 22 distinct cases across SQLite and PostgreSQL. Eight
passed in the initial selection; constructor, empty-capability and inherited
project-inventory fixture faults were independently reviewed before correction.
Failure-only retries passed two configuration guards, eight diagnostic cases,
and the final four unchanged-inventory cases. No product behavior was changed
for those fixture repairs. The tests cover canonical credentials, account-cache
refresh and recorded tokens, paired-device revocation, protected diagnostic
turn/replay and delayed receipts, memory refusal without a project owner, and
tool/reply-size refusal. Ruff passed and mypy passed on four changed source
files. Raw logs, XML, selection receipts and the combined case inventory remain
under `/tmp/nebula-host-*`. These inert fixtures do not establish live vendor
compatibility, native memory enforcement or the production LAN/browser journey.
