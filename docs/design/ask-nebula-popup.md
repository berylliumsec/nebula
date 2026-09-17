# Ask Nebula popup

Journey: select text → Ask Nebula → ask and follow up in a floating dialog → close.
A separate Add context to chat action queues the selected bytes in the main composer.

| Step | Invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover/open | Compact overlay; route, composer and layout stay unchanged | React | component/browser |
| Fork | Snapshot of saved history; independent runtime; excluded from chats and search from creation | Core | Python/real Core |
| Ask/follow up | Responses stay in the popup; main conversation is unchanged | Core temporary branch | component/real Core |
| Stop/failure/retry | Stop cancels only the temporary turn; errors stay visible | Core + React | component/real Core |
| Close/refresh | Discard branch; abandoned branches expire; no reload restoration | Core lease + React | Python/real Core |
| Add context | Existing bounded context pack receives exact selection for next main turn | draft context/Core on send | component/browser |
| Mobile/keyboard | Floating dialog fits small screens; focus moves freely between popup and page; controls reachable | non-modal dialog/CSS | Chromium/WebKit production |

No split pane, automatic main-chat submission, or saved sidebar entry. Backgrounding
keeps the popup within its lease; closing and reload discard it; in-app navigation preserves it. Tests
will select only popup, selection handoff, and temporary-branch journeys. Physical
device evidence is required to claim physical-keyboard/mobile behavior.

## Product rule

A question about selected content must preserve the current draft and navigation.
Adding material to the main conversation is a separate, explicitly named action.
This applies to finding edits, notes, code buffers, and text selections alike.

## Verification — September 15, 2026

- `poetry run pytest -q tests/v3/test_temporary_chat.py tests/v3/test_chat_workspace.py`: **11 passed**, Python 3.11.
- `npm --prefix ui test -- src/components/AskNebulaPopup.test.tsx src/components/selection/selectionActions.test.tsx src/state/WorkbenchDraftContext.navigation.test.tsx`: **24 passed**.
- Production browser journey `tests/interface.spec.ts --grep 'assistant popup'`: **8 passed** at `http://192.168.1.155:19461`. Desktop Chromium 1440/1024; emulated Android Chromium and iPhone WebKit at 320/390/430. Includes bounded popup geometry, accessibility analysis, separate context action, asking, closing and unchanged URL. Screenshots: `/tmp/nebula-popup-browser/*/popup.png`.
- `tests/real-core.spec.ts --project=real-core --grep 'assistant upgrade popup'`: **2 passed**. Real Core and durable SQLite storage, production assets, LAN IPv4 with dynamically assigned ports, inert local provider and harness adapters. Verifies inherited history, private follow-ups, failure/retry, main draft preservation, list/search exclusion, dedicated harness identity, deletion and reload cleanup.
- `npm --prefix ui run build`: **passed**. Existing bundle-size/dynamic-import warnings remain.
- Scoped Ruff checks and `git diff --check`: **passed**.
- Selection: `.github/test-selection.json`; reviewed local receipt: `/tmp/nebula-popup-test-receipt.json`. No full suites or CI upload/push were performed.

### Limits

Physical devices, software keyboards and live vendor inference were not exercised.
Mobile evidence is browser emulation. The normal running service was not restarted
or deployed; acceptance used isolated Core processes and the production bundle.

Temporary branches use Core storage while open and are deleted when closed. If the
client disappears or cannot deliver cleanup, Core collects abandoned branches
one day after the last activity, checking once per minute while running.
The open popup renews its lease every five minutes and when the browser
regains focus. Normal service
audit retention is unchanged; this is a disposable chat, not a zero-retention
inference mode. Nothing from the popup is promoted to the main conversation.

## Concurrent questions

Journey: open Ask Nebula, start a response, select another source, and open a
second Ask Nebula window without stopping, replacing, or hiding the first. Each
window owns a separate temporary Core branch, stream, transcript, draft,
position, visibility, Stop action, and Close action. The provider owns an
unbounded keyed collection of windows; Core remains authoritative for each
temporary branch. New windows are slightly offset when the viewport has room,
while the existing viewport clamping keeps every window reachable on desktop
and mobile.

Closing or hiding one window must not affect any other window. Native browser
surfaces cannot composite beneath a DOM popup, so the Browser continuously uses
the largest rectangular part of its page surface that does not intersect any
visible Ask Nebula window. Moving or resizing a popup updates those native bounds;
hiding or closing it restores the full page surface. The native page remains live
and visible instead of blanking the whole Browser, while no part of the popup can
be buried beneath it. Full reload retains the existing disposable cleanup behavior
for every open branch. The broader product rule is that starting an auxiliary task
must not destroy another active task merely because both use the same presentation
type.

Agent results render as Markdown while they stream and remain visible after the
completion snapshot arrives. If a harness completion snapshot omits text, the
popup preserves the normalized `message_delta` result instead of clearing it.
An agent that genuinely completes without any text result produces an explicit
visible notice rather than an apparently empty successful turn.

## Non-modal popup correction

Journey: open Ask Nebula, keep using the underlying page, drag the header (or
use arrow keys on its move handle), navigate to another page, continue the same
side conversation, and explicitly close it. React at the application provider
owns the popup position and frozen source snapshot; Core owns the temporary
branch. Navigation must never discard or replace that branch. No backdrop,
scroll lock, focus trap, or outside-click dismissal. Resize keeps the window
reachable. Closing and full browser reload retain disposable cleanup semantics.

Verification: selected popup component/navigation tests, the existing eight
production browser profiles extended with movement and background interaction,
and the real-Core popup journey extended with route persistence and follow-up.
Backend and native code remain unchanged; physical devices unavailable.

