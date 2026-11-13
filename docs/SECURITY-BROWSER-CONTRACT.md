# Browser tools operator contract

The entry point is Project → Workbench → Browser. Nebula supplies browser
primitives for operator-directed work: navigation, interaction, identities,
traffic inspection, request interception and editing, Repeater, crawling,
HAR import/export, encoding utilities, and evidence capture.

Core owns sessions, traffic, requests, results, scope, grants, and budgets.
The browser engine owns live pages and secret-bearing identity state. The URL
owns the selected tool and session; unsaved request drafts remain local.

## Acceptance contract

- Operators can discover, select, and use the retained tools on desktop and
  inspect their durable results from paired devices.
- Agent browser automation follows the supplied objective and declared tool
  capabilities. Nebula supplies no Intruder execution, payload generation,
  guided scan profiles, or issue-validation plans.
- A saved request appears after Core read-back. Refresh and reconnect preserve
  its identity, request text, and retained results without duplicating a send.
- Interception, replay, and crawling obey the applicable scope revision,
  identity, request budgets, approvals, and stop/revoke controls.
- Failures explain the observed result and the next valid action. Uncertain
  effects require inspection before retry.
- Evidence and independently authored findings remain available. Recording a
  finding does not establish that its claimed impact has been verified.
- Retired assessment and Intruder links return to browser tools with a short
  unavailable-feature notice.

## Verification

Use focused component and API tests, then a production UI against disposable,
authenticated Core on a LAN origin. Exercise desktop Chromium and mobile
Chromium/WebKit, reload/reconnect, stale links, failures, and retained browser
automation. Use synthetic local targets and record build identity and receipts.
