# Chat switch preview contract

Entry: select A, select B, then return to A through Conversations.

| Journey | Observable invariant | Authority | Evidence planned |
| --- | --- | --- | --- |
| Discover/select | URL, title and transcript identify the selected chat; cached content appears before refresh completes | URL; memory preview; Core refresh | focused browser journey |
| Create/use/stream | Only durable messages enter previews; submission waits for refresh; existing drafts remain per chat | Core; existing draft storage | real Core create/use and browser disabled state |
| Interrupt/background | Switching detaches viewers without cancelling work; obsolete fetches cannot replace the new chat | existing generation/abort guards; Core | rapid-switch browser regression |
| Refresh/reconnect/retry | Fresh history replaces previews; failure retains readable preview with recovery and no submission | Core | browser failure/retry and real Core reload |
| Delete/revoke | Successful deletion invalidates preview; denied/missing history discards preview | Core | cache unit test and request error handling |
| Scroll | Returning to a chat restores its reading position, including after refresh | transient per-chat memory | browser long transcript |
| Scope | Cache is bounded to ten chats, isolated per project/API connection, and never persisted to browser storage | component-owned memory | unit test and implementation review |

Fork/create continues through existing selection and receives a cold load. Provider/harness execution semantics, layout, software keyboard behavior and new controls are unchanged. Test only the selected conversation-switch journey across the permanent desktop/mobile Chromium and WebKit profiles; production real-Core LAN checks cover durable history and reload. Physical devices are unavailable and must not be claimed.

## Acceptance evidence — 2026-09-12

- Unit: `npm --prefix ui test -- src/pages/chatPreviewCache.test.ts` — 3 passed.
- Production: `npm --prefix ui run build` — passed; existing bundle-size/dynamic-import warnings remain. Build log: `/tmp/chat-cache-build.log`.
- Browser: `tests/interface.spec.ts`, exact filter `conversation switching commits URL identity`, projects `desktop`, `compact`, `mobile-chromium-small`, `mobile-chromium-ledger-390`, `mobile-chromium-wide`, `mobile-webkit-small`, `mobile-webkit`, `mobile-webkit-wide` — 8 passed (47.2 seconds), served from the production preview on port 15420. Desktop widths 1440/1024, emulated Chromium 320/390/430, emulated WebKit 320/390/430.
- The selected browser case covers a 21-message transcript, held refresh, replacement with fresh history, draft retention, disabled/enabled submission, 503 failure/retry, rapid switching, keyboard selection, touch selection, no horizontal overflow and axe analysis of the chat thread. Reading offset 150 is preserved during cached rendering and after refresh, including when the mobile list hides the transcript.
- Real Core: `tests/real-core.spec.ts`, exact filter `assistant upgrade conversation switching restores durable Core history promptly`, project `assistant-real-desktop` — 1 passed (8.7 seconds total). Production bundle at `http://192.168.1.155:42169`; origin and screenshot are retained in `/tmp/chat-cache-real-core/real-core-assistant-upgrad-cfd23-rable-Core-history-promptly-assistant-real-desktop/`. A paired browser opens durable provider chats, reads A while its history request is held, refreshes against Core, retains A during a simulated transport 503, retries against Core, and reloads with durable content intact.
- Physical devices/software keyboards were not tested. No provider or harness command executes in these selected journeys. No full suite was run.

Product rule: a readable transcript should not wait for a network refresh when a session-scoped preview is available. Preview state is presentation only; current Core state governs submission and approvals.
