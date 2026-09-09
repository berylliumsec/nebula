# Application model redesign: implementation contract

Status: approved redesign implemented; acceptance remains partially verified pending a live assistant-driven site interpretation session.
The visual redesign requires explicit approval of a clickable Figma mockup before
implementation. The user explicitly approved the walkthrough with “Yes approved,
implement it” on 2026-09-09. No live reset has been executed.

Implementation authority: Core owns a single project graph, claim evidence and
revision history. Transactions compare the project revision and retain a durable
idempotency receipt; identities include authentication context. Browser captures
remain independent and do not instantiate objects. The URL owns selection and
filters; component state owns unsaved drafts. The journey and gate matrix below
remain the acceptance contract.

## Authorized rebase and local deployment — 2026-09-09

The operator requested rebasing on current main, pushing the branch, and updating
its local server. Rebased on `origin/main` `450ef96`; resolved the sole conflict by
retaining main's non-intercepting tooltip behavior and the compact reset controls.
The reviewed focused test selection is `.github/test-selection.json`: 35 backend
checks and 17 component checks passed. Production UI build passed, with index
SHA-256 `260b84c416776f3034113c61e9111e0f9e70e29e1154ec92689e073f10ac323a`.
The initial selected browser matrix passed 15/16; WebKit 320 was still bootstrapping
at a five-second readiness assertion. The bounded readiness wait was increased to
20 seconds and only that affected profile was selected for retry; both tests passed.

Deployment contract: retain the existing service environment, credentials, data
root, browser evidence and project folders; restart the user service onto an
immutable exported checkout of this branch (not a new git worktree), preserving
prior hashed UI assets for open clients. Verify served bundle identity, Core health,
migration version, source counts and paired desktop/mobile LAN navigation.

Before any live change, online SQLite backup
`~/.local/share/nebula/v3/backups/pre-hypothesis-rebase-20260909.db` passed integrity
checking. An isolated migration rehearsal removed 329 experimental model entities
and preserved all 1,209 other entities byte-for-byte; migration reached
`0014_application_graph` and integrity remained `ok`. Take another stopped-service
backup immediately before deployment. The operational rollout receipt is retained
with these backups. The live assistant/native-capture and physical-device limits
recorded below remain separate from deployment verification.

## Implementation and acceptance — 2026-09-09

This section supersedes the historical preparation/checkpoint statuses below.
Work remains in the requested worktree and `codex/hypothesis-graph` branch.

Implemented:

- A project graph with atomic revision checks, cross-process database serialization,
  durable idempotency receipts, claim-level evidence snapshots and edit history.
  Authentication context is part of identity; source references must belong to the
  same project and match their cited revision. Dismissals retain history/evidence
  and prevent silent identity recreation. Project deletion cascades to graph data.
- All 77 types, ten categories, 18 relationships and required inheritance, plus
  project-defined object types and relationships using the same validators/renderer.
- Shared discovery/search/neighborhood/evidence/update/transaction tools for legacy
  browser, attached-browser companion, project harness, and explicit model-question
  context. Graph-only access does not require an optional command runtime. Existing
  provider capability verification and cloud-result consent still apply.
- Responsive categorized outline, labeled directional map with bounded neighborhood
  expansion and stable positions, claim editor, project schema editor, evidence and
  history disclosures, URL-backed selection/filtering, source-browser deep links,
  old-link recovery, draft-preserving conflicts/retries, and existing-assistant handoff.
- Migration `0014_application_graph` deletes exactly the nine retired experimental
  entity kinds and their projection queue. Original browser records, observations,
  evidence and unrelated entity kinds survive. Removed projector/state/solver
  interfaces and the unused `z3-solver` dependency. No live database was reset.

Workspace maintenance journey: Workbench → Files → select/inspect a file → expand
maintenance only when needed. Core owns reset eligibility and durable file data;
component state owns the project-keyed disclosure and typed confirmation. Scratch
reset is collapsed by default and bounded when expanded. Linked folders show only
an explanatory status line, with no unusable reset form. Existing confirmation,
server-side eligibility recheck and linked-folder protection remain authoritative.
The broader product rule is progressive disclosure of secondary destructive actions
and hiding controls that do not apply to the selected workspace.

Verification commands (run from the worktree, except Playwright from `ui/`):

