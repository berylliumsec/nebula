# Explanatory-value admission

Contract before implementation: Project > Model > Add object > What this explains
> save > visible claim > refresh. Core is authoritative for admission, claims and
revisions. URL owns selection; the editor owns unsaved purpose and other drafts.
New objects require a nonblank textual purpose explaining behavior, a dependency
or a specific uncertainty. Existing records remain readable/editable without
backfilling invented explanations. This checks explicit rationale, not its truth.

| Lifecycle | Observable invariant | Planned layer |
| --- | --- | --- |
| Discover/create | Rule visible in schema/tool guidance and editor | unit/component/browser |
| Reject/retry | Missing purpose rejected atomically; draft retained | service/API/component |
| Select/refresh | Saved purpose appears as an ordinary provenance-bearing claim | real-Core browser |
| Legacy/edit | Old records editable without forced invented purpose | service/component |
| Repeated evidence | Strengthen existing claims, do not make duplicate nodes | workflow guidance |
| Stream/fork | No provider lifecycle changes; existing revision safeguards apply | existing tests, no provider calls |
| Dismiss/reset | Existing operator removal behavior retained | reset regression |

Use focused admission, graph/API/reset, editor tests; production real-Core LAN
browser journey in permanent desktop Chromium 1440/1024 and emulated Chromium/
WebKit 320/390/430. No live data reset, deployment or provider calls in this task.
Physical-device and semantic model-quality evaluation are separate missing gates.

## Evidence — 2026-09-10

Journey: existing Model entry point > add operation > missing explanation blocks
save and retains draft > explanatory claim saves > evidence/review/concurrent
edit recovery > refresh/reconnect > select the saved object. Existing reset and
lost-response retry are exercised on disposable projects with the new admission.

Unit/component: 57 backend tests passed in 11.11s, using `PYTHONPATH=src
/home/agent/nebula/.venv/bin/python -m pytest -q` with the exact files/nodes in
`.github/test-selection.json` and `--maxfail=1`. Covers missing/blank/non-string
purpose, atomic rollback, HTTP recovery, idempotency, legacy edit, discovery,
browser/harness instruction propagation and existing graph/reset boundaries.
`npx vitest run src/pages/ApplicationModelPage.test.tsx
src/pages/ApplicationModelReset.test.tsx` from `ui/`: 17 passed in 2.10s.
Initial build found a state-updater return-type issue; corrected and rebuilt.
Final `npm run build` passed, retaining existing bundle-size/import warnings.

Production/LAN command from `ui/`: `NEBULA_MODEL_TEST_HOST=192.168.1.155
NEBULA_MODEL_TEST_PORT=19444 NEBULA_TEST_PYTHON=/home/agent/nebula/.venv/bin/python
PYTHONPATH=/home/agent/nebula-hypothesis-graph/src timeout 300s npx playwright test
--config playwright.application-model.config.ts application-model.spec.ts
application-model-reset.spec.ts --max-failures=1
--output=/tmp/nebula-model-admission-browser`.
All layers bounded to 300s. No full suites or external targets.

Desktop Chromium: model-desktop 1440×900 and model-compact 1024×768 passed both
journeys. Mobile Chromium: emulated Android 320/390/430×844 passed. Mobile WebKit:
emulated iPhone 320/390/430×844 passed. Real Core: all 16 cases passed in 3.9m,
using the production bundle at `http://192.168.1.155:19444` and disposable SQLite.
The existing journeys include axe, focus/cancellation, reduced motion, mobile
geometry, reset landscape, refresh/network transition and concurrent writer retry.
Inspected the 320px WebKit screenshot: purpose and hypothesis provenance remain
readable. Screenshots retained in `/tmp/nebula-model-admission-browser/`.

Physical device: not run. Skipped gates: physical software keyboard, manual
interactive dogfood, active-provider semantic evaluation and PostgreSQL.
This validates the workflow and structural admission rule, not a guarantee that
an AI's supplied explanation is meaningful. A nonblank rationale can still be poor;
do not claim measured improvement in model selectivity without provider evaluation.
Existing model data is not retroactively rewritten or deleted. No live deployment.

Product rule: a valid type is insufficient grounds for creating an object. Require
an explicit contribution and allow “no model edit” as the correct result of routine
browsing. The nebula-product-quality skill drove the editor/recovery, preservation
and permanent mobile-browser checks rather than stopping at API validation.
