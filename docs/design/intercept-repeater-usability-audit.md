# Intercept and Repeater usability audit

Date: 2026-09-12
Branch: `codex/intercept-repeater-usability`

## Scope and evidence

Source review of the existing uncommitted Intercept/Repeater work, preserved on
this branch. This is a design and state-management audit, not runtime validation
or a claim of Burp feature parity. No network execution behavior was changed.

The product goal is to make it immediately clear which transaction is selected,
what has been edited, what was saved, and which response belongs to which request.
Burp is a reference for interaction clarity, rather than a feature checklist.

Primary references:

- [Burp HTTP history](https://portswigger.net/burp/documentation/desktop/tools/proxy/http-history): selecting a transaction brings its request and response together.
- [Burp Repeater tabs](https://portswigger.net/burp/documentation/desktop/tools/repeater/managing-tabs): separate requests have recognizable, manageable workspaces.
- [Burp HTTP messages](https://portswigger.net/burp/documentation/desktop/tools/repeater/http-messages): target context and message inspection stay close to the work.

## Findings, in priority order

1. **Unsaved work has no protection.** In `BrowserResearchSuite.tsx`,
   `selectRepeater` and `newRepeater` replace the editor directly. The parent
   `WorkbenchBrowser.tsx` unmounts this component when switching to Traffic,
   Actions, Identities, or Session. Switching between Intercept and Repeater
   retains the component, but a detour through those other tools loses local
   editor state. Preserve drafts by request and session above the tool lifetime;
   show a modified marker and make discard explicit.

2. **The response is detached from the editor.** Repeater puts the editor above
   a saved-request list, then nests result history, response headers, and a body
   preview inside individual list entries. Give the selected request a dedicated
   response pane. Show the result's submitted revision so subsequent edits cannot
   make an old response look like it belongs to the current draft.

3. **Copy has an incomplete handoff.** `copyInterceptToRepeater` saves and selects
   a request but leaves the operator in Intercept with a text instruction to find
   Repeater. It explicitly initializes an empty body. Provide an actionable
   “Open in Repeater” confirmation, preserve the paused-transaction context, and
   explain unavailable or redacted content. Never present a partial copy as an
   exact original. This audit does not propose changing capture capabilities.

4. **Polling can erase recovery information.** `refresh` clears the same error
   state used by operator actions after every successful fetch, including the
   1.5-second background poll. Separate fetch status from action failures; an
   unrelated successful poll must not dismiss a failure the operator needs.

5. **Selection changes can leave misleading fields.** The session-change effect
   clears the selected ID and replaces the URL but retains the previous method,
   headers, body, and name. Deleting a selected request clears its ID but leaves
   the editor content. Define atomic transitions to a complete session-scoped
   draft or an explicit empty state, with no mixed identity or request context.

6. **Intercept mixes decisions with history.** Pending and completed items share
   a long list, and the basic request-forward action is inside the edit disclosure.
   Visually separate pending attention from completed history; keep existing
   decisions discoverable without opening an editing form. Show expiry urgency
   next to the affected item. Validate this independently of native execution.

7. **Preview limits are silent.** `loadRepeaterBody` slices text at 1,048,576
   characters without an explicit truncation indicator. Distinguish empty,
   unavailable, redacted, truncated, loading, and failed response content.

8. **The current contract contradicts itself.**
   `intercept-repeater-workflow.md` says native edits flow through in its invariants
   but says mutation is unsupported in its lifecycle section. Reconcile intended
   scope with separately verified behavior before presenting that work as done.

These are source findings. Browser reproduction and native correctness remain
unverified; the existing tests do not establish the complete operator journey.

## Proposed workspace

Use one compact request selector with recognizable names, method, and modified
state. Keep the selected request's target and identity in a quiet context row.
Place the request editor and selected response side by side on desktop, with
Request / Response panels on narrow screens. Keep response status and revision
visible when changing panels. Put history beside the response, with one selected
result, rather than nesting it under every saved request.

Use existing design tokens and familiar icons for secondary controls, with
tooltips, accessible names, visible focus, and 44 px touch targets. Keep primary
actions labeled. Avoid decorative cards around each field and competing toolbar
rows. A selected request should look like one continuous workspace.

## First implementation slice: selection, drafts, and response inspection

Operator journey: open an existing request, inspect its retained response, edit a
draft, switch requests and tools, return, and recover from a failed read without
losing edits. This slice concerns presentation and state preservation; it does
not require new execution or automation capabilities.

State authorities:

- Core remains authoritative for saved requests, revisions, and retained results.
- Session and identity provide explicit context for the selected request.
- A parent-owned draft map, keyed by project/session/request, owns unsaved edits
  across tool navigation. New drafts need stable temporary keys.
- Selection and result selection must be explicit; background refresh cannot
  replace a dirty draft or silently choose a different result.
- Unsaved refresh behavior must be deliberate: either restore drafts using an
  approved persistence policy or warn before leaving. Do not introduce storage
  of potentially sensitive message content as an incidental UI convenience.

Observable invariants:

- Switching requests and tools preserves each draft and its modified marker.
- Switching sessions never combines fields from different requests.
- The visible response identifies its saved request revision.
- Deleting a selected item resolves to a defined next selection or empty state.
- Read failures remain visible with local retry; polling cannot dismiss them.
- Missing, redacted, and truncated content is clearly distinguished from empty.
- Mobile users can reach both message panels and their actions without horizontal
  page overflow. Keyboard selection and focus remain predictable.

Planned validation layers, before implementation:

- Focused component cases for draft switching, tool unmount/remount, session
  isolation, selected deletion, revision selection, and polling/error separation.
- Focused production browser journeys for the same selection/inspection workflow,
  using retained benign fixture messages and real Core persistence and reconnect.
- Desktop and 320–430 px mobile Chromium/WebKit coverage required by the product
  quality skill, plus applicable LAN-origin and physical-device evidence. Emulation
  must be reported separately from physical devices.
- Loading, empty, long-content, disabled, error, retry, and success states;
  keyboard, touch, screen-reader labels, visible focus, and reduced motion.
- Verify the served production build contains the change before claiming success.

Exact files, filters, collection counts, runtime bounds, and exclusions must be
recorded in the diff-bound test-selection receipt before executing tests. No
tests were run for this documentation-only audit. Execution engines and unrelated
tool suites are outside this first slice.

## Broader product rule

Every tool transition must preserve the operator's work and make the selected
object, its provenance, and its result visible together. Improving spacing alone
cannot compensate for ambiguous selection or disappearing drafts.

## Implementation contract (September 12)

Entry is Workbench's research toolbar. Keep the existing interception toggle's
pause/play action visible there, with its existing confirmation and capability
checks. Core remains authoritative for interception state; show pending feedback
and avoid duplicate submissions. Drafts live only in parent-owned memory, scoped
by project and session; warn on page unload while modified. Saved content still
comes from Core. Request selection, session switching, deletion, read retry, tool
navigation, and response-history inspection are applicable. Network streaming and
execution engines are unchanged; their native verification remains outstanding
for the preexisting work. Validate selected component journeys, production build,
and focused desktop/mobile browser coverage; do not run unrelated suites.

## Implemented UI changes and verification

Implemented on the branch, September 12:

- Existing interception pause/play controls appear in the browser toolbar or the
  open research-panel header, with state, pending feedback, and a setup path.
- Repeater now has a request selector and adjacent request/response panes;
  narrow layouts stack them. Save/Send are above the editor. Delete is a quiet,
  accessible icon. Empty assessment chrome collapses for manual tools, while
  existing assessments remain visible. Paired clients use the full panel width.
- Parent-owned, session-scoped memory preserves drafts across research-tool
  navigation. Modified markers, explicit discard, unload warnings, and complete
  session/deletion transitions protect local work. No message content was added
  to browser storage.
- Background fetch failures and action failures have separate state; stale fetch
  completions are ignored. Retained result selection stays stable during polling.
- Pending intercepts and completed history are separated. The basic Forward
  action is outside the editing disclosure. Copy reports missing body content
  and provides an Open in Repeater handoff. Preview truncation is disclosed.
- Retained results do not expose their submitted request revision in the current
  API. The pane explicitly discloses that limitation; this change does not invent
  a revision relationship or add capture/execution capabilities.

Evidence:

- `npm --prefix ui test -- src/components/RepeaterWorkspace.test.tsx`: 12 passed.
  Includes draft switching, remount, session isolation, response inspection,
  error retention, unload warning, discard, deletion, truncation, failed-read
  recovery, and transport presentation. Log: `/tmp/nebula-repeater-components.log`.
- `npm --prefix ui run test:e2e -- tests/interface.spec.ts --grep
  'stabilization manual request workspace preserves drafts and keeps responses visible'`
  with the eight selected projects: 8 passed. Desktop Chromium at 1440/1024;
  emulated mobile Chromium and WebKit at 320/390/430. Covers actual tool entry,
  selection, draft retention across Traffic, response display, focus, geometry,
  and axe checks. Log: `/tmp/nebula-repeater-browser.log`.
- `npm --prefix ui run build`: passed; existing large-chunk advisory remains.
  Log: `/tmp/nebula-repeater-build.log`.
- Browser checks served the new production bundle at
  `http://192.168.1.155:19452`, using mocked API fixtures. This is production/LAN
  presentation evidence, not real-Core persistence or native interception proof.
- Desktop and mobile screenshots inspected. Final desktop screenshot:
  `/tmp/nebula-manual-workspace-desktop.png`; mobile:
  `/tmp/nebula-manual-workspace-mobile.png`.
- Local diff-bound selection receipt validated and reviewed at
  `/tmp/nebula-repeater-selection.json`. No CI receipt was uploaded in this turn.

Outstanding release gates: real-Core/native end-to-end persistence, interruption,
reconnect, and desktop capture behavior for the preexisting native work;
physical-device/software-keyboard and manual screen-reader testing. These were
not exercised. The overall native workflow is partially verified and must not be
presented as complete or superior to another product. The live server was not
updated with this branch.
