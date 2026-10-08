# Studio Dark interface implementation

Figma reference: https://www.figma.com/design/z4C8ISDgsLbDQCrPf39yGf

## Operator journey and state

The operator opens a project, reads its current assessment, enters a conversation,
reviews live activity and evidence, and continues work from a phone or desktop.
The route and selected session remain URL-owned. Core owns projects, runs, findings,
approvals, transcripts, goals, and activity. Browser storage owns only the theme
choice and existing device-local presentation preferences. Component state owns
open menus, unsent text, and transient disclosure.

| Journey step | Observable invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover | Studio Dark is selectable and fresh previews show the new design | Theme preference | Component + browser |
| Create and select | Existing project/chat controls still create, list, and select the saved item | Core + URL | Real Core + browser |
| Use and stream | Real content and current progress remain legible; pending decisions remain visible | Core/harness | Browser + real Core |
| Interrupt and background | Stop and background behavior remain available without layout loss | Core + URL | Selected real-Core journey |
| Refresh and reconnect | Selection and durable content return in the same visual hierarchy | Core + URL | Selected real-Core journey |
| Failure and retry | Errors and retry actions stay visible in place | Core error contract | Browser |
| Delete and revoke | Existing destructive actions and confirmation remain reachable | Core | Selected real-Core journey |

## Visual and responsive invariants

- At desktop widths, navigation is a quiet left rail; the conversation and its
  Core-backed context have separate, bounded columns. The composer remains in view.
- At 320, 390, and 430 px, the transcript is the primary surface. Secondary
  context collapses into existing disclosures, and navigation and composer controls
  remain reachable with 44 px touch targets and the software keyboard.
- Project metrics use Core values. Zero, loading, error, long text, and pending
  approval states retain their current meaning and actions.
- Other themes remain selectable. This visual experiment changes the Studio Dark
  theme and the two referenced journeys, not their persistence model.

## Planned layers

Use focused theme/component tests and catalogued Workbench/Project Playwright
journeys in desktop Chromium and mobile Chromium/WebKit. Build the production
bundle, exercise a real Core session, and inspect a non-loopback LAN preview.
The throwaway instance uses an isolated checkout and data directory; it does not
replace the running Nebula service.

## Verification receipt

- Focused component selection: `ThemeContext.test.tsx`, `OverviewPage.test.tsx`,
  and `SettingsPage.test.tsx`; 13 tests passed.
- Focused browser selection: theme persistence (8), project and Workbench
  geometry (8), populated Studio conversation (3), and first-run default (1).
  The selected 1440/1024 px desktop Chromium, 320/393/430 px mobile Chromium,
  and 320/390/430 px mobile WebKit journeys passed. These are emulated browser
  profiles, not physical devices.
- The production UI build succeeded. A separate Core served that bundle from
  `http://192.168.1.155:18764` using an isolated temporary data directory.
  Through the visible UI, a `Studio Preview` project was created and selected;
  the project page showed the saved name and Core-owned zero-state metrics.
  Desktop Chromium and mobile WebKit/Chromium LAN views had no page errors or
  horizontal overflow; mobile metrics formed two columns at 320 and 390 px.
- The real-Core browser could open a new chat and focus its composer, but no
  provider or harness is configured in this throwaway data directory. A live
  streamed response was therefore not exercised; the populated conversation
  view was covered by the focused browser fixture.
- On this LAN launch, the initial bearer token is consumed from the URL and an
  unpaired browser receives 401 after reload. Reopening the tokenized preview
  link restores access. Pairing and a physical phone keyboard were not tested.
