# Top bar actions
Journey: open Workbench, discover assistance/details/focus directly, toggle and restore.
Invariants: no desktop overflow step; consistently spaced 44px icons, named and
tooltip-labelled; existing mobile navigation stays reachable.
Authority: React owns focus and popovers; the URL drawer parameter owns inspector visibility (the existing localStorage write is retained).
Discovery/use/refresh are applicable. Create, streaming, reconnect, retry and
delete are unchanged: no server-owned state or API changes.
Layers: existing App and PostToolAssistant component coverage, focused toolbar
browser journeys and production build. No origin-sensitive changes.

Verification: production build passed. Production preview at
http://127.0.0.1:15473 passed the direct-toolbar journey in desktop Chromium
1440x900 and 1024x700, emulated Pixel 5 Chromium, and emulated iPhone 13 WebKit.
Details open/close, named 44px icons, keyboard focus entry on desktop, touch-profile
focus entry/exit, and document overflow were exercised. Mobile retains More views.
Component selection: 36/38 initially passed; focus test passed after awaiting
mount; remaining transcript DOM-stability failure also reproduces on unchanged HEAD.
Logs: /tmp/toolbar-unit.log, /tmp/toolbar-unit-retry.log,
/tmp/toolbar-baseline.log, /tmp/toolbar-browser.log,
/tmp/toolbar-browser-retry.log, /tmp/toolbar-build.log.
Limitations: no physical device, full 320–430px boundary matrix, manual visual/AT
review or live deployment check. Partially verified; no backend or origin-sensitive
behavior was changed. No CI upload was made; local selection receipt reviewed.
Product rule: expose frequent secondary actions directly when toolbar space allows,
with consistent icon geometry rather than an unnecessary overflow step.

Integration against main c5fd66d: retained the shell-header toolbar and New chat
action from #283; direct actions use the same 44px spacing. Mobile shell header
keeps only New chat and retains More views for navigation/focus.
Production build passed. All 12 selected browser checks passed: direct action
journey (4 profiles) and existing compact-header journey (8 profiles, including
320/390/430 Chromium/WebKit). Screenshots inspected for desktop layout.
Component rerun: 37 passed, one existing transcript DOM-stability failure.
Artifacts: /tmp/toolbar-integrated-{build,unit,browser,header}.log.
Physical-device and manual screen-reader checks remain unavailable.
