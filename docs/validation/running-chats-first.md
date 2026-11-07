# Running chats first

The operator opens Workbench → Conversations and sees running work at the top,
above pinned idle chats and chats waiting for input. Core owns activity and pin
state; the sidebar derives its groups. URL selection and parent/child ownership
remain unchanged. Within each group, pins and recent activity retain their order.
Expanded Butler chats and subagents put running siblings first.

| Journey | Invariant | Verification |
| --- | --- | --- |
| Discover/select | Working is first; chat IDs and hierarchy are preserved | Unit + production Core browser journey |
| Activity update | A pinned chat finishing returns to Pinned; remaining work stays first | Core fixture mutation + normal activity polling |
| Refresh/reconnect | Reload restores grouping from Core; search retains working-first ordering | Production Core browser journey |
| Blocked/paused | Waiting and idle chats follow running work | Unit + seeded Core records |
| Create/stream/interrupt/delete | No lifecycle mutation behavior changed | Outside this presentation-only change |

Independent diagnostic reproduction confirmed fixed group priority, pin-first
classification and unsorted nested children as the cause, even with correct
activity input. No Core status change is needed. Existing supervisor-controlled
waiting subagents retain their grouping, below actually running roots.

Selection: 16 unit cases in `conversationSidebar.test.ts`; one browser journey in
eight permanent model profiles (desktop Chromium 1440/1024, emulated Chromium and
WebKit 320/390/430). No Python unit suite or native tests: production behavior is
limited to the frontend transformation; the Python file is an isolated fixture.
No external provider, operator installation, or physical-device testing.

Validation results are recorded in the pull request. The fixture serves the
candidate production build at a non-loopback LAN address, verifies its build
manifest, keeps device pairing enabled, uses temporary data and a null keyring,
and does not submit work to a provider. It removes its data on shutdown.

Local validation (2026-11-07): all 16 unit cases passed; `npm --prefix ui run
build` passed. Production Core at `http://172.26.91.237:19489` passed the one
committed journey in all five Chromium profiles (1440/1024 desktop and emulated
320/390/430 mobile), including grouping, search, reload, authoritative finish
transition, group accessibility and no page overflow. Build manifest matched
`ui/dist/web-build.json` (base commit `99bb2e07`, built 12:22:36 UTC with the
candidate changes). The temporary Core shut down and removed its data.

Browser command used `NEBULA_TEST_PYTHON=/workspace/nebula/.venv/bin/python` and
`NEBULA_TEST_CHROMIUM_EXECUTABLE=/usr/bin/chromium` with the fixture's LAN address
added to `NO_PROXY`/`no_proxy`, then `npm --prefix ui run test:e2e --
tests/conversation-order.spec.ts --project=model-desktop --project=model-compact
--project=model-chromium-320 --project=model-chromium-390
--project=model-chromium-430` (5 passed in 22.1s).

WebKit binaries were unavailable locally: the browser download was denied by the
environment's outbound network policy. The three WebKit profiles remain required
in the PR CI selection before merging. Physical devices were unavailable. The
unchanged New chat provider subtitle has a pre-existing contrast failure; the axe
check intentionally covers the reordered conversation groups, which pass.
