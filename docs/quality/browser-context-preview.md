# Browser context preview

Journey: Browser > Ask about page/selection > inspect, fold, reopen, attach or close.
React owns the unsent capture and fold state; Core owns page revision, session and
assistant_paused. Folding must preserve the capture and browser stream, closing
must discard only the preview, and a fresh capture must open for review. Attaching
keeps existing context-authority and approval behavior. Live access still requires
explicit Resume assistant control; direct page input pauses control.

Validation: component state/focus tests, production desktop and mobile Chromium /
WebKit geometry and interaction checks, existing real-Core capture/attach/reload
journey, production build and LAN publication. Physical Mac installation remains
operator-owned. No changes to backend grants, persistence, deletion or runtime tools.
