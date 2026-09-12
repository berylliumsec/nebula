# Compact terminal header

Journey: Workbench Terminal → discover/select a terminal → inspect connection and
runtime → capture output or stop the selected terminal → open Assistant.

| Step | Invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover/select | Tabs, active connection and actions share a toolbar; switching keeps sockets mounted | Core sessions, React active key | component + production browser |
| Details/network | Image identity and inbound settings open on demand; values and policy remain unchanged | Frozen Core runtime/network, local next-launch input | component + browser |
| Capture/stop/retry | Actions still target the active session; failures stay visible | Existing socket/Core APIs | component + focused browser |
| Refresh/reconnect | Existing recovery and connection lifecycle stays unchanged | Core sessions/socket | existing component lifecycle tests; no backend changes |
| Responsive | Controls retain 44px targets, keyboard focus, no clipping; narrow layout may wrap | CSS/transient disclosure state | desktop and mobile Chromium/WebKit |

No new terminal commands, permissions, network behavior, or session lifecycle are
introduced. Generic runtime metadata may collapse; errors, audit warnings and
frozen VPN/bridge boundary notices stay visible. Planned layers: selected terminal
component file and exact compact-header/capture/VPN/Assistant browser journeys,
production build and LAN-origin preview. Physical devices unavailable. Real-Core
end-to-end evidence will be reported separately from mocked UI evidence.

## Added reported regressions

- Terminal long output: the last rendered row and prompt must remain inside the
  visible shell; scrollback must move independently and input must return to the
  current prompt. The compact live surface uses a shrinking flex child instead
  of the former fixed grid rows. Add a long-output/echo browser regression.
- Browser status: Project permission is distinct from native session readiness.
  A native missing-scope event overrides the permissive badge and reports blocked
  navigation without suppressing research records. Native events own live status;
  durable traffic is the reload fallback. Successful unblocked traffic clears the
  live error. No scope enforcement or automated action workflow changes.

## Acceptance evidence — September 12, 2026

- Component files: ContainerTerminalPanel (16 passed), WorkbenchBrowser (16
  passed), browserScopeStatus (3 passed): 35 total. Commands use `npm --prefix ui
  test --` followed by only those files; final browser component run is recorded
  in `/tmp/compact-terminal-browser-unit-final.log`.
- Production build and frontend diagnostic audit passed. Existing bundle-size and
  mixed static/dynamic import warnings remain.
- Production LAN preview: `http://192.168.1.155:19449`, mocked Core and terminal
  transport; no real commands or external browsing performed for these checks.
- Terminal: 32 toolbar/screenshot/VPN/Assistant checks plus 8 long-output checks
  passed. Desktop Chromium 1440/1024; emulated Android Chromium 320/390/430;
  emulated iPhone WebKit 320/390/430. All use reduced motion. Long-output checks
  cover 150 output rows, desktop wheel input, keyboard scrollback, echoed input
  and the final cursor inside the viewport. Screenshots inspected at desktop
  1024 and Android 320. Geometry receipts retain the cursor ancestor bounds.
- Browser: one desktop native-event fixture passed, showing an unavailable-scope
  badge and navigation notice without replacing the research panel with a false
  workspace-loading error. Native calls are mocked; screenshot inspected.
- Artifacts: `/tmp/compact-terminal-final`,
  `/tmp/compact-terminal-scrollback-verified`,
  `/tmp/compact-terminal-regressions-retry`, `/tmp/compact-terminal-build.log`.
- The clipping regression caught both the FitAddon padding mismatch and the
  vendor-canvas layer overriding component min-height rules. Final geometry
  overrides live in ui.css, the unlayered geometry contract.
- Missing evidence: actual Core/container execution and physical native-webview
  reproduction, physical touch/software keyboard, landscape rotation and manual
  screen-reader checks. This is verified UI behavior in the stated production
  fixtures, not a claim that a missing native scope has been repaired.

Product rules: secondary controls share an existing toolbar; canvas sizing must
use its renderer's measured content box; Project permission and runtime readiness
must be represented separately. Navigation failures must not masquerade as lost
research data.
