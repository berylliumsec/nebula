# Operator Chromium browser panel

## Contract before implementation

Entry: Project Workbench → Browser → Interactive browser. The operator enters an
in-scope URL, sees the resulting page inside the Browser panel, clicks or types
in that page, resizes the window or opens Assistant, switches tabs, and reconnects
without losing the selected project or browsing identity. The address field and
other Nebula controls remain visible and clickable at every layout size.

| Journey step | Observable invariant | State authority | Planned test layer |
| --- | --- | --- | --- |
| Discover and select | Interactive browser is the default desktop browser, and the separate Assistant browser remains read-only | URL browser engine; React | component and desktop/mobile Playwright |
| Open and navigate | Core refuses out-of-scope URLs before Chromium navigates; the address and frame show the resulting page | Core scope; browserd page; React draft | Core/API and real browser journey |
| Click, type, scroll | Input from the framed image reaches the same Chromium tab; pending Assistant approvals become stale after manual input | Core session and approval ledger; browserd CDP | component, Core/API, real browser journey |
| Resize and change layout | The frame stays inside the React surface; viewport and pointer coordinates track its measured image | React surface; browserd viewport | component and desktop/mobile Playwright |
| Refresh and reconnect | The same Core session and tab return, with an actionable error if the stream disconnects | Core browser session; browserd tab | Core/API and real browser journey |
| Close or revoke | Closed tabs and revoked identities cannot receive further input | Core session; browserd tab | Core/API |

Streaming Assistant output, research traffic capture, and native WKWebView proxy
features are outside this first replacement. The existing Native browser option
remains available for those workflows until they are moved to the Chromium
session; it must not be described as visually fixed by this work. The Assistant
browser stays passive. A distinct operator-only API path grants direct input to
the Chromium session, retains authentication and live scope enforcement, and
does not widen the Assistant viewer's read-only endpoint.

Selected tests will cover the operator route and input validator, the interactive
surface and panel, and the Browser entry point in permanent desktop Chromium,
mobile Chromium, and mobile WebKit projects. The production bundle and real Core
browserd journey are needed because mocked Tauri calls and the Linux host cannot
observe the affected Mac's native compositor. The committed focused real-Core
journey is `scripts/smoke_test_operator_browser.py`; it uses a bounded fixture,
headed Chromium browserd, the production UI, and an optional non-loopback LAN
origin. The actual macOS screen remains a separate acceptance gate for the new
desktop default and the old Native capture option.

`npm --prefix ui run dev:desktop` now verifies or stages the pinned full Chromium
runtime before Tauri starts. The first run downloads the runtime; later runs reuse
it only while the version and executable digest still match the manifest.
