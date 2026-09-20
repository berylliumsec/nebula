# OpenRouter provider and Nebula harness improvements

Status: user approved implementation on 2026-09-18; provider discovery increment in progress.
Branch: `codex/openrouter-native-harness` (existing working branch name).

## Implementation boundary and first increment

This increment covers account-filtered OpenRouter discovery and model metadata in
existing selectors. Autonomous offensive goals, delegated attack execution and
offensive skill orchestration are excluded from this implementation. The broader
proposal below is not a claim that those features are implemented.

| Journey | Invariant and authority | Verification |
| --- | --- | --- |
| Save provider, refresh catalog | Core owns credentials; OpenRouter owns account availability; only allowlisted IDs and metadata reach the browser | adapter and API unit tests |
| Choose default/chat model | Existing saved IDs remain compatible; labels use advertised names and context limits, never imply verified inference | frontend unit checks and production build |
| Failed refresh/retry | Preserve previous browser snapshot, mark provider unavailable; never fall back to the public catalog | adapter failure tests; browser acceptance still required |
| Reload/reconnect | Existing Core profile retains selected ID; catalog is rediscovered | real-Core acceptance required |
| Disable/delete | Existing provider lifecycle remains authoritative; no new credentials or durable catalog store | existing behavior; real-Core acceptance required |
| Stream/interrupt/background/fork | No execution changes in this increment | not applicable to catalog-only change |

Required UI acceptance remains desktop/mobile Chromium and mobile WebKit,
production bundle and real Core. Live OpenRouter inference and physical-device
checks must be reported separately if unavailable. No full-suite run is authorized.

## Corrected scope

Add first-class OpenRouter provider support and improve Nebula’s existing harness
so goals, skills, planning, tools, approvals, persistence and advanced session
controls offer a comparable experience to Codex and Claude. This is an improvement
to Nebula, not a separately configured or branded “Nebula native” product.

The previous proposal incorrectly emphasized a separate runtime setup and showed
Sessions as a primary navigation item. This revision follows the actual product:
Settings → Advanced → Models, and Workbench → Chat with Assistant settings and
session details. It supersedes the earlier separate-runtime proposal.

OpenRouter already has a provider flavor and default endpoint in the source. The
work is completing its end-to-end integration and making the existing harness
capabilities available consistently with provider-backed models.

## Mockup review

Figma: https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7

- [Settings: OpenRouter provider](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=4-69)
- [Workbench Chat: harness controls](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=4-73)
- [Session details: goals, skills and agents](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=4-77)
- [Mobile Chat, 320 px](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=7-113)

The Desktop page also shows the existing Add model provider dialog with OpenRouter
selected and the Assistant settings panel with provider/model and harness controls.
Action-state frames cover goal setup, model/skill selection, Plan/Execute, questions,
approvals, budget exhaustion, provider failure, restart recovery and restore conflicts.
These are editable static mockups, not application screenshots or a functional prototype.
Models, usage and catalog metadata shown are illustrative.

## Changes visible in Nebula

1. **Settings → Models:** OpenRouter appears in Add provider, using the existing
   credential store, connection verification, discovered model list and provider card.
   No additional harness installation/setup is introduced for the operator.
2. **Workbench → Chat → Assistant settings:** select OpenRouter and a discovered
   model. Expose model-supported controls and explain unavailable capabilities.
3. **Existing composer:** compact Goal, Skills and Plan/Execute controls; skill
   autocomplete remains available through `$`. Avoid a competing top-level workspace.
4. **Conversation:** compact current-goal progress and usage, stop/resume and steering;
   approvals, questions and failures remain visible outside collapsed activity.
5. **Existing session details:** plan, budgets, skill provenance, context, delegated
   agents, checkpoints, forks and advanced hooks/scheduling.

## OpenRouter discovery and capability integration

Mapped against official documentation on 2026-09-18. This is a planned contract,
not implemented or live-account-verified behavior. Existing
`OpenAICompatibleProvider.health()` reads `/v1/models` and retains only `data[].id`;
`ProviderHealth.models` is a string list. That loses metadata needed by the picker.

### Discovery sequence

Core performs discovery with the stored credential; the browser never calls
OpenRouter with an API key. The base URL is `https://openrouter.ai/api/v1`.

| OpenRouter endpoint | Planned use in Nebula |
| --- | --- |
| `GET /key` | Verify the supplied key; normalize remaining key limit, usage and expiration when present. A successful key check does not prove inference works. |
| `GET /models/user` | Primary selectable catalog, filtered by the account's provider preferences, privacy settings and guardrails. Intersect with Nebula's configured model allowlist and session policy. |
| `GET /models` | Optional broader catalog/metadata lookup. Never silently substitute it for account-filtered availability after a `/models/user` failure. |
| `GET /models/{author}/{slug}/endpoints` | On-demand hosting-endpoint details for a selected model: supported parameters, context/output limits, pricing and reported availability. |