```sh
PYTHONPATH=src /home/agent/nebula/.venv/bin/python -m pytest tests/v3/test_application_model.py tests/v3/test_application_model_registry.py tests/v3/test_application_model_api.py tests/v3/test_chat.py tests/v3/test_harnesses.py -q
# 103 passed
PYTHONPATH=src /home/agent/nebula/.venv/bin/python -m pytest tests/v3/test_browser_companion.py -q
# 25 passed
npm --prefix ui run test -- --run src/pages/ApplicationModelPage.test.tsx src/components/ApplicationModelGraph.test.tsx src/components/WorkspacePanel.test.tsx
# 17 passed
npm --prefix ui run build
# Passed; existing chunk-size and mixed Tauri import warnings remain.
NEBULA_TEST_PYTHON=/home/agent/nebula/.venv/bin/python PYTHONPATH=/home/agent/nebula-hypothesis-graph/src npx playwright test --config playwright.application-model.config.ts
```

`poetry build` also passed. Wheel inspection confirmed the catalog, graph contracts
and migration are packaged and the retired solver/dependency are absent.
The wheel SHA-256 is `917ef456a17f846fdc5046f8de214713055f945d42060a3ca0f4cef21825bc8b`.

The tested production `ui/dist/index.html` SHA-256 is
`375ef88995c43abf6058b94ca6d3d59386711a60ae6459b8318bb7d799ddee81`.
The fixture serves this bundle through real Core and a disposable SQLite database.
Its workspace-path adapter uses disposable directories; listing, upload, preview
and reset eligibility use the real WorkspaceService. It does not use the live
project's workspace or database.

The main production journey passed in all **8** browser projects. Permanent projects cover Chromium desktop
1440×900 and 1024×768, Pixel 5 Chromium and iPhone 13 WebKit profiles at
320/390/430×844. All use reduced motion. Mobile profiles are emulated, not physical.
The main journey includes device pairing, actual browser navigation to a controlled
local 403 page, recording that response through the production capture API, meaningful
operator composition, evidence attachment, custom schema, concurrent edit recovery,
reload/network transition, source navigation, dismissal, upload/preview, and linked
workspace inspection. Browser evidence capture in this test is an explicit API
adapter, not a native/attached-browser capture-engine acceptance test.

The dense-content regression passed **8/8** on the same profiles, including 30
objects, long titles, disputed/hypothesized status, search persistence, editor
focus and cancellation, zero Axe violations, no horizontal clipping, and emulated
mobile landscape at 844×390:

```sh
NEBULA_TEST_PYTHON=/home/agent/nebula/.venv/bin/python PYTHONPATH=/home/agent/nebula-hypothesis-graph/src npx playwright test --config playwright.application-model.config.ts application-model-responsive.spec.ts --output test-results-dense
```

LAN acceptance passed **2/2**: desktop Chromium and emulated iPhone WebKit 390 px,
using the same production build at `http://192.168.1.155:19421`:

```sh
NEBULA_MODEL_TEST_HOST=192.168.1.155 NEBULA_MODEL_TEST_PORT=19421 NEBULA_TEST_PYTHON=/home/agent/nebula/.venv/bin/python PYTHONPATH=/home/agent/nebula-hypothesis-graph/src npx playwright test --config playwright.application-model.config.ts application-model.spec.ts --project model-desktop --project model-webkit-390 --output test-results-lan
```

Raw Playwright output from this run is retained locally at `/tmp/nebula-model-verification-20260909T121501Z`.

Axe reported zero violations within the application-model surface during each
main journey. Keyboard Enter and mobile tap exercise the reset disclosure; the
tests inspect real uploaded and linked-folder files while reset controls remain
compact. Screenshots are retained in `application-model-verification/` (desktop
and WebKit 390 px). Ruff on changed Python files and `git diff --check` passed.

Still missing: an end-to-end live assistant/provider session driving the controlled
site and evaluating uncertain firewall/database interpretations, including real
stream interruption and resumption. The disposable Core has no live provider or
attached browser engine configured. Broker dispatch, harness gateway, provider
model-context preparation/resume and evidence persistence are tested; they do not
prove model reasoning quality or native browser capture. Physical-device Safari,
software-keyboard behavior and actual browser zoom were not exercised. No live
service or packaged desktop build was deployed. These limits prevent an unqualified
product-completion claim.

## Figma walkthrough — 2026-09-09 continuation

Figma authentication now succeeds. Created the walkthrough in the authenticated
team's drafts: https://www.figma.com/design/puxMgdpG9k7dPJj5KzYk2F

- Desktop entry: `2:44`, 1440 × 960.
- Mobile entry: `4:585`, 390 × 844, relationship-list navigation.
- 68 walkthrough frames, 190 navigation actions, and 66 Back actions.
- Final structural audit: zero missing destinations, zero mobile-to-desktop
  navigation jumps, and zero overflowing detail-body text blocks.
- Visual samples inspected: foundations, control states, desktop overview,
  mobile overview, firewall detail, mobile evidence, mobile claim editor,
  and the complete categorized type selector. Text-family audit found Geist.
