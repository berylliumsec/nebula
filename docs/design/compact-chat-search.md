# Compact conversation search

Journey: open Assistant (including browser/terminal companions), activate header search,
enter a query, change filters, follow a match, close search and resume reading.

| Step | Observable invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover/open/close | Named 44px header action; no closed search row; opening focuses input; Escape/close restores trigger focus | React presentation state | component + production browser |
| Search/select | Existing filters, results, match navigation and URL selection remain usable | Core search results and URL; local query/filter state | existing component tests + focused browser |
| Failure/retry | Query remains available and error allows retry | Core error, React presentation | component + browser |
| Refresh/switch | Search closes when session/view changes; saved conversation identity remains URL-owned | URL/Core | browser |
| Create/stream/interrupt/fork/delete/reconnect | No changes to these lifecycle operations or durable state | Existing Core/URL owners | excluded from this presentation-only change |

Planned layers: ChatSearchPanel component tests; existing transcript-search layout and
compact-header browser journeys across desktop Chromium 1440/1024, Android Chromium
320/390/430 and iPhone WebKit 320/390/430; production build. No origin-sensitive API
or durable-state changes. Physical devices and manual screen-reader access unavailable.

Product rule: secondary controls belong alongside existing header actions; a closed
control should not reserve a full content row.

## Editor file scrolling

Journey: Code → Files → scroll a long directory → reach and open the last entry.
The file list must use the remaining sidebar height, with header/tabs/path visible.
The same holds after refresh and when switching Files/Changes, in fitted desktop,
focus mode and mobile layouts. Core owns directory entries; React owns directory
selection; CSS owns only scroll geometry. No file mutation or permission changes.
Verification: one exact production browser journey across the same eight profiles,
with a long mocked directory, scroll position, containment and final-entry navigation.
Unit coverage is not useful for a CSS layout defect. File API and durable state
semantics remain unchanged; real-Core mutation/reconnect gates do not apply.

## Sidebar width

The shared Files/Changes divider supports pointer dragging, Arrow keys (Shift for
larger steps), Home/End bounds, and double-click reset. Device-local browser storage
owns preferred width (200–600px); container geometry clamps it to leave 320px for
the editor. A ResizeObserver applies that bound after viewport/focus-mode changes
without overwriting the preference. Narrow mobile remains stacked with no divider.
Verify drag, bounds, keyboard focus, Files/Changes continuity and reload retention
in the same focused sidebar browser journey; hook tests cover invalid storage and
clamping. No server-owned preferences or API changes.

## Acceptance evidence — September 12, 2026

- Component: `npm --prefix ui test -- src/components/ChatSearchPanel.test.tsx`
  (4 passed); `npm --prefix ui test -- src/components/useEditorSidebarWidth.test.tsx`
  (2 passed).
- Production build: `npm --prefix ui run build` passed. Existing chunk-size and
  mixed static/dynamic import warnings remain.
- Browser: three exact `tests/interface.spec.ts` journeys named in the committed
  selection, 24 passed total. Desktop Chromium 1440/1024; emulated Android Chromium
  320/390/430; emulated iPhone WebKit 320/390/430. Reduced motion enabled.
- Origin: production Vite preview at `http://192.168.1.155:19447`, mocked Core APIs.
- Search: header discovery, focus, filters, empty/error/retry, close/Escape/reopen,
  query retention, refresh, touch geometry and axe passed. Closed/mobile and
  expanded/desktop screenshots inspected.
- Editor: 70-entry directory, final entry focus/scroll/navigation, refresh,
  Files/Changes switching, focus mode, drag, keyboard bounds/reset and width
  retention after reload passed. Desktop screenshot inspected.
- Artifacts: `/tmp/compact-search-browser`, `/tmp/compact-sidebar-browser-retry`,
  `/tmp/compact-search-build.log`. Initial sidebar assertion required an exact
  intersection ratio; fractional border rounding produced 0.995. Final test allows
  0.99 while asserting containment and successful final-entry activation.
- Real-Core mutation/stream/reconnect checks: not applicable to these presentation
  changes. Physical-device and manual screen-reader testing were unavailable;
  emulation and automated accessibility do not establish those results.
