# Application Model V1 implementation handoff

Worktree: `/home/agent/nebula-hypothesis-graph`

Branch: `codex/hypothesis-graph`
Feature flag: `NEBULA_APPLICATION_MODEL=1` on Core (disabled by default).

## Delivered implementation

- Typed Pydantic domain records in `src/nebula/v3/application_model/domain.py`.
- Core entity persistence plus migration `0013_application_model_outbox`.
- Transactional source envelopes, deduplication, asynchronous projection,
  retry, history import, pause/resume, and durable projection checkpoints.
- Immutable observation/state/object-version histories and interpretation forks.
- Recorded traffic, action/command receipts, Evidence references, WebSocket
  metadata, and Repeater-result adapters. Explicit source references are retained;
  time proximity does not establish causality.
- Bounded extraction from already-redacted response JSON artifacts. A response
  representation is not represented as proven backend storage identity.
- Versioned formula AST and isolated Z3 workers, base consistency checks,
  typed assignments, assertion-linked unsat cores, cancellation, persisted results,
  and restart handling. Queries cannot dispatch browser actions.
- Project-scoped APIs; derived model kinds are excluded from generic CRUD routes.
- Existing browser-context agent tools for offline reads, comparisons, evidence
  inspection, identifiable proposals, and recorded-state queries. Harness agents
  receive project-scoped versions through Nebula's authenticated MCP gateway;
  `model.get_updates` incrementally reads facts after a state checkpoint.
- Project → Application model inspector: a primary black-box topology map,
  collection health metrics, interaction/object/state/inference lanes, typed
  selection inspector, state comparison, source links, object versions,
  assertions, condition builder, and saved query history. The map collapses to a
  vertical flow on mobile and keeps selection in URL state.
  URL owns selection, Core owns saved data, and forms own unsaved drafts.

```text
Existing Browser / API capture controls (unchanged)
                      |
       source record + outbox envelope
             [one Core transaction]
                      |
          retryable local projector
                      |
     observation -> object version -> knowledge state
         |                                  |
     source refs                    immutable parent refs
         |                                  |
         +---------- Inspector -------------+
                                            |
                       recorded fields + selected assumptions
                                            |
                                bounded Z3 subprocess
                                            |
                                  saved query/result

                  No result-to-browser execution path
```

## Verification (2026-09-08)

Backend regression command:

```sh
PYTHONPATH=/home/agent/nebula-hypothesis-graph/src:/tmp/nebula-model-deps.L4Brdi \
  /home/agent/nebula/.venv/bin/python -m pytest \
  tests/v3/test_application_model.py tests/v3/test_application_model_api.py \
  tests/v3/test_storage.py tests/v3/test_migrations.py \
  tests/v3/test_browser_automation.py tests/v3/test_browser_research.py \
  tests/v3/test_evidence.py tests/v3/test_browser_security.py -q
```

The temporary dependency path supplied Z3 without modifying the shared virtual
environment. Normal installations obtain it from the updated Poetry lockfile.
The system Poetry interpreter had an unrelated OpenSSL/boto import failure;
verification used the existing Nebula virtual environment.

- Backend: 74 passed, 1 skipped (PostgreSQL URL not configured).
- Component: `npx vitest run src/pages/ApplicationModelPage.test.tsx` — 3 passed.
- Production: `npm run build` — passed; existing bundle-size/dynamic-import warnings.
- Ruff for new Python files and `git diff --check` — passed.
- Production real-Core browser matrix: eight profiles, Chromium at 1440/1024,
  Android Chromium emulation at 320/390/430, iPhone WebKit emulation at
  320/390/430. Dedicated config: `ui/playwright.application-model.config.ts`.
- LAN origin: `http://192.168.1.155:19421`, desktop Chromium and iPhone WebKit
  emulation at 390. Test-only HTTP pairing opt-in; pairing issuance remains
  localhost-only. No production authentication setting was changed.
- Browser checks cover paired-device refresh, collection creation, committed
  capture → state → saved SAT result, pause/resume, deletion, retained sources,
  navigation-label overflow, scoped axe accessibility analysis, and screenshots.