- Desktop graph labels have direct inspection hotspots; observed and hypothesized
  edges use solid and dashed lines and textual labels.
- Source-backed tokens: 19 scoped variables and four Geist text styles.
  Code Connect discovery found no mappings; library discovery found no matching
  Nebula controls or canvas token. The team Body style was 13 px, so the source
  14 px body token was retained.

The walkthrough includes the Site A form/input, script, authentication, cookie,
response, uncertain firewall and database/query examples; all ten categories and
77 built-in types; custom types/relationships; evidence attachment and conflict;
claim edits and history; model questions and interruption; save, dismissal,
loading, empty, failure/retry, concurrency and dense-content states. These are
illustrative design fixtures. Form values and search results are staged prototype
states, not functional Core-backed controls. Prototype navigation was inspected
through the Figma reaction graph; a browser-run prototype click test was not run.

Figma IDs are retained in `application-model-figma-state.json`; review previews are
in `application-model-previews/desktop.png` and `application-model-previews/mobile.png`.
The source UI, backend foundation, and live database were not changed this turn.
`git diff --check` passed. This is design-review evidence only: approval, runtime
implementation, backend/component tests, real-Core browser/assistant coverage,
production builds, browser/device matrices, accessibility and LAN gates remain
outstanding. Approval must be explicit before application UI implementation.

## Continuation evidence — 2026-09-09

Work continues in `/home/agent/nebula-hypothesis-graph` on the existing
`codex/hypothesis-graph` branch. Figma `whoami` returned `UNAUTHORIZED`,
`oauth_token_invalid_grant`, and `TRIGGER_REAUTHENTICATION`. No Figma file or
preview could be created; reconnecting Figma is required before the walkthrough.
The explicit visual-approval requirement comes from the requested work summary.

Registry foundation verification:
`PYTHONPATH=src /home/agent/nebula/.venv/bin/python -m pytest tests/v3/test_application_model_registry.py -q`
— **18 passed**. Coverage includes the complete 77-type, ten-category built-in
catalog and all 18 requested relationships, inherited property discovery and relationship
compatibility, invalid inheritance, identity hints, project extension isolation,
immutable definition mappings, strict scalar validation, and duplicate definitions.
Identity hints reject undefined properties and duplicates. `JavaScriptAsset`,
`Database`, and `QueryOperation` inherit from `Asset`, `Storage`, and `Operation`.
Cookie, token, CSRF token, and browser-storage definitions expose identifying
metadata but no secret-value property. The focused Ruff check and `git diff
--check` both pass.

Figma authentication was retried and still requires reauthentication. This is
partial foundation work. Graph persistence and evidence contracts, assistant
integration, migration, and approved visual design
remain to be implemented. No real-Core workflow, production bundle, desktop/mobile
browser matrix, LAN-origin acceptance, or physical-device workflow was exercised.

## Operator journey and authorities

Entry: Project → Application model, or the existing browser assistant's model link.
The operator browses a site, sees meaningful objects and labeled relationships,
inspects evidence, asks questions, and corrects the model without entering opaque
identifiers. The catalog describes possibilities; it does not instantiate them.

| Required journey | Observable invariant | Authority | Planned verification |
| --- | --- | --- | --- |
| Discover | Categorized search exposes built-in and custom types; only instantiated objects appear in the map | Registry + Core | Registry, component, Playwright |
| Create | Agent/operator changes appear immediately and are selectable | Core transaction | Unit + real Core |
| Select/use | Deep link and inspector identify the same object or relationship | URL + Core | Playwright + real Core |
| Edit | Evidence status belongs to each claim; acceptance never upgrades a hypothesis | Core revisions | Unit + real Core |
| Ask/stream/interrupt | Cited answers distinguish observations and interpretations; cancellation retains saved changes | Existing assistant runtime + Core | Harness + real Core |
| Background/resume | Returning restores selection and durable changes | URL + Core | Mobile + real Core |
| Refresh/reconnect | No duplicate objects or lost saved edits | Core revision cursor | Real Core |
| Failure/retry | Errors offer local recovery; replay is idempotent | Core mutation ledger | Unit + real Core |
| Delete/dismiss/revoke | Stale selection clears; dismissed claims do not silently return | Core tombstones + URL | Real Core |
| Custom types | Same registry, validation, tools, and renderer serve extensions | Project registry | Unit + UI + real Core |
| Migration | Only experimental model records and queue are reset; source evidence survives | Versioned migration | Migration tests |

Graph forking is not applicable: the replacement has one graph per project and
revision history, not a knowledge-state fork workflow. Browser sessions remain
evidence context, not graph ownership. Component state holds only drafts and
presentation; browser storage holds only device-local preferences. Identity and
authentication context must never be merged merely because URLs match.

