# Workbench navigation hierarchy

Reference: https://www.figma.com/design/iG9y5Z3VXfT7AqInDmrTyi?node-id=1-2

## Operator contract

Entry: Workbench, through the main navigation or a saved conversation URL.
Primary views (Assistant, Terminal, Code, Browser) have labels on wide desktops;
Files, Notes, Missions and Activity remain distinct icon controls after a divider.
Conversation search, tool assistance, details and focus belong beside the current
conversation title. One pane-close control and one primary New chat action suffice.
The closed pane can be reopened beside the conversation title. Mobile retains its
existing bottom navigation and More sheet; labels collapse at compact widths.

| Journey | Invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover/select/use | Eight views remain reachable; selection follows URL; keyboard arrows cross the visual grouping | URL | component + production browser |
| Create | New chat stays at the far right and clears the selected conversation | existing session logic/Core | production browser |
| Refresh/reconnect | Conversation identity, history and pane preference survive reload | URL/Core; localStorage for pane | real Core LAN + browser |
| Search/details/focus | Existing actions operate in context; Escape exits focus; closed pane can be reopened | component state/localStorage | production browser |
| Loading/empty/long title | Context header has a meaningful fallback and cannot push actions offscreen | Core catalog | browser |
| Failure/retry | Existing transcript search and Core errors keep their recovery controls | existing Core/UI contracts | existing focused navigation cases |
| Stream/interrupt/background | No changes to runtime, transcript or cancellation state; toolbar does not remount the session | existing Core/session hooks | real Core retained history journey |
| Delete/revoke | Existing conversation menu remains available; lifecycle unchanged | Core | menu discovery only; mutation excluded |

All visible glyph targets stay at least 44 px. Check production desktop Chromium
1440/1024 and emulated Chromium/WebKit 320/390/430, keyboard focus, reduced motion,
long content and no horizontal clipping. Use existing tokens in all themes.
Native code, live provider calls, container execution and full test suites are
excluded. Physical-device testing is unavailable and is not claimed.

## Validation

- Component: `npm --prefix ui test -- src/components/SurfacePrimitives.test.tsx`
  (7 tests); frontend diagnostic audit and production build.
- Production browser: `tests/interface.spec.ts`, filtered to
  `stabilization compact Workbench header icons|stabilization conversations sidebar icon`,
  in desktop, compact, mobile-chromium-small, mobile-chromium-ledger-390,
  mobile-chromium-wide, mobile-webkit-small, mobile-webkit, mobile-webkit-wide
  (16 cases). Preview origin: `http://192.168.1.155:19452`.
- Real Core: `tests/real-core.spec.ts`, assistant-real-desktop, filtered to
  `assistant upgrade conversation switching restores durable Core history promptly`
  (1 case). Disposable real Core serves `ui/dist` on the LAN, pairs a browser,
  switches durable conversations, recovers from a history outage, and reloads.
- Local artifacts: `/tmp/nebula-navigation-final`, `/tmp/nebula-navigation-core`.
  Mobile devices are emulated, not physical. Plain light/dark desktop themes and
  the default Zero theme are covered by the selected header journey.

The broader product rule is to group navigation by destination and keep actions
beside their content. Responsive labels use the available header width, including
space taken by the project sidebar; narrower headers retain named 44 px controls.
