# Start over: application model and browser captures

Contract before implementation: Project > Model > Start over icon > counted
confirmation > clear > empty model > new capture/model creation. Do not reset
live operator data during development. Core owns reset revision, evidence cutoff
and retry receipt; URL owns selection/filter; drafts/confirmation are transient.

Clear this project's model, custom definitions, history, browser traffic/frames,
repeater results and browser-companion observations. Preserve other evidence,
observations, findings, chats, files/artifacts, browser identities, tabs, sign-ins,
action/command audit records and other projects. Shared attachments are retained.

| Lifecycle | Invariant | Test layer |
| --- | --- | --- |
| Preview/cancel | Counts/scope before mutation; Cancel focused | component + browser |
| Clear/retry | Atomic scope; same retry cannot clear newer data | service + API + browser |
| Concurrent edit | Stale revision rejected; explicit re-review | service + component |
| Refresh/use | Empty state durable; URL/drafts cleared; new captures work | service + browser |
| Old evidence/edit | Old references and revisions cannot replay | service |
| Stream/fork | Sessions are retained; reload open pages to recreate ongoing capture connections | new HTTP capture tested; existing WebSocket continuation not promised |

Planned focused tests: reset service/API/component, Model page, and reset journey
in permanent Chromium desktop 1440/1024 and emulated Chromium/WebKit 320/390/430.
Production LAN bundle, keyboard/focus/44px targets/axe/long title. Physical devices
and real AI-provider evaluation unavailable.

## Verification, 2026-09-09

Journey: Model > Start over > counted confirmation > Cancel; reopen > clear >
intentionally lose the successful response > collect a fresh capture > retry >
empty model and cleared URL > create a fresh object > reload and select it.

Unit/component: `PYTHONPATH=src /home/agent/nebula/.venv/bin/python -m pytest -q
tests/v3/test_application_model_reset.py tests/v3/test_application_model.py
tests/v3/test_application_model_api.py --maxfail=1` — **23 passed** in 8.76s.
`npx vitest run src/pages/ApplicationModelReset.test.tsx
src/pages/ApplicationModelPage.test.tsx` from `ui/` — **15 passed** in 2.50s.
All executions bounded by `timeout 300s`. The added storage sentinel initially
lacked required timestamps; corrected the test fixture and reran the selection.

Desktop Chromium: permanent `model-desktop` (1440×900) and `model-compact`
(1024×768), passed. Mobile Chromium: emulated Pixel profile at 320/390/430×844,
passed. Mobile WebKit: emulated iPhone profile at 320/390/430×844, passed.
Each case also rotates the open confirmation to 844×320 and verifies reachable
Cancel. Automated axe checks, initial/return focus, 44px button height, long-title
wrapping, reduced motion, no dialog horizontal clipping and safe retry passed.
Desktop and 320px WebKit screenshots inspected; fixed missing body inset spacing.

Real Core: production bundle served by disposable SQLite-backed Core, authenticated
device pairing and actual capture/model mutations; eight cases passed in 55.7s.
Command from `ui/`: `NEBULA_MODEL_TEST_HOST=192.168.1.155
NEBULA_MODEL_TEST_PORT=19442 NEBULA_TEST_PYTHON=/home/agent/nebula/.venv/bin/python
PYTHONPATH=/home/agent/nebula-hypothesis-graph/src timeout 300s npx playwright test
--config playwright.application-model.config.ts application-model-reset.spec.ts
--max-failures=1 --output=/tmp/nebula-model-reset-browser-final`.

Production/LAN: `http://192.168.1.155:19442`, `npm run build` passed. Build retains
existing large-chunk/dynamic-import warnings. Screenshots retained beneath
`/tmp/nebula-model-reset-browser-final/`. No live service changed or data cleared.

Physical device: not run; no device access. Skipped gates: manual interactive
browser dogfood, physical touch/software keyboard/screen reader, PostgreSQL and
active AI-provider/ongoing WebSocket scenarios. The reset has automated production
workflow verification, not an unqualified physical-device or streaming claim.
Open pages must be reloaded after clearing to capture ongoing connections again.
Preserved historical non-browser evidence remains elsewhere but cannot be reused
as pre-reset Model provenance. This is not a secure erase: shared files and audit
records remain. Stop active modelling first if it should not collect new data.

Product rule: reset is a first-class lifecycle action, with explicit project scope,
retention, cancellation, stale-edit protection and idempotent recovery. The
nebula-product-quality skill drove these safeguards and permanent mobile cases.