### Approval candidate — non-modal revision

Figma: https://www.figma.com/design/R9gGLFaL3Ag2ATNv8oOObV
Branch: `codex/movable-ask-popup`. This revision awaits the requested design
approval before PR merge or live deployment. Mockup proposes a visible source
conversation label; final sizing and that label await design approval.

- 11 popup/component navigation tests passed.
- Eight production LAN browser profiles passed across Chromium and WebKit.
  Three initial failures clicked a heading covered by the moved popup; moving
  the window clear of that heading fixed the test sequence.
- Two real-Core popup journeys passed: provider follow-up across navigation,
  preserved main draft/history, harness isolation, and explicit-close deletion.
- Production build and frontend diagnostics audit passed.
- Evidence: `/tmp/nebula-movable-browser`, `/tmp/nebula-movable-browser-retry`,
  `/tmp/nebula-movable-real-core`, `/tmp/nebula-movable-build.log`.
- Physical-device keyboards and live vendor inference were not exercised.

Product rule: an auxiliary question window must not acquire modal ownership of
the workspace. Page navigation and focus changes are not dismissal actions.

## Hide and restore design

Journey: select context → Ask Nebula → ask or wait → Hide → use another
workbench view → Show → continue the same temporary conversation → Close.
The Figma states are [hidden](https://www.figma.com/design/R9gGLFaL3Ag2ATNv8oOObV?node-id=9-42)
and [restored](https://www.figma.com/design/R9gGLFaL3Ag2ATNv8oOObV?node-id=9-81),
with a [phone view](https://www.figma.com/design/R9gGLFaL3Ag2ATNv8oOObV?node-id=10-59).

| Step | Observable invariant | Authority | Test layer |
| --- | --- | --- | --- |
| Hide | A compact launcher remains visible; the question draft, response and branch survive | React presentation; Core branch | component, browser, real Core |
| Work elsewhere | Other page controls remain usable; the launcher stays reachable across in-app routes and viewport changes | React provider; browser viewport | production Chromium/WebKit |
| Stream while hidden | The response continues; the launcher shows short progress or an actionable problem | Core turn; React summary | component, real Core |
| Show | The same source snapshot, branch, transcript, draft and window position return; no branch creation | Core branch; React presentation | component, browser, real Core |
| Close after Show | Close discards the branch immediately and removes both window and launcher | Core deletion; React | real Core |
| Refresh | Full browser reload keeps the existing disposable cleanup behavior | Core lease | real Core |

Hide is a presentation choice, not cancellation or deletion. The compact
launcher uses a single Show target with an accessible name and status. Close
remains the explicit discard action in the expanded window. The source chat,
draft and navigation never change because of Hide or Show.
When the mobile Workbench navigation is present, the launcher sits above it.
Hide moves keyboard focus to Show; Show restores focus to the question field.

### Branch validation — September 15, 2026

- Popup component tests: 14 passed, including hidden response completion,
  draft retention, keyboard focus, branch identity and Stop.
- Focused production LAN browser journey: 8 passed at
  `http://192.168.1.155:19467`. Desktop Chromium 1440/1024, emulated
  Android Chromium 320/390/430, and emulated iPhone WebKit 320/390/430.
  The launcher clears the mobile navigation, remains available across
  in-app routes, permits Activity/Chat navigation by touch, passes a focused
  accessibility scan, and restores the same transcript and unsent text.
- Real-Core popup journeys: 2 passed with production assets and inert local
  provider/harness adapters. They verify branch persistence, a hidden active
  harness turn, cleanup after a hidden reload, explicit Stop and Close.
- Production build and diff-bound test-selection validation passed.
  Physical-device software keyboards and live vendor inference were not run.

### Reported no-response / Stop failure

The live temporary chat used a harness. Its stored events included startup,
reasoning and commentary while the popup only showed Thinking. The popup
tracked only `started.turnId`, so harness events with `harnessTurnId` left Stop
without a target. The saved live turn was eventually cancelled by popup discard.

The revision tracks harness identity across status/activity/start events, uses
the harness Stop endpoint, and falls back to the temporary session's pending
turn when the first event is missing. Cancellation failure remains retryable;
an unconfirmed Stop becomes an actionable error after ten seconds. It shows
compact progress/commentary and reconnect status while retaining the question.

Final validation: 17 component/navigation tests, eight production LAN browser
checks, and two real-Core journeys passed (25 selected tests). The harness
journey now stalls before an answer, verifies visible commentary, stops the
authoritative harness turn, edits/resends the retained question and receives a
response, then verifies cleanup and unchanged source history. Logs are under
`/tmp/nebula-movable-stop-*`. Physical devices and live vendor inference remain
unverified. No merge or live deployment of this revision was performed.

### Open-popup lifetime correction

The original created-at cleanup could discard a popup while an operator was
still using it. Core now collects after 24 hours without activity; the
non-modal popup renews every five minutes and on browser focus. Explicit Close
remains immediate. An orphaned temporary branch may remain in Core storage
for up to a day, while never appearing in chats or search. This is a UI
lifetime guarantee during an active session, not zero-retention inference.

### Immediate-question draft correction

On the 320 px WebKit journey, the popup could appear before Core finished
creating its temporary branch. Typing during that window sometimes cleared
the controlled input on a concurrent render, leaving Ask disabled. The
question field now retains its DOM draft during branch opening and reads it
when submitted. A component regression types before creation resolves; the
selected production WebKit journey verifies the visible draft and usable Ask
button. A single drag plus keyboard movement checks viewport bounds without
repeated key events during mobile viewport clamping.
