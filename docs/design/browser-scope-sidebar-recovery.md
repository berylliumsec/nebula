# Browser scope initialization and conversations entry

Entry: operator opens a permitted page from Workbench Browser, or retries a
missing-scope failure; operator reveals conversations from the left of the chat
controls. Core owns Project scope and session identity. Native proxy owns compiled
scope. React owns drawer presentation; existing device preference owns desktop
visibility. Never infer native readiness from a permission label.

| Journey | Invariant | Authority | Verification |
| --- | --- | --- | --- |
| Open/new tab | Install current scope before first proxied request | Core / native proxy | component, native compile check |
| Navigate/reload/back/forward | Refresh scope before manual navigation; failures prevent navigation | Core / native proxy | component |
| Scope missing/revoked | Remain blocked; provide retry in place | Core / native proxy | component |
| Conversations discovery | Left sidebar icon, named and focusable, 44px | UI | production browser matrix |
| Reveal/close/refresh | Existing selection and preference semantics remain | URL / Core / local preference | selected browser journey |

No creation/deletion of Project policy, automated commands, harness streaming,
or browser automation changes. Existing scope enforcement remains authoritative.
Use focused tests only. Physical devices and actual native-webview/Core acceptance
must be reported separately from mocked browser tests.

## Evidence

- Desktop toggle is first in Workbench controls, uses PanelLeft/PanelLeftClose,
  has Show/Hide conversations names and a 44px target. Mobile reuses its existing
  bottom Conversations entry with PanelLeft; no extra crowded top-header control.
- 38 selected component tests pass: terminal 16 + scope status 3 in
  `/tmp/nebula-scope-components.log`; browser 19 in
  `/tmp/nebula-scope-components-verified.log`.
- Two exact native scope restriction tests pass; native test binary compiles.
  Logs: `/tmp/nebula-scope-native-match.log`, `/tmp/nebula-scope-native-reject.log`.
- Production build passes (`/tmp/nebula-scope-build-final.log`); diagnostic audit
  has zero unclassified catch handlers.
- Production LAN `http://192.168.1.155:19450`: sidebar journey passes desktop
  Chromium 1440/1024, emulated Android Chromium 320/390/430, emulated iPhone WebKit
  320/390/430. One native IPC/browser fixture passes, including scope payload,
  bounds and truthful missing-scope status. Nine passed; one desktop-only fixture
  invocation skipped on compact. Artifacts: `/tmp/nebula-sidebar-scope-final`.
- Inspected desktop and smallest WebKit screenshots. Keyboard reveal/close,
  accessible names, 44px targets, no horizontal overflow and reduced motion pass.
- CI's existing native job additionally runs `tests/v3/test_sandbox.py -k macos`:
  two collected adapter-boundary checks, 48 excluded. This fixed integration
  selection is reviewed; no full suites. Remaining PR terminal coverage retains
  its previously reviewed exact five-journey/eight-profile selection.
- Partial verification: actual Core/native webview startup, page loading and
  reconnect were not exercised; local browser evidence uses mocked IPC/Core.
  Physical touch/software-keyboard, manual screen reader and rotation not tested.
  Requires a rebuilt desktop binary as well as UI assets; a UI-only deployment
  cannot supply the new native create-time scope argument handling.

Broader rule: install required enforcement state before starting a resource that
uses it; do not rely on background reconciliation to authorize its first request.
Sidebar controls should identify the pane and sit at its edge; keep existing
mobile navigation when duplicating it would crowd the header.
