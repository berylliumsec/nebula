# Native browser proxy recovery quality plan

## Operator journey

From the Workbench native browser, the operator enables capture, enables or
disables interception, navigates HTTP and HTTPS targets, makes an explicit queue
decision, and returns to direct browsing without a blank or permanently loading
surface.

## Observable invariants and authorities

| Step | Invariant | Authority | Test layer |
| --- | --- | --- | --- |
| Status feedback | Status icon, message, and dismiss action never overlap the native page or each other | React presentation state plus native child bounds | component + Playwright + packaged desktop |
| Capture setup | macOS HTTPS capture is not described as ready until the Project CA is actually trusted | macOS trust store plus native CA status | native test + physical macOS |
| Intercept | An in-scope HTTP or HTTPS request appears once and waits for a decision | native proxy waiter plus Core durable queue | native test + real Core |
| Resume | Disabling interception releases every native waiter even when its event was lost | native proxy waiter map | native test + packaged desktop |
| Failure/retry | TLS or proxy failure terminates loading and explains the valid recovery action | native page event plus React error state | component + physical macOS |
| Refresh/reconnect | A fresh tab uses current proxy and trust state without stale child bounds | native identity store plus Core session | real Core + physical macOS |

Create/delete, streaming, and mobile-browser lifecycle steps are not applicable to
the macOS child-webview transport itself. The paired-device UI still requires the
selected permanent responsive projects for any shared notice changes.
