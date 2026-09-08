# Browser loading and direct input

Journey: remote Mac > Workbench > Browser > navigate > see live page > click a field and type > reconnect/reload. Remove the separate Page keyboard form. Core owns identities and sessions; browserd owns the page; the UI owns transient input focus. Preserve scope, authentication, protected values, and explicit mutation approval.

| Step | Invariant | Authority | Test |
| --- | --- | --- | --- |
| Discover/use | No separate keyboard form; focused stream accepts hardware text/keys | UI, host page | component + production browser |
| Navigate | Core waits for a bounded browser operation; slow successful navigation is not reported failed after five seconds | Core/browserd | delayed real HTTP + real Core |
| Failure/retry | Timeout has an actionable safe message; no automatic replay | browserd/Core | API/component |
| Stream/reconnect | Missing upstream tab closes recoverably, not an unhandled exception | Core/browserd | websocket/API + real Core |
| Refresh/select | Existing identity/conversation survive | Core/URL | existing real Core workflow |
| Delete/revoke | Existing scope and revocation behavior stays authoritative | Core | existing browser tests |

Production LAN Chromium desktop/compact and mobile Chromium/WebKit checks cover the removed control and retained focus. Physical Mac/iPhone evidence must be reported separately from emulation. No new session creation/deletion semantics or assistant streaming semantics are introduced.

Diagnosis: live Core returned HTTP 500 after about five seconds while browserd later exhausted its 20-second navigation timeout. Isolated Chromium 149 navigation to Google succeeds directly; authenticated BrowserHostProxy navigation stalls with HTTP/2 and succeeds with HTTP/1.1. IPv4/IPv6 TCP/TLS and curl through the authenticated proxy succeed. Chromium netlog shows the page request cancelled before HTTP/2 request headers; the upstream Chromium/Playwright root cause is not established. The compatibility restriction applies only to Core's built-in managed browser, retaining authentication, TLS and scope enforcement. External browserd configurations keep their protocol settings. HTTP/2-specific behavior is not supported by this built-in path while the workaround remains.

Companion operations now have a 30-second total browserd budget and a 35-second Core read budget; health checks keep their short deadline. Timeout failures are sanitized and actionable, and no mutation is replayed. Rejected upstream stream handshakes close recoverably.

## Icon explanations

All app surfaces share a tooltip layer, including lazy screens, dialogs and disabled controls. Labels come from existing titles and accessible names; themes remain the styling authority. Hover and keyboard focus expose the explanation, Escape and leaving the control dismiss it, and the fixed overlay stays inside the viewport. The native title is temporarily suppressed to avoid two competing popups. Touch input is not intercepted. Tests cover name/description preservation, dynamic controls and absence of unnamed icons in the 24-screen production audit.

Evidence logs: `/tmp/nebula-load-unit.log`, `/tmp/nebula-load-ui-unit.log`, `/tmp/nebula-load-ui-matrix.log`, `/tmp/nebula-google-core.log`, `/tmp/nebula-load-core-ui.log`. Final counts and deployment identity are recorded in the pull request. No physical Mac or phone test is claimed from Linux browser emulation.
