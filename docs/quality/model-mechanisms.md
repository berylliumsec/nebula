# Mechanism-focused application model

## Acceptance contract (before implementation)

Entry: project Model screen and browser assistant schema discovery. The operator
models how an application works, not a page/content inventory.

Core's catalog owns available types; the project graph in Core's database owns
claims, evidence, revisions and project specializations. UI state owns drafts and
selection; no new browser storage or capture authority is introduced.

| Journey | Invariant | Planned test layer |
| --- | --- | --- |
| Discover | Only mechanism types offered for new records | registry, API, browser |
| Create/select/use | Mechanism properties and connections persist and are inspectable | service, real-Core browser |
| Refresh/reconnect | Saved records, including legacy records, remain readable | service, real-Core browser |
| Failure/retry | Retired types cannot create new inventory; errors explain recovery | service, API |
| Delete/review | Existing evidence, corrections and dismissal behavior preserved | existing service/browser regression |
| Stream/interrupt/fork | Not applicable: no streaming or session lifecycle changes | none |

Production UI acceptance uses the dedicated Model test configuration: desktop
Chromium 1440/1024 and emulated mobile Chromium/WebKit 320/390/430. Physical
devices are unavailable. No provider calls or external application exploration.

## Meaningful things to model

1. Application workflows: user goal, entry view, trigger, operations, observable
   outcome and state transitions. A page merits an object only for its role here.
2. Authentication and access: identity establishment, identity provider, session
   establishment/expiry/logout, roles, permissions and observed access rules.
   Token and cookie metadata only, never credential values.
3. APIs: endpoint method and route, input/output shapes, validation and failure
   behavior; GraphQL operations, streams and callbacks when actually relevant.
   Individual requests/responses are evidence, not objects.
4. Executable client code: scripts, modules, handlers and workers whose observed
   responsibilities explain behavior, calls or state changes. No bundle inventory.
5. State and data: logical resources, transitions, ownership/lifecycle and browser
   or server persistence. Do not invent database tables from response fields.
6. Service dependencies and deferred work: observed service boundaries,
   integrations and background jobs that explain an application operation.
   Do not infer hosting topology or protection products from branding/status codes.

Promotion rule: a new object or relationship must explain behavior, establish a
meaningful dependency, or resolve a stated uncertainty. Repeated observations and
routine navigation need no graph edit. Keep evidence separate from the explanation.

Generic assets, links, controls, marketing content, credits, raw protocol messages,
and speculative infrastructure are excluded from new built-in vocabulary. Existing
records stay readable/editable without automatic reclassification or deletion.
Project types may specialize active mechanism types, not reopen inventory types.

## Supported vocabulary

The active catalog has 33 types (previously 77). The existing UI category names
are retained for saved links and filters; these six focus areas group the types:

| Focus | Active types |
| --- | --- |
| Workflows and behavior | Application, Page, Workflow, Operation, StateTransition |
| Authentication and access | AuthenticationFlow, Session, IdentityProvider, Role, Permission, Token, AccessControl, SecurityPolicy |
| APIs and exchanged shapes | Endpoint, Schema, GraphQLOperation, WebSocketChannel, EventStream, Webhook |
| Executable client code | JavaScriptAsset, ScriptModule, EventHandler, ServiceWorker, WebWorker |
| Data and persistence | DataResource, Storage, Cookie, LocalStorage, SessionStorage, IndexedDB |
| Dependencies and deferred work | Service, ExternalService, BackgroundJob |

The 17 built-in relationships are `contains`, `loads`, `executes`, `calls`,
`authenticates_via`, `requires_permission`, `uses_cookie`, `governed_by`, `stores_in`,
`reads_from`, `writes_to`, `depends_on`, `triggers`, `establishes_session`,
`requires_session`, `changes`, and `uses_schema`.

Every active type offers an evidence-bearing `purpose` property. Mechanism-specific
properties describe triggers, outcomes, data shapes, validation, session lifecycle,
script responsibilities and state changes. Unknowns remain absent. This schema
constrains vocabulary, not semantic truth: meaningful promotion still depends on
the evidence and the collection instructions. Raw capture/retention is unchanged.

## Verification (September 9, 2026)

- Python: 40 passed with Python 3.11, using
  `PYTHONPATH=src /home/agent/nebula/.venv/bin/python -m pytest -q --tb=short`
  and the six exact file/node selectors in `.github/test-selection.json`.
  Covers discovery, properties, transactions, evidence, conflict/retry, dismissal,
  durable reload, legacy records/subtypes, and browser/harness model tool exposure.
- Components: `npm test -- src/pages/ApplicationModelPage.test.tsx` — 9 passed.
  Includes preserving an old relationship's meaning when its claim is edited.
- Production: `npm run build` passed; existing chunk-size/dynamic-import warnings.
- Real-Core/browser: the two selected Model journeys passed all 16 cases using
  `npx playwright test --config playwright.application-model.config.ts application-model.spec.ts application-model-responsive.spec.ts --max-failures=1`.
  Disposable Core and production bundle at `http://192.168.1.155:19442`.
  Desktop Chromium 1440/1024; emulated Android Chromium and iPhone WebKit at
  320/390/430. Reload, schema selection, save, legacy inspection/edit, errors,
  retry, evidence, keyboard, responsive landscape, focus, and axe were exercised.
  Artifacts: `/tmp/nebula-mechanism-browser-final`.
- After the final nested-legacy-type compatibility adjustment, the same 40 Python
  tests passed again. The affected `application-model.spec.ts` journey was rerun
  in all eight profiles: 8 passed in 2.7 minutes. The dense-layout journey was
  unchanged and not repeated. Artifacts: `/tmp/nebula-mechanism-legacy-final`.
- The product-quality skill exposed a native-gray mobile map toggle contrast
  failure and a legacy-link editing fallback. Both now have regression coverage.
- Ruff and `git diff --check` passed. The focused selection receipt is validated
  locally against the change digest; no remote CI run was requested.

Limitations: no physical-device or real AI-provider session evaluation. Tests prove
the schema contract and instruction delivery, not how consistently an AI will
choose useful observations. The live service and its existing graph were not
modified or redeployed. No original evidence or graph objects were deleted.
