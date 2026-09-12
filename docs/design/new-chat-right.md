# New chat at the right edge

Journey: open Workbench, find New chat at the far right of the shell header,
activate it by keyboard or touch, and reach the empty composer. In focus mode,
the same action remains at the right edge of the Workbench toolbar.
Invariant: one visible New chat action, 44px target, accessible name and tooltip,
at least 12px separation from the preceding control, and no horizontal clipping
at desktop 1440/1024 and emulated mobile 320/390/430 widths.
React owns portal placement and transient presentation; existing URL/Core chat
identity and mutation behavior remain unchanged. Discovery, navigation, focus-mode
entry/exit and New chat activation apply. Streaming, retry, reconnect and deletion
are unaffected by this placement-only change.
Test layers: PageHeader portal/action and App/TopBar component tests; existing
compact-header browser journey across eight profiles; production build.
No origin-sensitive behavior changes. Physical-device/AT evidence is unavailable.
Product rule: separate the primary creation action from tool/view controls and
reserve a consistent end-of-header position for it.

Transcript search: open search disclosure, enter a query, choose conversation and
bookmark filters, submit, read results or an inline error, and retry/select a hit.
Invariants: one labelled search field, icon submit with busy feedback, 44px filter
targets, wrapping at narrow widths, and unchanged search arguments/result selection.
React owns query/filter/busy state; Core remains authoritative for search results.
Presentation-only: saved bookmarks, search API and navigation are unchanged.
Planned layers: focused ChatSearchPanel unit tests and one mocked production
search journey across the same eight browser profiles, including empty and retry.

Verification: 47 selected component tests passed; 3 search component tests passed
again after restricting scrolling to results. Production build passed. Eight
header placement/navigation browser cases passed. Eight search layout/error/retry
and Axe cases passed, including a focused WebKit rerun after a transient geometry
read; geometry now waits for layout to settle instead of sampling once.
Production preview: http://127.0.0.1:15478 and :15479. Desktop Chromium 1440/1024;
emulated Chromium and WebKit at 320/390/430. Final screenshots inspected at desktop
and 320px. Controls remain visible; only results scroll. Physical devices and
manual screen-reader checks were unavailable. No live deployment was changed.
Evidence: /tmp/header-search-unit.log, /tmp/search-final-unit.log,
/tmp/header-search-build.log, /tmp/header-search-browser.log,
/tmp/search-layout-final2.log, /tmp/search-webkit-detail.log.
