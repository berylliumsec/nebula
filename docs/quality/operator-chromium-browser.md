# Chromium Browser Research contract

## Operator journey

Entry: Project Workbench → Browser. The operator opens an in-scope page inside
Nebula, switches identities and tabs, captures and intercepts requests and
responses, inspects HTTP/2 and WebSocket traffic, sends a request to Repeater,
chains an upstream proxy, runs approved automation, and saves evidence. The
page and controls remain in one Browser panel across resize, Assistant toggle,
refresh, and reconnect. A failed security control must stop the affected
request and explain recovery in the Browser panel.

| Step | Observable invariant | State authority | Required evidence |
| --- | --- | --- | --- |
| Discover/select | One Browser Research entry point; current identity/session/tab and capture state agree | URL for navigation, Core for research selection | component and production UI |
| Open/navigate | In-scope page loads in the panel with no second desktop window; out-of-scope main and subresource requests stop | Core scope, host egress proxy, Chromium page | real Core and Mac desktop |
| Capture | HTTP requests/responses, HTTP/2 metadata, WebSocket frames, bounded redacted bodies, and site map belong to the correct durable tab | Chromium network events, Core research records | HTTP/2 and WebSocket fixture |
| Intercept | Request/response pause, edit, forward, drop, timeout, and resume use Core's durable decision and fail closed | Core intercept record and Chromium interception bridge | real Core and failure tests |
| Repeater/upstream | Repeater uses the selected identity and configured upstream; secret references never enter the UI receipt | Core research state and Chromium network runtime | upstream fixture and evidence readback |
| Automation | Approved actions, run lease, stop, proxy rules, and evidence use the visible research identity and frozen scope | Core lease/approval, browserd, Core artifacts | real Core workflow |
| Refresh/reconnect | Same saved session, identity, tab, traffic, and pending decisions return without duplicates | Core database and browserd profile | reconnect and stale-revision tests |
| Revoke/delete | Revoked identities, stopped proxies, or deleted sessions cannot continue network or input | Core and browserd lifecycle | negative integration test |

The Assistant browser remains a separate, read-only viewer with its own
identity. Component state owns only drafts and panel presentation. The URL
identifies the selected project and research session. The Core database owns
research identities, sessions, tabs, traffic, intercepts, automation, and
evidence. browserd owns live Chromium pages and profile storage. The host
egress proxy enforces Project scope on every network request, including
subresources and WebSockets.

## Architecture decision and current gap

The current draft implementation does **not** meet this contract. Its operator
Chromium session is filtered out of Browser Research, and its private proxy
checks scope but does not capture or intercept traffic. The separate Native
capture control still uses WKWebView. The initial Chromium launch was headed,
which caused the separate OS window. An operator-only offscreen launch change
is in progress and still needs physical Mac confirmation. No focused UI test or Linux smoke
test can establish that the reported Mac compositor issue is resolved.

The target is one Core Browser Research session displayed through a Chromium
screencast, with Chromium network instrumentation and the same durable Core
research services. Chromium's request/response and WebSocket events provide
per-tab attribution; its interception stage must wait for a Core decision and
fail closed on disconnect. The host egress proxy remains a second enforcement
point. Upstream settings and credential references must be applied before a
context starts or through a controlled context replacement; replacing a
context must preserve the selected identity and disclose lost live page state.
The existing Rust capture proxy remains for legacy native sessions until the
Chromium path passes the full fixture matrix. Browser Research controls must
move together with the page; showing controls that operate a different session
would be unsafe.

This follows the publicly documented browser-session pattern: Browser Use
keeps an explicit session and CDP target per page, and Hermes uses a local
headless Chromium/CDP driver by default. Claude Code's documented Chrome
integration instead controls a visible external Chrome through an extension;
that is a different product surface and does not solve Nebula's in-panel
requirement. The public Codex browser automation examples demonstrate
Playwright/CDP control but do not document a reusable embedded security proxy.
Sources:

- https://github.com/browser-use/browser-use/blob/main/browser_use/browser/session.py
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/browser.md
- https://code.claude.com/docs/en/chrome
- https://github.com/openai/openai-cua-sample-app/blob/main/javascript-app/README.md
- https://playwright.dev/docs/network
- https://chromedevtools.github.io/devtools-protocol/tot/Fetch/
- https://chromedevtools.github.io/devtools-protocol/tot/Network/

## Verification boundary

Use selected unit, component, and permanent desktop/mobile Playwright journeys;
do not run full suites. The final acceptance fixture must use a production UI,
real Core, Chromium, HTTP/2, WebSocket, upstream proxy, scope denial, request
and response interception, Repeater, automation, evidence, reconnect, and
revocation. It must run at a non-loopback LAN origin. A physical Mac desktop
run must confirm that no separate window appears and no frame covers the URL
field during tab switching, resizing, and Assistant toggling. Until those
results exist, this PR stays draft and the Browser Research replacement is
incomplete.
