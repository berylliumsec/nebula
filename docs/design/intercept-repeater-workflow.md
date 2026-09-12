# Intercept and Repeater operator contract

## Journey and entry point

An operator opens **Workbench → Intercept** to inspect a paused native browser
transaction, then forwards, drops, or copies it into **Repeater**. In Repeater,
the operator edits a request and sends the exact visible draft once through the
selected desktop identity, then reads the retained response.

## State authorities

- Core owns intercept receipts, decisions, Repeater requests, revisions, and results.
- The native desktop proxy owns the live paused transaction and network execution.
- The selected browser session and identity determine cookies and request isolation.
- React owns only the unsaved Repeater editor draft, transient busy state, and notices.
- The URL owns the selected Workbench tool; device-local storage is not authoritative.

## Observable invariants

- The primary Repeater action sends the exact method, URL, headers, and body visible
  in the editor; it cannot silently queue an older durable revision.
- Saving without sending remains possible and visibly distinct.
- Headers accept familiar HTTP `Name: value` lines; JSON remains accepted for
  compatibility.
- A paused request can become a durable Repeater draft without retyping its URL,
  method, or retained non-secret headers.
- Forward and Drop remain explicit, fail-closed decisions. Request method, URL,
  and non-secret header edits flow through Core's durable decision to the live
  native transaction; redacted secrets remain unchanged.
- Durable state and result history survive refresh and reconnect.
- Paired browsers may prepare drafts and decide durable intercepts, but only the
  owning desktop performs network sends.

## Lifecycle and planned evidence

Discover, select, edit, save, send, receive a result, retry, cancel, delete, refresh,
and reconnect are applicable. Native live-request mutation is present in the preexisting draft but remains
unverified; UI checks alone do not establish native correctness. Component tests cover editor semantics and the intercept
handoff. Focused desktop Chromium, mobile Chromium, and mobile WebKit journeys cover
layout, keyboard/touch actions, and accessibility. Real-Core and production-build
checks cover durable revisions and network-worker integration; LAN origin is only
required if the focused journey exposes an origin-sensitive difference.
