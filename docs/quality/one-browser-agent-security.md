# One visible browser and agent security runtime: acceptance contract

This contract precedes implementation of the disabled desktop cutover. The real
entry points are the Project Browser tab, the attached Chat browser tool, Chat's
MCP selector, and the Project security dashboard. The cutover must be enabled
only when all applicable rows pass together; a working page view alone is not a
working security workflow.

| Journey step | Operator-visible invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Desktop Browser offers one normal browser; Chat offers its existing browser tool and one optional Security Tools MCP. Web/mobile explains the desktop handoff. | Core catalog and desktop capability; React presents them | Component, desktop Electron, mobile Chromium/WebKit |
| Create | Opening the Browser creates one sandboxed visible tab for the selected Project. Starting security work creates a distinct headless identity and run. | Electron for visible tabs and storage; Core for run and policy | Electron, real Core/proxy |
| Select and use | Browser URL bar, selected tab, and chat browser action refer to the same visible page. Security MCP actions affect only the headless run. | Electron tab identity; Core project/conversation attachment and tool ledger | Electron plus real Core |
| Isolation | Visible navigation never enters security capture and shares no cookies, proxy, or request interception with the headless identity. | Electron session partition; Core worker and proxy | Electron plus proxy integration |
| Scope | Every outbound security request, redirect, and WebSocket connection checks current Project scope and active grants. Allow all never changes these checks. | Core policy and proxy | Real Core/proxy |
| Approve and stream | Security decisions, attack progress, evidence, and failures appear in the dashboard; the operator can approve or stop without manual attack forms. | Core run state, research records, and audit ledger | Component, real Core, LAN |
| Interrupt | Revoking Allow all or pressing emergency stop stops queued and active security work and records the stop. | Core policy and worker supervisor | Real Core/proxy |
| Background, refresh, reconnect | Project selection, approval choice, research results, and run status return from Core. The desktop bridge reports disconnected if Electron closes. | Core database; Electron process status | Real Core, Electron, production bundle |
| Retry and failure | Proxy, browser, or MCP failure is visible in place; retry does not duplicate actions or records. | Core tool ledger and run state | Component, real Core, Electron |
| Delete and revoke | Closing a tab destroys its view; stopping a security run closes its isolated context; revocation invalidates pending authority. | Electron and Core respectively | Electron, real Core |

Lifecycle coverage: discover, create, select, use, stream, interrupt, background,
reconnect, refresh, retry, close, and revoke are required. Forking a conversation
requires a new explicit browser attachment; it never silently inherits the old
conversation's authority. Research record history remains readable but cannot be
used as an active command target.

The URL owns only navigation identity. React owns transient display and inputs.
Electron owns visible pages, their storage, bounds, and page privileges. Core owns
Project policy, chat attachment, approvals, MCP selection, the security identity,
run state, durable research records, and audit. The headless browser and proxy are
supervised executors; neither is a policy authority.

Focused verification will select exact component, Python integration, Electron,
desktop/mobile browser, real-Core, production-bundle, and LAN journeys. No full
suite is authorized. Physical Mac overlap and packaged macOS, Windows, and Linux
smoke evidence remain mandatory before an unqualified cutover claim.
