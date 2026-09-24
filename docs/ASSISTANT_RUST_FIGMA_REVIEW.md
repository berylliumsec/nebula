# Assistant Rust rewrite: Figma journey review

Reviewed live on September 23, 2026. **The rewrite remains incomplete.** Figma
inspection establishes design requirements; it cannot establish API, persistence,
execution, performance, or production-browser parity.

Read 21 pages of the [Assistant design file](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7),
including their frame identities, text labels and descendant prototype reactions.
Visually inspected the desktop `4:73` export at 1440 × 1121 and the mobile
`7:113` export at 320 × 844. The [review receipt](../assistant-rs/compatibility/figma-review.json)
records page/frame identities, dimensions and the limits of this inspection.
Shared settings and workbench references were inspected as Assistant dependencies;
their implementation remains outside this change.

Also read the [recovery lifecycle board](https://www.figma.com/board/aoQcmstUOUo3n11Cg8YKXf)
and located the [quiet Assistant controls](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=16-2).
The board contains historical incident diagrams as well as proposed states; it is
not proof that a state machine is implemented. The inspected design pages returned
no flow starting points or descendant reactions. No interactive prototype
clickthrough was performed. No design files were changed.

## Journey-to-contract map

Every row still needs real Rust-backed production UI acceptance. The final column
distinguishes isolated API evidence from outstanding production acceptance.

| Journey and Figma reference | Authority and invariant | Rust status / remaining evidence |
| --- | --- | --- |
| [Main chat](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=4-73): create, select, reload | Core session/message identity and URL agree; provider choice survives | SQLite transactions and Python interoperability exist; session/message catalogs and identity reads have 64-case differential HTTP evidence. Create, full selection dependencies and production UI parity pending |
| [Archive](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=52-3): archive, open, send, unarchive | Archived metadata remains durable; sending restores the same chat | Archive metadata and catalog visibility are retained; archive mutations, schedule pause/resume and production UI behavior pending |
| [Edit in place](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=78-39): edit, resend, expand replaced history | Old messages remain retained but are excluded from current transcript/context | Record/reference preservation and retraction predicate ported; rewind transaction, retry and UI pending |
| [Elapsed time](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=81-128): stream, stop, inspect usage | Durable elapsed and approval-wait time remain distinct from tokens | Stored timing fields preserved; producer timing and UI pending |
| [Compact controls](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=16-2): search, bookmark, catch up | Core messages/bookmarks/cursors are authoritative; pending actions stay visible | Rust transcript/search/bookmark services and authenticated HTTP are implemented with a 112-case Python navigation oracle; cursor HTTP has isolated authentication/durability evidence. Catch-up and turn-summary reads have 78-case differential HTTP evidence, including acknowledgment without hiding pending actions. Shipped integration and production UI parity pending |
| [Session details](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=4-77): goal, limits, skills, checkpoints, fork | Durable goals and lineage; frozen permissions; restore cannot overwrite newer edits | Goal/child/schedule and raw catalog reads have a 153-case Python HTTP oracle; response-only live elapsed preserves saved usage and lineage. Mutations, execution, remaining dependencies and production UI pending |
| [Subagents](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=21-2): inspect, wait, approve, stop | Children retain parent identity; waits release execution capacity; pending approval is visible | Retained child views have a 73-case Python HTTP/header oracle covering recovery, approvals, questions, usage and elapsed time. Isolated queue policy exists; durable admission, actions, delivery and execution pending |
| [Nested chats](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=120-85): select child, open parent | Sidebar and banner preserve the parent/child relationship after refresh | Canonical IDs retained; snapshot projection and UI integration pending |
| [Peer messaging](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=124-113): enable, discover, send, receive | Explicit opt-in, same project, one durable message/transcript entry, no idle-peer wake-up | Record endpoint/delivery invariants ported; transactional delivery and authorization pending |
| [Action states](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=4-19): approval, failure, retry, restart | Decisions remain visible; trustworthy receipts settle effects; unknown outcomes are not replayed | State GET preserves pending controls, expiry, first progress, passive connection state and durable display revisions with 81 HTTP comparisons plus a writer-phase fault. Receipt adoption, full recovery, cancellation and production UI remain pending |
| [Agent view](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=97-3): follow, pin, minimize, dock | Saved Results own snapshots; device preferences own panel placement | Shared structured-results projections remain pending. The separate chat Results drawer has retained-output/context-source reads and bounded file-diff previews; these do not implement Agent view |
| [Side terminal](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=44-3) and [environment](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=57-2) | Workspace/session references stay attached to the selected host and project | Helper, artifact and permission contracts remain dependencies; no terminal/environment area edits |
| [Runtime/tool selections](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=101-3) | Existing provider/MCP catalogs remain discoverable; opt-ins and disclosure persist | Shared settings remain unchanged; Assistant consumption, auth and bounded helpers pending |
| [320 px chat](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=7-113) and [iPhone shell](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=64-6) | Composer, interruption and approvals remain reachable; reconnect retains content | Design export inspected; production Chromium/WebKit, LAN, keyboard and device gates pending |

The separate chat Results drawer in `ui/src/components/ChatResults.tsx` consumes
`/chat/sessions/{id}/results`; it is not the shared structured-results API used
by Agent view. Its Rust projections retain source-message navigation identities,
paging and excerpt content for the unsent context pack. The 110-case Python HTTP
oracle covers stored outputs, missing previews and context-policy variants.
Production drawer, source navigation, draft preservation and reconnect acceptance
remain open; no new Figma inspection or browser run is implied by this increment.

Activity, saved follow-up queue and recorded hook summaries now have a 40-case
isolated Python/Rust HTTP comparison. Sidebar activity preserves hidden-session
conflict detection and pending recovery markers; an absent queue remains unsaved.
These reads support main-chat, nested-chat and recovery displays but do not
implement queue dispatch, receipt reconciliation or production UI acceptance.
Malformed hook-schema error details still have a documented parity gap.

Goal and child-goal reads now preserve response-only active-time calculation,
stored child elapsed, lineage, counters and claims. Schedule reads retain saved
configuration even when the provider no longer exists. The 153-case retained-plan
oracle also covers raw goal/usage/schedule/subagent catalogs and error namespaces.
No new Figma visit or browser run is implied by this increment. Existing UI
`ProviderGoalPanel.activeSeconds` adds active time already included by the GET,
and `ProviderGoalChildren` hides child-read errors as an empty list. These are
unresolved production-journey blockers, not behaviors proved correct by API parity.

The retained subagent view now returns child references, recorded/live usage,
questions, approvals and recovery display without changing records or settling
receipts. Its 73-case oracle preserves 191 Assistant and six Approval records,
including conditional missing-reference behavior and the no-store header. The
current client drops question, reasoning effort and rounds; read parity does not
resolve that visibility gap or prove approve/reply/stop/open-child journeys.
Those actions and the production matrix remain outstanding.

## Resolve conflicting design generations

- Recovery frame `7:5` says the operator must reconcile and resume. The current
  [recovery contract](design/execution-recovery-lifecycle.md) supersedes it: use
  durable receipts, preserve unknown outcomes, and retain safe automatic recovery.
  Do not reintroduce per-agent recovery clicks from the old mockup.
- Harness notes `108:446` describe three concurrent children and six per response.
  [Later limits designs](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=114-2)
  remove that default and preserve an optional conversation limit. A host's
  admission/execution bound is a separate resource constraint. Queued work must
  remain distinguishable from executing work, without changing legacy status enums.
- Main-agent messaging frames describe peers, not children. Sending a message to
  an idle peer must not start it or disclose hidden/cross-project conversations.
- The edit journey retains old messages behind a disclosure. Deleting them or
  feeding them back into current model context would violate that contract.

## Acceptance work still required

Use the selected existing journeys in `ui/tests/real-core.spec.ts`,
`ui/tests/chat-reconnection.spec.ts`, and `ui/tests/catch-up.spec.ts` as references.
They currently target Python Core; their historical passes do not transfer to
Rust. The real-Core spec includes exact tests for conversation switching, in-place
editing, provider choices, goal-before-first-turn, approvals, failure/no-replay,
restart, and fork/discard. Select only the changed journeys with a new diff-bound
receipt when a Rust server can exercise them.

Required production matrix: desktop Chromium 1440/1024; mobile Chromium and WebKit
320/390/430; non-loopback LAN for auth/cookies/CSRF/SSE/WebSocket behavior. Include
loading, empty, error/retry, background/reconnect, long content, keyboard/touch,
labels, focus, reduced motion, revocation and deletion. Label emulation separately
from physical devices. None of these product gates passed on Rust in this review.


Session-state reads now preserve the revision authority consumed by
`useSessionState.ts`, including approval expiry, pending questions, interruption
capability and passive transport observations. Unchanged reads avoid the writer;
changed reads re-project under the write boundary before assigning a revision.
The 81-case API oracle and writer-phase fault verify the retained state and error
contracts. These are isolated API checks, not production reconnect, keyboard,
mobile, LAN or physical-device evidence. No new Figma visit is implied here.


The pending-turn and session-hook API port follows the later durable-recovery
contract: saved terminal receipts can repair interrupted display state, while
uncertain outcomes stay visible. It preserves the conversation-selection and
reconnect entry points used by `SessionsPage.tsx`. This tranche adds isolated
storage/API evidence only. Automatic continuation, real reconnect and the
production browser/LAN/device journeys above still require end-to-end proof.
The previously inspected Figma frames remain the design reference; no new live
Figma inspection occurred in this tranche.
