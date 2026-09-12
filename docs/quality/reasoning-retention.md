# Reasoning retention evidence

Branch: `codex/reasoning-display`, based on `6f341bc45ad15ec5cef8a8f074b3c833283204ad`.
Contract: [reasoning retention](../design/reasoning-retention.md).

## Outcome

Codex requests detailed provider summaries. Supplied summary and commentary text no longer inherits the 64,000-character diagnostic limit or the UI's 65,536-character stream limit. Oversized fragments are split into valid transport events. Completed snapshots retain differing earlier streamed text under an explicit disclosure. Commentary renders in expanded chat details. Completed reasoning with no supplied text stays discoverable; thinking counts advertise the activity disclosure. Historical truncation markers and malformed summaries receive an explanation.

Raw private Codex reasoning remains outside the display contract. Provider summaries are not a complete internal reasoning trace. Already discarded historical content cannot be reconstructed. Diagnostic/tool payload bounds and text redaction remain in force.

## Verification

- Python: 9 selected cases passed (4.56 s), Python 3.11. Covers schema-pinned detailed summary requests, Codex long summary streams split above 200,000 characters, malformed/private-field exclusion, Grok thinking and cancellation/completion races, commentary, and identical long-text durable replay for both vendors.
- UI: 33 tests passed (2.95 s) in `harnessActivity.test.ts`, `ActivityLedger.test.tsx`, and `HarnessReasoningDetails.test.tsx`. Covers reducer replay, long tails, differing final snapshots, public commentary, missing-summary discovery, disclosure and historical truncation explanations.
- Production: `npm --prefix ui run build` passed, with the existing bundle-size warning. Ruff and `git diff --check` passed.
- Browser: **16 passed (2.2 minutes)**: desktop Chromium 1440/1024 and emulated Android Chromium/iPhone WebKit at 320/390/430 px. Automated axe checks, keyboard/touch disclosure, keyboard scrolling and no-horizontal-overflow assertions passed before reload; both vendor journeys passed again after reload. Desktop and 320 px WebKit screenshots were also visually inspected.
- Browser selection: only `tests/thinking.spec.ts --grep 'thinking episodes remain'`, two vendor journeys across eight permanent profiles. The production bundle is served by disposable real Core at `http://192.168.1.155:19447`; fixtures contain synthetic provider events, not network stubs or live provider tasks.

The browser journey pairs a device, opens an existing project chat, expands saved activity, checks episode text including long tails and earlier snapshots, exercises keyboard/touch disclosure and keyboard scrolling, confirms absent-summary and historical-truncation explanations, checks accessibility and horizontal overflow, and reloads to repeat the checks. The Grok fixture also checks saved interrupted work and subsequent-message ordering.

Local logs and receipts: `/tmp/reasoning-python-test.log`, `/tmp/reasoning-ui-test.log`, `/tmp/reasoning-build.log`, `/tmp/reasoning-browser.log`, `/tmp/reasoning-selection-receipt.json`, `/tmp/reasoning-browser-selection.md`. Browser screenshots are retained under `ui/test-results/`. Initial test-only failures (ambiguous commentary label/body selector and asynchronous keyboard scrolling) are retained under `/tmp/reasoning-browser-first-failure` and `/tmp/reasoning-browser-scroll-failure`; the assertions were corrected without expanding coverage.

## Limits

Physical mobile devices and live authenticated Codex/Grok turns were not exercised. Browser evidence uses desktop Chromium and emulated mobile Chromium/WebKit with synthetic saved events against real Core; runtime tests exercise streaming/persistence separately. This does not establish physical-device or live-provider acceptance. Session creation/deletion/forking and network-transition behavior are unchanged and were not broadened into this selection. No full suite, deployment or push was performed; uploaded CI receipts are unavailable until the branch is pushed.
