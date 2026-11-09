# Durable provider-chat tool context

Provider-backed conversations retain exact tool knowledge in Core. Native
harness adapters, catalogs, context delivery, compaction and loaded-tool tracking
keep their existing behavior. Jev remains an optional project opt-in.

The operator continues through Assistant: select a provider and its existing
tool/source controls, send a request, load or use a deferred tool, continue,
refresh/reconnect, compact, restart Core, retry or fork. Core's database owns the
manifest, transcript, turn snapshots and lineage. The provider owns only its
request/response protocol. No browser state or provider cache owns tool knowledge.

| Journey step | Observable invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover/select | Existing built-ins, pinned tools, selected sources and deferred tools retain their initial selection | Current project scope and runtime catalog | Focused provider preparation |
| Load/use | Successful loads introduce the complete definition once; suggestions and failed loads do not | Revisioned session manifest and successful durable load records | Unit and real Core |
| Stream/interrupt | Calls retain current scope, approval, argument and budget checks | Existing broker and turn execution claim | Focused routing and approval |
| Continue/compact | Exact parameter names, required fields and constraints survive all context reduction | Manifest outside transcript history | Focused ordinary/background/mid-turn recovery |
| Refresh/reconnect/restart | The same session reconstructs its known definitions independently of Jev | Core manifest revisions and request snapshot | Restart and authenticated Core journey |
| Retry/switch | Each request identifies its manifest revision; paused execution keeps existing revision guards | Core turn snapshot and current scope | Retry, approval and provider/model switch |
| Fork | Only knowledge available at the selected message boundary is inherited, within allowed scope | Message revision reference and immutable manifest history | Historical fork regression |
| Disable/revoke | Revoked definitions leave active context and cannot execute; historical calls remain | Current scope/catalog plus immutable history | Reconcile and execution regression |
| Delete | Session/project removal cleans up its manifests | Existing Core retention transaction | Storage regression |

`provider_tool_manifests` is an additive table keyed by session and revision. Its
non-null project identity participates in execution-process SQL fencing. A
revision records stable runtime name, source, exact model-facing description and
input schema, complete-definition digest, representation, presence and introduction
reason. The search-index fingerprint is deliberately separate: it does not cover
every schema constraint. Schema family 5 remains readable by older workers.

The canonical representation is deterministic. Native tool declarations contain
their definitions once; the managed tools section contains loaded deferred
definitions once, plus a schema-free directory for absent definitions. Catalog
load replay preserves the issued call and its result but refers to the managed
definition, so replay does not introduce another authoritative schema copy.
Suggestions name absent candidates and never establish presence.

Tool manifests are not summarizer input. Ordinary, background and mid-turn
compaction, tool-result clearing and context-length recovery reconstruct the
managed section from durable state. Capacity estimates include it, and retained
definitions are never silently truncated or evicted to make a request fit.

Ranking candidates are eligible deferred tools whose current complete-definition
digest is absent. The same exclusion applies to Jev and local ranking; empty
candidates skip ranking. Jev cache identity includes complete-definition digests
without sending schemas or tool output to Jev. Existing privacy and local fallback
rules apply. Explicit catalog search and load remain available.

“Once” means one authoritative representation in an assembled context. Providers
may still require native declarations on every network request. Prompt-cache
eligibility and token/context size are separate measurements; synthetic fixtures
do not prove real cache savings or live-provider behavior.

Baseline at `48077146`: the inert preloaded fixture's native declarations cost
2,381 estimated tokens and routing instructions 1,389. After an explicit load,
the same schema also appeared in replayed tool output (3,933 estimated total input
tokens). Without a repeated preload, the later turn and compacted turn had only a
directory and no retained complete schema, despite a durable successful load.
Sanitized capture and raw validation evidence are recorded in the task receipt;
these values describe the fixed synthetic fixture, not provider billing.

Acceptance is incomplete until the focused regression selection, independent
causal validation, isolated authenticated Core journey, product-knowledge source
digest synchronization and applicable production-browser checks have receipts.
