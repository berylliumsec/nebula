# Conversation cleanup: parent and subagent views

Status: approved for implementation on 2026-09-25. Mockup text and counts are illustrative; they are not claims about a live Atlas run.

## Purpose

An operator should be able to find the current state or final answer in one scan, then open the work history when needed. The current transcript renders every streamed commentary item as full body content in `ChatTranscriptRow`, while thinking and the activity ledger have separate disclosures. In a long run, those updates dominate the conversation. Parent conversations also need a clear summary of child work and any request that the parent owns.

Keep the full durable record. Change its default presentation.

## Mockups

| Journey | Figma | Local preview |
| --- | --- | --- |
| Subagent running: one current status, activity behind a disclosure | [Desktop running](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=2-2) | [PNG](conversation-cleanup-mockups/subagent-running-desktop.png) |
| Subagent complete: answer and evidence first, work history second | [Desktop complete](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=2-3) | [PNG](conversation-cleanup-mockups/subagent-complete-desktop.png) |
| Subagent input: decision stays visible at a narrow width | [Mobile action](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=2-4) | [PNG](conversation-cleanup-mockups/subagent-action-mobile.png) |
| Parent running: parent progress, child counts, and an owned request | [Desktop running](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=6-2) | [PNG](conversation-cleanup-mockups/parent-running-desktop.png) |
| Parent complete: consolidated answer with child reports one click away | [Desktop complete](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=6-32) | [PNG](conversation-cleanup-mockups/parent-complete-desktop.png) |
| Parent mobile: child request reaches the supervisor without hiding the composer | [Mobile action](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=6-65) | [PNG](conversation-cleanup-mockups/parent-action-mobile.png) |

The frames use Nebula's dark palette and Geist font. They show layout and information hierarchy, not final copy, a new theme, or verified runtime data. Figma has no Nebula component library or Code Connect mappings in this file; implementation should reuse the existing React components and tokens.

## Operator contract

1. **Running:** The turn has one concise live status containing the current work, meaningful progress, elapsed time, and a control to inspect the saved steps. The latest commentary remains reachable without reading every update in the transcript.
2. **Completed:** The answer stays in the transcript at full fidelity. Result or evidence links follow the relevant claim. Work history starts collapsed with an accurate duration and step count.
3. **Needs action:** Approval, user input, failure, and retry controls remain visible in the transcript. A disclosure never hides an unresolved request or the action needed to continue.
4. **Parent:** Child counts and status remain discoverable without duplicating every child update in the parent transcript. A child report is attributed to that child and opens the child's conversation. A child request that requires the operator is presented as a parent-owned action with a direct path to resolve it.
5. **Subagent:** Its parent relationship and route back to the parent stay clear. Its own answer, work history, and pending actions use the same presentation rules as the parent.
6. **Mobile:** The current state, action buttons, and composer remain reachable at 320–430 px with no horizontal clipping or hover-only controls.

Core owns the transcript, activity, child status, requests, and answers. The URL owns conversation selection. React may own disclosure state and unsent input; it must not become a second authority for progress or completion. Reload, reconnect, and history replay must reconstruct the same visible state from Core.

## Implementation acceptance contract

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Open parent or child | The selected conversation and its relationship are visible | URL plus Core session | Component and browser |
| Stream | One current status is visible; every saved commentary update remains reachable | Core activity and transcript | Component and browser |
| Interrupt or resume | Stop, pending work, and recovery remain visible and usable | Core turn and queue state | Real Core browser |
| Complete | The answer leads; result links and saved work remain reachable | Core message and activity | Component and real Core browser |
| Child needs action | Parent shows the request and links to the authoritative decision | Core child approval | Component and real Core browser |
| Refresh or reconnect | No duplicated commentary, child status, or lost decision | Core plus URL | Real Core browser |
| Failure and retry | The failed operation and next valid action remain visible | Core error contract | Component and real Core browser |
| Delete or revoke | Existing conversation lifecycle behavior remains intact | Core | Existing focused coverage; presentation change does not alter deletion |

## Implementation sequence after design review

### 1. Unify the work presentation

- In `ui/src/pages/SessionsPage.tsx`, replace the always-expanded `assistant-commentary` list with a compact turn status. Build it from the current turn's authoritative activity and commentary. Preserve every saved commentary item in the expandable work detail.
- Use the existing `ActivityLedger` and thinking disclosures instead of adding a second activity feed. Show one count and one current step; do not fabricate progress from an elapsed timer or merge unrelated child and parent steps.
- Keep final answer text, citations, evidence, tool failures, approval cards, and input requests outside collapsed activity.

### 2. Make parent and child roles legible

- Keep `ChatSubagentRail` as the compact parent entry to child status; show counts only once near the relevant work.
- Keep `ChatSubagentResultCard` attributed and linked to its child conversation. Present a short report preview after the parent's answer, with full report and child history available on demand.
- Surface supervisor-owned child requests in the parent transcript with a labeled action. Resolve the request through the existing authoritative interaction path; do not create a separate parent-only decision state.
- Preserve the subagent's parent link and shared-files context in a quiet line above its transcript.

### 3. Reduce chrome without losing controls

- Tighten the running card and align status, message, and composer to the same reading track.
- Keep the runtime, stop/send, and pending-action controls discoverable. Move only infrequent secondary controls into a named overflow menu if their current access path remains one step and works by keyboard and touch.
- Use familiar named icons, visible focus, tooltips, and at least 44 px touch targets for implemented controls. Long titles and model names should truncate without pushing actions off screen.

### 4. Verify the exact journeys

- Record a diff-bound, feature-specific selection in `.github/test-selection.json` before running tests. Select component/reducer cases for running, completion, replay, parent/child status, pending action, long text, and failure/retry. Do not run a full suite by default.
- Exercise one continuous conversation in a production bundle with real Core: send, stream, inspect steps, complete, open result, switch parent/child, reload, reconnect, and retry a recoverable failure.
- Run the selected browser journeys in desktop Chromium and the existing mobile Chromium/WebKit profiles at 320, 390, and 430 px. Check keyboard, touch, labels, focus, reduced motion, scroll anchoring, and the focused composer.
- Use a non-loopback LAN origin for origin-sensitive behavior. Report separately whether a physical mobile device and live provider turn were exercised.

## Review decisions

- Confirm that the status card contains enough live information without exposing repeated process narration.
- Confirm that the parent action card is prominent enough and that child counts are not repeated needlessly.
- Confirm the result card and activity disclosure order in the completed view.

Implementation begins after this mockup review. No acceptance or deployment claim follows from these static frames.
