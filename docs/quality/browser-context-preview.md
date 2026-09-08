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