## Figma deliverable and approval gate

Create a new design file with desktop and mobile walkthroughs using the product's
Geist/Geist Mono typography and tokens from `ui/src/tokens.css`. Reuse existing
PageHeader, buttons, form controls, disclosures, and project navigation semantics.
No Code Connect files were found during initial discovery. Remote component and
library discovery cannot proceed until Figma authentication is restored.

Desktop: searchable categorized outline, labeled relationship canvas, selection
inspector, and Ask about this model. Mobile: relationship-list default, drill-down
inspector, and equivalent editing with an optional graph. Keep touch targets at
least 44 px; use text and line styles as well as color for evidence status.

Required prototype frames and interactions:

1. Site A overview → select account page → inspect requires-auth relationship.
2. Authentication → cookie metadata → source evidence; no cookie secret value.
3. Form → input → operation → observed response; show linked JavaScript asset.
4. Observed 403 → hypothesized WAF inspector → supporting evidence. Do not claim
   that a status code establishes the cause or automatically generate a WAF.
5. Operation → hypothesized database/query relationship → evidence and disputed
   interpretation. Unknown vendor, host, table, and query remain unspecified.
6. Add object → categorized type search → schema-driven fields → saved selection.
7. Link objects → searchable endpoint selection → evidence/status → saved edge.
8. Edit/dismiss → revision history; custom type creation through the same controls.
9. Ask model → cited answer → source selection; show streaming and cancellation.
10. Mobile list → detail → evidence → edit, plus empty, loading, retry, unavailable
    assistant, long-content, and dense-graph frames.

Use all ten user-approved category labels verbatim. Return a Figma link and
previews for explicit approval before changing the application UI. A local image,
HTML prototype, or written design brief does not substitute for that deliverable.

## Foundation and rollout

Introduce the registry separately from the existing runtime. It contains canonical
type definitions, inheritance, typed properties, identity hints, evidence examples,
relationship compatibility, and project-scoped custom extensions. Catalog entries
must never manufacture observations or instantiate graph objects.

Next integrate claim/evidence contracts, project graph persistence, transactional
edits, revision cursors, source evidence reads, and shared browser/harness tools.
Switch runtime and apply a versioned experimental-model reset only when the new
path is implemented and tested. Remove old projections/solver/state interfaces in
that cutover. Do not reset the live database as part of design preparation.

## Required acceptance evidence

Registry/unit tests are foundation evidence only. Product completion additionally
requires the approved Figma design, backend/component coverage, committed real-Core
browser/assistant workflows, and the production bundle. Test Chromium desktop at
1024/1440 px and permanent Android Chromium/iPhone WebKit projects at applicable
320/390/430 px boundaries. Cover keyboard, focus, touch, screen-reader labels,
reduced motion, long content, refresh, cancellation, reconnect, and safe retries.
Run production non-loopback LAN acceptance and verify build identity. Record exact
commands/results and whether devices were emulated or physical. Any missing
required gate means the workflow remains incomplete or partially verified.

## Managed browser recovery regression (2026-09-09)

Journey: open the project browser from a paired LAN device, receive the host page, reconnect and refresh. Core owns durable browser identity and conversations; browserd owns live tabs and page contents; React owns the transient stream. Tab discovery and reconnect must return usable tabs without losing saved identity or altering page contents. Operator explicitly requested no redaction: this repair removes the dangling browserd helper call and returns browser results unchanged; it changes no UI, navigation, authorization, or mutation behavior. Validate the real browserd success endpoint and unchanged page contents, then existing-session tab discovery and streamed frames through the production LAN UI. New object creation, deletion, and assistant action execution are outside this focused repair. Physical Mac testing requires the operator's device; emulated browser checks cannot substitute for that evidence.

## Automatic agent model maintenance (2026-09-09)

Operator requirement: agent browsing includes incremental model maintenance without a separate request. Journey: attached browser operation -> durable observation -> agent receives exact evidence reference -> schema discovery/search -> graph transaction -> visible model on refresh. Core owns observations and graph revisions; the agent owns semantic interpretation. Browsing instructions apply equally to provider routing and harness developer instructions. Meaningful observations require graph maintenance before continuing/finishing; tab enumeration alone does not require edits. Repeated observations should reuse objects; unknowns remain unspecified and no-change observations must not produce invented claims. Tool results carry the saved observation id/revision and authentication context, including approved actions. Page data never supplies workflow instructions. Existing transaction concurrency, idempotency, project isolation and review rules remain authoritative. Test the saved-evidence/agent-context boundary and both instruction consumers; live provider compliance and full browser matrices require separate evidence and cannot be inferred from instruction tests.