- Additional Core test covers two redacted response-body versions, an observed
  property change, secret preservation, saved queries across Core restart, and
  immutable forks with equal semantic content but distinct provenance identity.

Browser commands (from `ui/`):

```sh
PLAYWRIGHT_BROWSERS_PATH=/tmp/nebula-model-browsers \
PYTHONPATH=/home/agent/nebula-hypothesis-graph/src:/tmp/nebula-model-deps.L4Brdi \
NEBULA_TEST_PYTHON=/home/agent/nebula/.venv/bin/python \
npx playwright test --config playwright.application-model.config.ts

PLAYWRIGHT_BROWSERS_PATH=/tmp/nebula-model-browsers \
NEBULA_MODEL_TEST_HOST=192.168.1.155 NEBULA_MODEL_TEST_PORT=19421 \
PYTHONPATH=/home/agent/nebula-hypothesis-graph/src:/tmp/nebula-model-deps.L4Brdi \
NEBULA_TEST_PYTHON=/home/agent/nebula/.venv/bin/python \
npx playwright test --config playwright.application-model.config.ts \
  --project model-desktop --project model-webkit-390 --output test-results/application-model-lan
```

Fresh temporary test browsers avoided stalled reads in the shared offloaded
browser cache. Screenshots are under `ui/test-results/` and
`ui/test-results/application-model-lan/`; those generated directories are not source files.

## Remaining acceptance and implementation limits

This is **partially verified**, not an unqualified release-completion claim.

1. Native Browser/agent controls → local fixture application → native capture
   was not exercised. Browser acceptance commits fixture traffic through the real
   capture API; it does not substitute for native execution coverage. The ordinary
   local-app workflow in the approved acceptance plan remains a release gate.
2. No physical phone, software keyboard, native desktop package, or physical
   Safari test. Mobile results are Playwright device emulation only.
3. PostgreSQL migration/concurrency coverage was skipped because
   `NEBULA_TEST_POSTGRES_URL` was unavailable. SQLite migration and rollback tests ran.
4. Background/resume, offline/reconnect transitions, full keyboard/touch/manual
   screen-reader testing, long-content matrices, and exact source deep-link
   navigation still need broader acceptance coverage. Axe is not manual usability
   evidence. A source-browser link selects a traffic record, not a restored page.
5. Inspector reads are bounded to 2,000 records per entity kind; larger collections
   explicitly fail rather than silently truncate. Solver requests are bounded to
   1,000 fields, a 900 KB payload, 500 AST nodes, depth 20, two workers and 16 queued
   requests. Pagination/compaction and multi-Core worker ownership are not V1 work.
6. Conditional values are representable but rejected by the solver until an
   explicit supported formula is supplied. Aliases/shared symbols require typed
   declared references. String values support equality/membership only; unknowns
   are unconstrained unless an explicit finite domain exists.
7. Response extraction currently covers bounded top-level scalar JSON properties;
   semantic page Evidence is retained as metadata/provenance, not a complete DOM
   or backend reconstruction. Dedicated network adapters remain future work.
8. Model tools are integrated with both the selected-browser chat context and
   the project-scoped harness MCP gateway. No generated-action or autonomous
   exploration catalog was added.

## Authorized live deployment

On 2026-09-08 the operator requested a branch push and live deployment. Commit
`7b47d8c` was deployed from the immutable checkout
`/home/agent/nebula-live-7b47d8c`. The user service has
`NEBULA_APPLICATION_MODEL=1`, uses that checkout for `PYTHONPATH` and static
assets, and runs Z3 4.16.0 from the shared Nebula virtual environment.

Before restart, SQLite online backup
`~/.local/share/nebula/v3/backups/pre-7b47d8c-20260908.db` passed
`integrity_check`. After restart, Core reported healthy as 3.0.0-alpha.13, the
database reported migration `0013_application_model_outbox`, the model API
reported both collection and solver availability, and the LAN origin served the
new production bundle. Generic CRUD continues to exclude application-model
records. Source evidence remains governed by its existing lifecycle; deleting a
collection removes derived model data only.
