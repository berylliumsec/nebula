# Browser context preview

Journey: Browser > Ask about page/selection > inspect, fold, reopen, attach or close.
React owns the unsent capture and fold state; Core owns page revision, session and
assistant_paused. Folding must preserve the capture and browser stream, closing
must discard only the preview, and a fresh capture must open for review. Attaching
keeps existing context-authority and approval behavior. Live access requires
explicit Resume assistant control and remains enabled until explicitly stopped.

Validation: component state/focus tests, production desktop and mobile Chromium /
WebKit geometry and interaction checks, existing real-Core capture/attach/reload
journey, production build and LAN publication. Physical Mac installation remains
operator-owned. No changes to backend grants, persistence, deletion or runtime tools.

## Explicit control lifetime

Operator correction: resuming is a session grant, not exclusive mouse ownership.
Normal page input, navigation, tab operations, viewport changes and removal of
attached values must not clear that grant. Explicit Stop clears it and revokes
pending actions before the execution lock. Manual mutations still invalidate pending
approvals; read-only pointer motion does not cancel them. Existing page-revision,
scope, runtime-privacy, turn-liveness, session-rebinding and browser-reset checks stay
in force. Core metadata remains the sole durable authority; the UI mirrors it.
Test resumed input/navigation, reload and follow-up, explicit stop, stale approvals,
concurrent queued operations and browser reset through component/Python/real Core.

## Stop and Resume are saved or reported

Journey: Browser > Stop assistant control (or Resume) > the control and status
show what Core saved. Core's session row is the only authority for
`assistant_paused`; the viewer mirrors it and never shows a state Core did not
confirm.

| Step | Observable invariant | Authority | Test layer |
| --- | --- | --- | --- |
| Stop / Resume | The control changes only after Core answers; meanwhile the status reads "Stopping…" or "Resuming…" and the control takes no second click | Core session row | component, real Core |
| Stop while the viewer polls or reconnects | Stop is saved and pending approvals are revoked whatever else writes the row; an unchanged tab poll writes nothing | Core session row | Python, real Core |
| Failure | "Assistant control was not stopped. The Assistant may still act in this browser." stays on screen with Core's reason and Retry stop; the control keeps Core's last reported state | viewer state | component, real Core |
| Retry | Retry sends the same Stop again; success clears the notice and returns focus to the control | Core session row | component, real Core |
| Reconnect / next poll | The notice clears once Core reports the requested state; hiding the browser chrome never hides it | Core session row | component, real Core |

The viewer's tab poll, a reconnect, a conversation rebind and the operator's
Stop all save the same row. Each applies its own fields to the row as stored and
tries again when another writer saved first, so none of them is refused as stale.
A Stop that still cannot be saved answers 409 and is shown, never dropped.