Sources: [key metadata](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key),
[account-filtered models](https://openrouter.ai/docs/api/api-reference/models/list-models-filtered-by-user-provider-preferences-privacy-settings-and-guardrails),
[model catalog](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties),
[model endpoints](https://openrouter.ai/docs/api/api-reference/endpoints/list-all-endpoints-for-a-model).

### Field mapping

| OpenRouter field | Nebula use |
| --- | --- |
| `id`, `name`, `description`, `canonical_slug` | Exact request ID, human-readable picker label/search text, description and identity metadata. Preserve the request ID including variants. |
| `architecture.input_modalities`, `output_modalities` | Attachment/output indicators. Show only modalities supported by both the model and Nebula's implemented adapter/UI. |
| `supported_parameters` | Advertised parameter set; gate tools, structured output and reasoning controls individually. Missing metadata means unknown, not supported. |
| `context_length`, `top_provider.max_completion_tokens` | Catalog context/output hints; validate against compatible endpoint limits before execution. |
| `pricing` | Decimal monetary values with explicit units. Display token prices per million; preserve other billing units separately. Missing price is unknown, not free. |
| `expiration_date` | Warn about retirement and preserve the saved model ID in historical sessions. |
| `alias_target.slug` | Exact model an alias (`~author/family-latest`) redirects to. Aliases publish no endpoints of their own, so endpoint discovery and context sizing measure the target; recorded limits lapse when the target moves. |
| Endpoint `provider_name`, `tag`, `supported_parameters`, limits and pricing | Advanced hosting preference and route compatibility; distinct from model publisher and Nebula's OpenRouter connection. |

The source schemas are the catalog and endpoint documentation linked above. Catalog
metadata is advertised capability, not a verified turn. A model appearing in the
account-filtered list is not a guarantee of sufficient credit or current capacity.

### Routing, controls and boundaries

- Nebula **Provider** means the saved OpenRouter connection. A model publisher
  (for example, Anthropic) and the provider hosting that model are separate concepts.
  Default hosting choice is **Automatic**; advanced hosting preferences use discovered
  route identifiers, never user-entered opaque strings.
- For harness requests, send `provider.require_parameters=true` and only parameters
  required or explicitly selected. Preserve account and session privacy restrictions.
  Hosting fallback may stay enabled within those constraints; automatic fallback to
  a different model is off. No compatible endpoint produces an actionable error.
- Parameter presence does not establish every valid value. Discovering `tools` does
  not prove strict schemas or parallel tool use. Reasoning effort values and token
  budgets require a documented model/endpoint mapping; otherwise show Default only.
- Preserve provider-specific reasoning payloads required for subsequent turns at
  the adapter boundary; do not expose raw internal reasoning as a UI requirement.
- Goals, skills, checkpoints and delegation are Nebula harness features. They are
  not OpenRouter-discovered capabilities. Model compatibility gates execution.

Sources: [routing and required parameters](https://openrouter.ai/docs/guides/routing/provider-selection),
[reasoning controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

### Refresh, failure and selection behavior

Proposed defaults: discover after Save/Verify, on explicit Refresh models, and when
opening a picker whose snapshot is older than 15 minutes. Cache by provider profile,
credential revision and policy revision; invalidate on credential or policy changes.
Coalesce simultaneous refreshes and retain the last successful snapshot with a visible
stale timestamp when a refresh fails. Never erase a chosen model on transient failure.

The documented `/models/user` contract currently returns one list and exposes no
pagination parameters, so discovery must not invent unsupported page controls.
Publish a replacement snapshot only after the complete response validates. Bound
the operation to 30 seconds total with 10-second individual request timeouts and a
10,000-model parsing ceiling. If OpenRouter adds a documented cursor or offset,
adopt it atomically before treating paged results as authoritative. Retry behavior
and lazy endpoint discovery remain outstanding.

Keep a removed/restricted saved model visible as unavailable with a Choose model
action. Distinguish invalid key, empty filtered catalog, unavailable endpoint,
rate limit and stale metadata. Never silently widen the allowed catalog or route.

Provider/model changes apply to the next turn, preserving conversation and goal
state. Interrupt an active turn first; revalidate context, attachments, tool schemas,
permissions and model-specific settings before switching. Do not forward incompatible
opaque reasoning state to another model. Obtain explicit confirmation for any
required compaction or loss of unsupported attachments/settings.

### Internal interface changes and acceptance

Add normalized per-model descriptors alongside the existing model-ID list to preserve
API compatibility: identity, modalities, advertised parameter set, nullable limits,
unit-aware pricing, expiration, discovery timestamp/source and verification status.
Keep endpoint descriptors separate and lazy. Distinguish credential verification,
catalog discovery and successful inference; do not reuse a generic healthy flag for all.

Focused contract cases: multi-page discovery; account-filtered vs global catalog;
model variants; unknown/missing fields; decimal prices; endpoint/model capability
disagreement; unsupported reasoning controls; no compatible route; credential
rotation; 401/403/429/5xx/timeouts; stale snapshot preservation; removed models; and
switching while active or with incompatible context. Browser acceptance must show
Save → discovered searchable models → selection → successful bounded turn → refresh
and reconnect with selection retained. Discovery does not run a paid prompt against
every model; live inference checks are separate, bounded acceptance steps.

## Gap checklist: reuse, extend and prove

Baseline is source inspection, not fresh runtime verification. Existing code is a
foundation to reuse, not evidence that the OpenRouter journey already works.
All unchecked items below are required work; none is satisfied by the mockup alone.

| ID | Existing foundation | Missing deliverable | Acceptance evidence |
| --- | --- | --- | --- |
| G1 | OpenRouter catalog entry; generic compatible requests and streaming | Account-filtered discovery, model descriptors, endpoint compatibility and constrained routing described above | Contract cases plus bounded live model/tool turns through OpenRouter |
| G2 | Provider/model selectors and saved defaults | Searchable models with publisher, capabilities, limits and pricing; advanced compatible controls; safe next-turn switching | Save → discover → select → use → switch → refresh/reconnect with selection retained |
| G3 | Provider-backed model/tool loop, approvals, tool history and call limits | Adapt the existing loop to model-specific schemas/parameters, goal/skill context and OpenRouter continuation payloads | Multi-step benign workspace task; malformed/partial tool calls; denied approvals; cancellation; no duplicate tool effects |
| G4 | External-harness `/goal` handling; mission planning and budgets | Nebula-owned persistent goals in provider-backed chat, independent of vendor goal RPCs | Create/start/pause/resume/cancel, blocked/completed states, visible progress and restart recovery |
| G5 | Harness skill discovery, invocation interfaces and autocomplete | Reuse discovery for provider-backed chat; load selected skill instructions/resources and preserve provenance | Browse project/installed skills → invoke through OpenRouter → reload; missing skill and changed skill recovery |
| G6 | Durable conversations/turns, usage and context compaction | Preserve goals, selected skills, instructions, permissions and pending decisions through compaction and compatible model changes | Long conversation and model-switch tests retain authoritative state; unsupported context changes are explained |
| G7 | Background provider-turn tasks and reconnect mechanisms | Durable goal execution ownership, restart-paused recovery and uncertain-action reconciliation | Disconnect/reconnect and restart at request/tool/receipt boundaries do not duplicate work or resume silently |
| G8 | Mission delegation infrastructure and external-harness agent controls | Provider-backed workspace delegation with inherited permissions, shared budgets, bounded concurrency and visible child controls | Child start/status/stop, parent cancellation and aggregate limits behave consistently |
| G9 | Existing harness capability flags and vendor-specific controls | Explicit lifecycle hooks, conflict-aware checkpoints and conversation forks for provider-backed workspace sessions | Hook timeout/failure, later user edits and independent fork lineage covered |
| G10 | Existing automation infrastructure | Scheduled workspace tasks using the same saved provider, goal, workspace and permission contract | Discover/create/disable schedule; run receipt; unavailable credential/provider recovery; no overlapping duplicate invocation |
| G11 | Existing Settings, Workbench Chat, activity and session details | Wire all new controls and states into these surfaces, including mobile and accessible interaction | Approved designs implemented; production real-Core browser journey and required device/origin matrix |
| G12 | Existing feature tests and capability reporting | Focused regression selection, compatibility regression evidence and an honest launch matrix | Diff-bound test selection, reviewed receipts and per-capability evidence; unverified features remain labeled |

G3 implementation checkpoint (2026-09-18): the normalized provider contract now
rejects partial JSON arguments and missing/invalid tool-call identities before they
enter the Core loop. Routing rejects prose mixed with calls, parallel calls,
unavailable capabilities and repeated provider call IDs. Denied operations are
persisted as bounded error results and supplied to final synthesis without retrying
the effect; cancellation stops the Core-owned in-flight worker and clears ownership.
Focused service tests prove malformed calls never reach the broker and a repeated
call ID cannot execute twice. A real multi-step OpenRouter workspace journey,
approval denial through the visible UI and live cancellation evidence remain open,
so G3 is not complete.

### Provider-backed goals: concrete behavior

Implementation checkpoint (2026-09-18): Core now has a provider-chat `ChatGoal`
record with one authoritative goal per conversation, explicit draft/start/pause/
resume/block/cancel/complete transitions, optimistic revisions, token/time/step/child
limits, cumulative usage fields, linked-turn capacity, stall state and mandatory
completion summary/evidence. Goal-linked provider turns now require running state,
inject the authoritative objective/criteria/plan, atomically advance and link the
turn, reject exhausted step/token budgets before dispatch, and reconcile provider
usage afterward. Workbench Chat now reloads the Core goal, exposes draft creation,
optional token/time/step limits and explicit Start/Pause/Resume/Cancel controls, and
attaches only a running goal to the next provider request. Restart-paused multi-turn
execution remains open. Active elapsed time now accumulates only while running;
dispatch pauses before an expired time/step/token limit, and child capacity is
revision-safe and cumulative for later delegation. Workbench Chat exposes child
limits, active elapsed time, explicit Block with a reason, and Complete with a
required summary and structured operator-confirmed evidence. Autonomous continuation,
actual child delegation, full budget reservation/reconciliation at every provider
boundary, richer session details and real-Core browser acceptance remain open.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Create | Saved goal remains a draft until explicit Start | Core `chat_goals` entity | service + real Core pending |
| Mutate | Revision-safe lifecycle rejects stale or invalid transitions | Core entity revision/status | service |
| Complete | Completed status requires summary and structured evidence | Core goal record | service |
| Delete | Deleting its conversation removes the private goal record | Core transaction | storage/service |
| Dispatch/reconnect/UI | Goal drives bounded turns and visibly survives refresh | Core goal + turns + UI | outstanding |

- [ ] **G4:** Persist objective, completion criteria, plan/current step, status,
  limits, cumulative usage, linked session/turns and completion evidence in Core.
  Add storage only where existing records cannot represent these facts. Goal state
  is independent of a particular model and is never reconstructed only from prose.
- [ ] Require explicit Start. Pause prevents new model/tool dispatch and acknowledges
  any action already in flight; cancel propagates to children and terminates local
  work where supported. Neither operation claims to undo completed side effects.
- [ ] Extend the existing tool loop rather than write a competing executor. At
  safe boundaries, persist progress and check permissions, pending decisions,
  cancellation and remaining limits before dispatching further work.
- [ ] Enforce token, time, step and child limits across the whole goal. Reserve
  budget for in-flight calls/children and reconcile actual usage afterward. If
  token bounds cannot be established, pause rather than advertise a hard limit.
  Cost remains an estimate unless the adapter supports reliable enforcement.
- [ ] Require a summary and evidence against the goal's completion criteria before
  marking completed; distinguish verified checks, model assertions and operator
  confirmation. Repeated unchanged failures stop as blocked instead of continuing
  indefinitely. Default stall threshold: three consecutive attempts at the same
  step with no new result; show the reason and a user-controlled Resume action.

### Provider-backed skills and API context

Implementation contract recorded before the G5 slice (2026-09-18): the operator
enters through the existing chat composer and `$` autocomplete. Core's bounded
catalog owns discovery; an exact name/path pair owns selection; Core stores an
immutable source/path/digest/instructions snapshot on the provider turn and, when
a goal is active, on that goal. React owns only the open menu and pending selection.
Discovery, selection, provider use, refresh/reconnect, changed or missing files,
duplicate names, and explicit removal are required lifecycle states. Skill text is
subordinate to policy and cannot alter workspace permissions, tools, approvals, or
dependency state. Component/API tests cover discovery and failures; real-Core
Playwright must prove selection, use, reload, mobile geometry and recovery before
G5 is complete.

Native provider workspace contract: shared, provider-neutral skills live in the
selected project's generic agent tree. OpenRouter and every other non-harness model
read that shared root. External harnesses retain their existing Codex/Grok discovery
rules, and also include the generic root. `.agents` is the shared convention for
every agent runtime; harness-specific roots are additional compatibility surfaces,
not replacements for it. Nebula must not create or consult a parallel `.nebula`
configuration tree for skills, hooks, checkpoints, or future agent capabilities.

```text
<project>/.agents/
  skills/<skill-name>/SKILL.md   # shared skill entry points
  hooks/                         # reserved for Core-owned native hook definitions
  checkpoints/                   # reserved for native checkpoint metadata
```

Native provider discovery must not fall back to `.codex`, `.grok`, a generic
`skills/` directory, or `.nebula`. The `.agents` hook and checkpoint directories
establish shared workspace ownership, but external harness adapters do not execute
them implicitly and they are not treated as implemented native behavior until their
later delivery gates.

G5 implementation checkpoint (2026-09-18): native sessions now discover bounded
project and managed `.agents/skills` catalogs, expose both roots in Settings, select
an exact source/path pair through the existing `$` picker, and persist immutable
instruction and referenced-resource digests on the turn and active goal. Referenced
resources are listed from local Markdown links and read only on demand through a
bounded, read-only, digest-checked tool; paths, symlinks, mutations and oversized
content fail closed. Codex/Grok retain their compatibility roots while also seeing
the generic `.agents` root. Focused evidence is 42 Python tests, 81 frontend tests,
the production UI build, four catalogued mocked-Core Playwright journeys across
desktop and mobile engines, and production-bundle real-Core journeys on a paired
non-loopback LAN origin at desktop plus 390px Chromium and WebKit viewports. Goal
skill sets can now be explicitly replaced at a
safe revision boundary: Core reuses unchanged immutable snapshots, snapshots only
new exact selections, permits explicit removal, and rejects edits during an active
worker claim. The UI exposes source/path choices and retained snapshots whose source
has disappeared. The real-Core journey proves project discovery, visible selection,
goal attachment, reload, source-loss recovery and use of the retained instructions
in the next provider request. Automated G5 behavior is covered; physical-device
acceptance evidence remains required before the product-level gate is complete.

- [ ] **G5:** Reuse the skill discovery service behind provider-neutral interfaces;
  installed skills must not require installing/logging into Codex or Grok. Extend
  Nebula's Library/settings interaction to browse and select a managed skill root.
  Discover native project skills only from `<project>/.agents/skills`; Codex and
  Grok additionally retain their harness-specific compatibility roots. Do not add a
  `.nebula` agent-configuration tree.
- [ ] An explicit picker or `$skill` invocation loads the selected `SKILL.md` and
  records its source, resolved path and content digest. Duplicate skill names show
  their source for selection. Missing/unreadable skills produce an in-place error;
  never silently substitute another same-named skill.
- [ ] Load referenced resources progressively within workspace permissions. Keep
  skill instructions below operator/system policy. Scripts run through the existing
  sandbox/tool/approval path; skill content cannot install dependencies, obtain
  secrets or expand permissions merely by requesting it.
- [ ] For a normal chat turn, the explicit skill applies to that turn's tool loop.
  For an explicitly started goal, skills selected for that goal remain attached
  until completion or an explicit edit. Snapshot selected instructions for continuity;
  do not silently replace them when files change during active work.
- [ ] Assemble each OpenRouter request from the selected model, bounded conversation,
  current goal/plan, applicable skill instructions and permitted tool definitions.
  Return model tool-call records and matching tool results on continuation. There
  is no special OpenRouter goal/skill endpoint in this integration.
- [ ] **G6:** Compaction preserves references to the authoritative goal and skill
  snapshots, permissions, unresolved approvals/questions and tool receipts. Preserve
  required compatible reasoning payloads at the adapter boundary; never treat a
  summary as permission to discard unresolved decisions or repeat completed tools.

### G6 expansion: model-aware context sizing and compaction

**Baseline at design approval (source inspection):** `context.py` resolved limits from
provider-level `metadata.options.context_window` and `max_output_tokens`, not the
selected model. Defaults are 8,192 context tokens and 2,048 reserved output tokens.
The input target is 75% of the remaining capacity: 4,608 tokens at default settings.
`chat.py` compacted older history when its estimate exceeded that target, retaining a
recent tail, sourced memory and selected original excerpts. Canonical messages remain
stored. Estimation uses UTF-8 byte length divided by three plus message overhead,
not a selected-model tokenizer. Hierarchical compaction uses 60% of input capacity
per source segment and caps each summary response at 2,048 tokens. The chat request
type separately caps requested output at 32,768 tokens; this is not the context window.
External harness sessions are marked runtime-managed, but the context API currently
returns an 8,192-token placeholder rather than authoritative runtime capacity.

Implementation checkpoint (2026-09-18): refreshed exact-model descriptors are now
persisted with a content revision. Chat preparation, continuation hard checks,
compaction, chat context status and mission context status use one exact-model
resolver. Explicit provider values are lower ceilings; unknown models are labeled
configured/unverified or fallback/estimated in the context UI. The 75% trigger and
60% post-compaction target are derived from effective input capacity, summary output
defaults to 5% of the post-compaction target, and image bytes are not counted as text
tokens. OpenRouter capability verification now refreshes the exact model's endpoint
catalog, persists its revision, and sizes every request from the minimum context,
input and output ceilings across active routes that support the request's required
parameters. Missing or invalid endpoint metadata fails closed to Nebula's labeled
8,192-token safe ceiling; the session details distinguish that estimate from verified
route capacity. Alias models are measured through the catalog's `alias_target`,
because the alias itself returns an empty endpoint set; the descriptor records which
slug was measured, and limits revert to the safe ceiling once the alias redirects
somewhere those routes never described. Remaining G6 work includes tokenizer integrations, route/model
downgrade UX and operator-facing compaction-budget settings. Active goals now reserve their remaining
token budget before a compactor call and charge every successful or failed summary
attempt to cumulative goal usage. A durable pending approval/recovery turn rejects a
new user turn before retrieval or compaction, so its approval and tool receipts remain
authoritative outside lossy memory. Before every initial, routing and final-synthesis
provider call, Core now reserves estimated complete input plus bounded output inside
the goal's remaining tokens. Non-stream goal turns are durable and charged like stream
turns. If routing consumes the final budget, Core pauses the goal before dispatching
the proposed tool effect. Provider-backed conversations now preflight provider/model
changes against the durable session revision, exact metadata revision, tool
compatibility, privacy policy and target input budget. Switches that would compact
active context require an explicit confirmation fingerprint; Core revalidates it at
dispatch, and incompatible or stale switches leave the prior runtime authoritative.
Each outbound request records its resolved context limits. This native switching and
compaction path does not apply to Codex or Grok: their harness owns context assembly,
limits, compaction, model changes and continuation state, and Nebula reports unknown
capacity unless the runtime supplies it. Confirmed provider context-length rejections
now use a typed adapter error: before any response delta or tool effect, Core refreshes
exact route/model metadata, reassembles canonical context, persists the revised limits
and compaction provenance, and permits exactly one retry. A second rejection becomes
an actionable capacity failure; authorization, billing and unrelated failures are not
retried, and completed tool effects are never replayed.
Revisioned operator context now includes an explicit unresolved-question kind.
Questions remain canonical Core records until the operator supersedes or removes
them, are snapshotted into each dispatched turn, count as request instructions, and
survive history compaction without allowing model prose or derived memory to resolve
them. Pending approvals and restart reconciliation continue to block a new turn before
retrieval or compaction.

**Required change:** preserve existing compaction/provenance infrastructure and make
its limits follow the exact model and compatible routing endpoints for every request.

- [ ] Resolve a per-request context policy using provider-profile ID, exact model
  ID/variant, eligible endpoints, metadata revision and any explicit operator cap.
  Use the most restrictive known model/route context and input limits. For automatic
  routing, every eligible route must fit the request; constrain routing or use the
  compatible-route minimum. Never size from a largest advertised window while
  allowing fallback to an endpoint that cannot accept it.
- [ ] Treat an explicit operator context cap as a lower ceiling, not an override
  that can exceed known model limits. Preserve existing explicit configuration as
  a cap during migration. Use the 8,192 fallback only when reliable metadata and
  explicit configuration are absent; label it estimated/unknown, not model capacity.
  An operator-supplied unknown-model limit is labeled configured/unverified.
- [ ] Compute `input_capacity = min(context_window - output_reserve,
  endpoint_input_limit)` when a separate input limit exists; otherwise use the
  context-minus-output value. Output reserve is the actual bounded request output
  allocation, constrained by model/endpoint output limits, operator policy and
  remaining goal budget. Account for reasoning tokens according to the adapter's
  token semantics without counting them twice.
- [ ] Default the compaction trigger to 75% of effective input capacity and compact
  toward 60%, providing room before the next compaction. These are proportional
  policies, not fixed token counts. Validate mandatory material against the hard
  capacity; if it alone exceeds the soft target, retain it and report limited
  headroom rather than discard instructions or repeatedly compact the same history.
- [ ] Count the complete outbound request: instructions, goals/plans, selected skill
  text, messages, tool schemas, pending calls/results, retrieval, attachments and
  adapter-required continuation data. Use a supported model tokenizer/count API
  where available; otherwise use a conservative adapter-aware estimate with a
  documented error margin. Never equate multimodal payload byte length with actual
  provider token usage. Check before every continuation and compactor call, not
  only at the start of a user turn.
- [ ] Replace the universal 2,048-token summary ceiling with an adaptive allocation:
  default maximum summary output is 5% of the post-compaction input target, bounded
  by the summarizing model's output capacity and a separate operator-configurable
  compaction cost/token budget. Reserve that allocation before sizing compactor
  source chunks. Do not inherit the normal chat reply's 2,048-token default as an
  accidental summary ceiling. Tiny allocations that cannot represent mandatory
  state fail visibly instead of claiming successful compaction.
- [ ] Keep chunking and hierarchical reduction for large histories, but derive each
  chunk from the compactor's complete prompt budget and selected-model limits.
  Preserve canonical history and source links. Goals, permission snapshots, skill
  references, corrections and unresolved approvals/questions remain authoritative
  outside lossy summaries. Charge all summary calls to cumulative goal usage.
- [ ] Use one resolver for chat preparation, tool-loop continuation, compaction,
  context-status APIs and applicable mission context. Replace the blanket 32,768
  output validation ceiling with model/endpoint-aware validation plus explicit
  operator budget ceilings, retaining positive-value validation and bounded defaults.
- [ ] Re-resolve before a model/provider/route change. A smaller window prompts the
  operator before switch-induced compaction, then reassembles and validates context;
  an incompatible switch leaves the previous model selected. A larger window does
  not automatically resend all archived history. Metadata changes are applied at
  safe request boundaries and recorded with the request's resolved limits.
- [x] For a confirmed context-length rejection with no tool effects dispatched,
  refresh metadata and allow one bounded recompact/retry. Do not repeat tools or
  arbitrarily retry authorization, billing or unrelated failures. Surface an
  actionable capacity error when mandatory input still cannot fit.
- [ ] Expose estimated active input, actual model context, reserved output, trigger,
  metadata source/freshness and last compaction status in existing session details.
  Keep full transcript length distinct from active request context. External Codex/
  Grok runtimes retain ownership of context assembly, context limits, compaction,
  model switching and continuation state; Nebula does not preflight, resize or
  compact their context. Display only capacity/status reported by the harness, or
  “managed by runtime / capacity unknown,” never the 8,192 placeholder as fact.

**Acceptance:** selected tests in `tests/v3/test_context.py` and
`tests/v3/test_api_stream_context.py`, plus affected chat/mission/UI cases, prove
different models on the same provider resolve different limits; 8k/32k/128k/large
synthetic windows scale thresholds; output/reasoning reserve changes input capacity;
automatic-route fallback never overflows smaller endpoints; tool-heavy and multimodal
inputs are accounted for; summary size adapts; compaction retains mandatory state;
model switches and stale/unknown metadata recover correctly; and UI status matches
the exact request budget. Verify canonical history survives compaction/reload and
that cumulative goal-token limits remain independent of per-request context capacity.
Follow the existing focused-test selection process before execution.

### Recovery and advanced workspace controls

- [ ] **G7:** Persist execution ownership and tool intent/receipts. One worker owns
  a goal at a time. Continue explicitly started work while Core runs, independent
  of browser followers. After restart, recover paused; Resume is blocked until any
  tool with an unknown outcome is reconciled. Do not promise exactly-once external
  effects where the underlying tool lacks an idempotency/reconciliation contract.

Implementation checkpoint (2026-09-18): Core now gives each executing provider turn
and linked goal one atomic durable worker claim. A competing worker is rejected;
transcript persistence is revision-fenced with the claim; approval waits release it;
and startup clears abandoned ownership, interrupts the turn, pauses the running goal,
and prevents the stale worker from saving output. Persisted provider call identity
and step metadata distinguish tool intents; a tool left running across restart blocks
Resume until the operator records a completed or failed outcome with a note. That
reconciliation is retained as unverified evidence and the tool is not replayed.
Workbench Chat restores the interrupted state and exposes the reconcile-then-resume
path. Fault injection at every request/tool/receipt boundary and production real-Core
browser acceptance remain open, so G7 is not complete.
- [ ] **G8:** Adapt existing delegation infrastructure to bounded workspace tasks.
  Children inherit or narrow the parent's permissions and consume its shared budget.
  Expose child status and stop controls in existing session details. Parent pause or
  cancellation propagates; concurrent edits use isolated workspaces or explicit
  serialized ownership, not uncontrolled writes to the same files.

G8 implementation checkpoint (2026-09-18): a running provider goal can start a
bounded child conversation/goal that consumes one child slot, inherits skill
snapshots, and is marked isolated rather than sharing parent approvals. Parent
pause and cancel propagate to non-terminal children. Session details list children
and can open them. Real-Core child-stop geometry and live OpenRouter child turns
remain open, so G8 is not complete.

Provider-model exposure (planned): OpenRouter and other OpenAI-compatible models
do not get a vendor subagent API. Core advertises the same function tools already
used by the native tool loop, only when tools are enabled, a goal is running, and
child budget remains. Planned tools: `start_subagent` (objective, completion
criteria, optional narrowed budgets and inherited skills), `list_subagents`, and
`stop_subagent`. Starting a child uses `ChatGoalService.start_child`; the parent
turn may pause as `WAITING_CALLBACK` until the child completes or the operator
opens it. Approvals stay on the parent conversation. The UI is provider-neutral:
Assistant settings “Allow subagents”, a compact transcript count, activity-ledger
rows, and session-details Open/Stop — no OpenRouter-specific chrome.
- [ ] **G9:** Reuse existing hook facilities where applicable; add explicit,
  operator-configured lifecycle hooks with versioned events, timeouts and visible
  outcomes. Approval decisions cannot be bypassed by hooks. Unknown hook side
  effects after restart follow the same reconciliation rule as ordinary tools.
  This is a native-provider/Core facility under `.agents/hooks`; Nebula does not
  replace or manage Codex/Grok context, compaction, checkpoints, goals or native
  hook execution. Harness-owned facilities remain behind their adapter contract.

G9 implementation checkpoint (2026-09-18): Core now discovers provider-native hook
manifests only from `<workspace>/.agents/hooks/*/hook.json`, validates a versioned
event contract, relative in-directory executable, timeout, failure policy and declared
side-effect class, and exposes the bounded catalog through the authenticated API.
Selection freezes manifest and executable digests. The durable runner revalidates
both before launch, sends a versioned JSON event on stdin with a minimal environment,
caps captured output, persists running and terminal outcomes, and treats timeout or
launch failure as data rather than approval. Hook output has no path to approval
state. At this foundation checkpoint, chat lifecycle wiring and recovery were still
open.

G9 lifecycle checkpoint (2026-09-18): provider chat requests can now explicitly
select bounded hook IDs. Core snapshots those exact `.agents` definitions into the
durable provider turn, emits started/completed events once per selected hook, applies
the declared continue/block failure policy, and never interprets hook output as an
approval. Restart marks in-flight hook attempts interrupted; workspace/external
effects block resume until an operator records a revision-safe reconciliation, and
the effect is never replayed. Hooks with declared `none` side effects may retry after
restart. This path is provider-only and does not enter the Codex/Grok harness runtime.
Operator selection and visible execution/reconciliation UI, failed/cancelled lifecycle
events, and production real-Core browser acceptance remain open, so G9 is incomplete.

G9 operator-workflow checkpoint (2026-09-18): provider Assistant settings discover
project `.agents/hooks`, allow explicit per-turn selection, and send exact hook IDs
only on provider requests. Durable outcomes reload with the conversation. Restart
recovery names the interrupted hook, records an operator note, and requires
Confirm completed or Mark failed before Resume. Failed and cancelled lifecycle
events run as best-effort terminal hooks and do not replace the primary provider
error. Hook subprocesses run off the Core event loop so production HTTP turns can
execute them. Hook checkboxes are per-mounted conversation UI state: they survive
later messages while Chat stays mounted and are not a durable project/session
preference, so reload requires an explicit opt-in. Execution summaries page past
the 1,000-row store cap and return the complete turn-scoped list. Focused evidence
includes the named App hook journey, hook restart reconciliation, HTTP execution,
failed/cancelled service tests, production UI build, catalogued mocked Playwright
on desktop and mobile Chromium/WebKit, and production-bundle real-Core LAN
discovery/use/reload/source-loss/restart reconciliation on desktop plus 390px
Chromium and WebKit.

G9 checkpoint/fork increment (2026-09-18): provider conversation forks copy the
parent objective as an independent draft goal with no running state, usage, or
linked turns, and still do not copy pending approvals. Native checkpoints store
bounded relative files under `.agents/checkpoints`, preview unchanged/missing/
conflict, and refuse restore when later edits conflict. Session details expose
Save checkpoint, Preview, and Restore. Physical-device evidence remains open, so
the product-level G9 row is not a launch claim.
- [x] Checkpoints capture only the workspace files under Nebula's edit control.
  Preview a restore and reject conflicts with subsequent user edits. Conversation
  forks create new lineage and independent goal state, disclose shared workspace
  behavior, and never copy pending approvals as authorization for the fork.
- [ ] **G10:** Integrate with the current scheduler rather than introduce another.
  Each occurrence has a durable identity and receipt. Revalidate credentials and
  policy before starting; an unavailable dependency pauses with an actionable error.
  Default to skipping overlapping occurrences while the preceding one is active,
  with a visible skipped-run reason.

G10 implementation checkpoint (2026-09-18): provider conversations can create an
hourly-or-longer Core schedule. Due occurrences revalidate the saved provider,
skip when a turn is still active or the goal is not running, and record a visible
skip reason. Recurring native missions skip a queued occurrence when another run
in the same series is still active. Session details can create, disable, and show
the next run. Live scheduled OpenRouter execution evidence remains open, so G10
is not complete.

### Delivery order and launch gates

1. After design approval, implement G1–G2: provider discovery and selection.
2. Extend G3–G5: the existing execution loop, persistent goals and provider-backed skills.
3. Complete G6–G7: context continuity, model switching and durable recovery.
4. Complete G8–G10: delegated workspace tasks, hooks, checkpoints, forks and scheduling.
5. Apply G11–G12 throughout every slice, not as a final UI/testing retrofit.

Keep additive schemas and existing provider/harness sessions compatible. Add focused
regressions for existing external-harness goals/skills and ordinary provider chat
where shared code changes. Land increments opt-in; do not claim broad parity or
enable unsupported capability flags until the corresponding acceptance row passes.
Any required unavailable live, production, LAN or device gate remains an explicit
verification gap. The user-facing mockup approval boundary is unchanged.

## Implementation direction after design approval

- Finish OpenRouter discovery, supported-parameter validation, streaming/tool-call
  normalization, secret-safe errors and provider verification. Preserve ordinary chat.
- Extend existing provider, chat, orchestration and harness services; reuse their
  durable records, tool policy, approvals, events, workspace ownership and budgets.
  Determine internal adapter requirements from those interfaces without exposing a
  duplicate setup flow. Do not precommit to the previous `nebula_native` kind/schema.
- Complete goals, explicit start/pause/resume/cancel, plans, project/installed skills,
  workspace tools, context compaction, boundary-safe steering and durable recovery.
- Implement bounded delegation, permission inheritance, shared budgets, explicit
  lifecycle hooks, conflict-aware file restore, forks and scheduled workspace work.
- Update relevant catalogs, capabilities, API/frontend types, selectors, defaults,
  error states and mobile controls together. Preserve existing providers and sessions.

The selected broad-parity launch target remains: goals, skills, plans, streaming,
approvals/questions, interruption/steering, resume/replay, workspace tools, compaction,
delegation, hooks, checkpoints, forks and scheduled workspace tasks. All additions
remain planned/unverified until exercised. Model quality is not guaranteed to match
Codex or Claude. Browser/computer-control expansion is outside this scope.

## State and lifecycle contract

| Journey | Invariant | Authority / proof |
| --- | --- | --- |
| Add/select provider | Saved OpenRouter profile and models immediately usable after reload | Core/vault; provider and real-Core browser checks |
| Start goal/use skill | Explicit goal start; skill provenance; no authority expansion | Core goal/plan and workspace skill snapshot; runtime + browser |
| Stream/approve/interrupt | Ordered output; durable decisions; visible stop acknowledgment | Core event/decision/tool ledger; runtime + real-Core |
| Close browser/reconnect | Started work continues while Core runs; reconnect does not duplicate it | Core execution ownership; real-Core lifecycle |
| Restart | Recover saved state paused; Resume required; uncertain effects reconciled | Durable receipts and ownership; restart fault injection |
| Delegate/compact | Shared budgets and inherited permissions; important context retained | Core state; runtime tests |
| Fork/restore | Explicit lineage; user edits never overwritten silently | Core lineage and workspace snapshot; conflict + browser tests |
| Fail/retry/revoke | Safe recovery in place; stale credentials/selections cannot start work | Core/vault; contract + browser tests |

Core owns durable state; URL owns session identity. Component state owns only
transient presentation and unsent input. Keep one project folder and freeze session
permissions. Distinguish estimated cost from settled usage; do not claim strict cost
limits without reliable accounting. After a Core restart, require explicit Resume.

## Verification and approval

Before implementation, map changed journeys to focused provider, harness, runtime,
component and real-Core browser tests. Collect exact cases and counts, announce
runtime bounds/exclusions, and bind `.github/test-selection.json` to the actual diff
using `scripts/test_selection.py`. Inspect CI triggers and receipts. No full suites
without a separate explicit user request.

Verify relevant journeys in the production bundle, desktop Chromium and mobile
Chromium/WebKit at required widths, plus a non-loopback LAN origin. Cover persistence,
restart, retries, malformed tool calls, budget limits, cancellation, permission
boundaries and checkpoint conflicts. Record bounded live OpenRouter acceptance using
two model families; report missing credentials/device gates honestly.

Implementation is active on the approved design. The current increment adds
credential verification, account-filtered discovery, bounded model descriptors,
allowlist filtering, searchable existing selectors and compatible tool routing.
It has not yet established live inference, endpoint compatibility, durable catalog
caching, broad harness parity, or the production/real-Core/device acceptance above.
