# Project removal acceptance

Removal archives a project; it retains Core history and all workspace files. Restore returns it to active status. Running work is not stopped by archiving.

| Journey | Observable invariant | Authority | Test |
| --- | --- | --- | --- |
| Discover/select | Switcher lists active projects and exposes removal and archived projects | Core status; URL selection | browser |
| Create/use | Existing creation and work remain available; archived projects are not auto-selected | Core; URL | browser |
| Remove/cancel | Confirmation explains retention; cancellation makes no mutation | dialog; Core | browser |
| Mutate/restore | Saved status is reloaded; restored project can be selected | Core | real Core browser |
| Refresh/reconnect | Archived projects stay out of active list; removed selection and URL are cleared | Core; URL; local storage | real Core browser |
| Failure/retry | Error remains in switcher; retry available; duplicate submissions disabled | Core revision; component pending state | browser |
| Stream/interrupt/background | Archive is catalog organization, not cancellation; history/workspace remain intact | existing Core execution | retention check; execution behavior unchanged |

Planned gates: component/API tests, production build, committed real-Core LAN browser regression on desktop 1440/1024 and emulated Chromium/WebKit 320/390/430. Check keyboard, touch sizes, overflow, cancellation, failure/retry, refresh, restore, and removing the final project. Physical device testing requires an available device.

## Verification — 2026-09-07

- Journey: project switcher → cancel → failed save → retry → archive → refresh → restore → open retained chat → archive final project → empty list → restore.
- Unit/API: `npm --prefix ui run test -- src/api/projectRemoval.test.ts`: 3 passed. Verifies revision-guarded archive/restore and an idempotent retry after a saved mutation.
- App/API regression: `npm --prefix ui run test -- src/App.test.tsx src/api/client.test.ts`: 78 passed.
- Production: `npm --prefix ui run build`: passed (existing chunk-size and dynamic-import warnings).
- Real Core/LAN: `npx playwright test --project=assistant-real-desktop --project=assistant-real-compact --project=assistant-real-chromium-320 --project=assistant-real-chromium-390 --project=assistant-real-chromium-430 --project=assistant-real-webkit-320 --project=assistant-real-webkit-390 --project=assistant-real-webkit-430 --grep 'project removal' --workers=1`: 8 passed. Temporary authenticated Core serves the production bundle on the host's non-loopback IPv4 address with dynamically assigned ports. No operator projects are mutated.
- Browser matrix: desktop Chromium 1440 and 1024; emulated Pixel 5 Chromium and iPhone 13 WebKit at 320, 390, and 430. The workflow checks 44 px removal targets, long project names, keyboard confirmation, cancellation focus, switcher accessibility with axe, failure/retry, durable archive/restore, and clearing local selection after the final removal.
- Retention: a real linked-folder file and real Core chat messages compare unchanged after archiving; restored chat is opened through the UI.
- Final-build follow-up: all eight profiles passed again after the idempotent-retry safeguard and mobile picker width correction (2.0 minutes). The final regression includes keyboard and touch/click cancellation focus, axe accessibility, and full-menu horizontal viewport bounds. Visual review of the WebKit 320 screenshot confirmed the long title is contained. Screenshots are retained under `ui/test-results/real-core-project-removal--efaa5-selection-on-production-LAN-<profile>/project-switcher.png`.
- Physical device: not run; mobile results are emulation, not physical Safari evidence. Active background execution was not exercised; archive updates only project status and does not invoke cancellation.
- Live deployment: subsequently authorized and performed; see the deployment record below. The browser matrix used temporary real Core instances.

Broader product rule: project management must expose the full create/select/remove/restore lifecycle at the project catalog, with durable state refresh and clear file/history retention semantics.

Additional audit: `npm --prefix ui run audit:css` fails on the pre-existing `font-size: 9px` at `ui/src/base.css:142` (`.top-bar-public-ip > span`). The same line is present in HEAD; this task does not modify that file. The CSS audit is therefore not a passing gate for this checkout.

## Authorized deployment

The operator requested deployment, mobile installation, and a branch/PR/merge.
Five reviewed frontend files were applied to the live checkout, preserving its
existing credential and compact-composer edits. A production build was staged in
`/tmp/nebula-project-removal-live-dist`, then assets were published and index.html
was replaced atomically. Prior hashed assets remain available for open sessions.
No Core restart was needed for this frontend-only deployment.

Localhost, LAN `http://192.168.1.155:8000/`, and WireGuard
`http://10.10.0.2:8000/` all returned HTTP 200 with index SHA-256
`01101ea8beb17552ed461039ee331cc978a899b12ea8b20e84c32f0ca654c85c`.
Unauthenticated API requests still return 401. The service's ephemeral admin token
is not retained in stdout (StandardOutput=null), so no authenticated production
mutation was attempted. Existing paired native apps remain the live device check.
