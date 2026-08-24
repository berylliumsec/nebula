# Assistant research continuity contract

## Outcome

An operator can move between Terminal, Code, Files, Notes, Findings, and Assistant
without losing an unsent prompt or replacing previously selected research context.
The Assistant keeps the primary surface calm while making the exact context,
runtime boundary, Core-owned memory state, and recovery actions inspectable.

This pass deliberately builds on Nebula's existing durable transcripts, harness
sessions, approvals, skills, MCP profiles, activity replay, checkpoint rewind,
artifacts, citations, and engagement scope. It does not add a second agent runtime
or duplicate those state authorities in the browser.

## Operator journey

The real entry point is Workbench > Assistant. The operator can:

1. Find or start a conversation and recover its unsent device-local draft.
2. Collect several exact text selections from project surfaces into one context
   pack, inspect each source, and remove individual items before sending.
3. Paste, drop, or choose supported images without leaving the composer.
4. Send a normal turn, or type guidance into the same composer while a steerable
   harness turn is active.
5. Inspect Core's authoritative context-window and compaction state without
   exposing private reasoning.
6. Copy or quote a durable message, fork at a message, and export the authoritative
   transcript with session identity, citations, and attachment hashes.
7. Search saved conversation titles and runtime metadata without hiding the active
   conversation unexpectedly.

## State authorities

- URL: active Workbench view and durable chat-session identity.
- Core database: sessions, messages, lineage, context snapshots, citations,
  attachment metadata, harness activity, and approvals.
- Harness/provider: live turn execution and advertised capabilities.
- Workspace context: transient exact selected-text context pack shared between
  Workbench surfaces; it is hashed only at explicit submission.
- `sessionStorage`: unsent prompt text keyed by project and conversation. Drafts
  survive refresh in the current browser tab but do not become a durable transcript
  or cross-device record.
- Component state: expanded previews, search query, upload progress, and ephemeral
  copy/export feedback.

## Acceptance matrix

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Entry/discovery | Assistant exposes search and context status without crowding the transcript | URL + Core catalog + UI | component + Playwright |
| Draft recovery | An unsent prompt survives refresh and stays isolated by project/session | sessionStorage + URL | unit + Playwright |
| Context collection | New selections append with exact bytes, source identity, bounded length, and stable deduplication | workspace context until submit; Core after submit | unit + Playwright + real Core |
| Image attachment | Choose, paste, and drop share the same validation, capability, count, and upload path | Core artifact store | component + Playwright |
| Create/mutate | Send persists one user/assistant turn; steer mutates only the active advertised harness turn | Core database + harness | real Core |
| Select/use | URL and visible selection identify the same conversation; filters never mutate selection | URL + Core | Playwright |
| Stream/interrupt | Output remains ordered; stop remains available; steer uses visible composer text | Core/harness activity | component + real Core |
| Context health | The meter and inspector reflect Core status, estimated usage, compaction, and memory summary | Core context endpoint | component + real Core |
| Message reuse | Copy is exact; quote creates an editable draft; fork retains durable lineage | Core + transient composer | unit + Playwright + real Core |
| Export | Export is built from a fresh authoritative transcript and records session identity, citations, and attachment hashes | Core database | unit + Playwright |
| Refresh/reconnect | Durable transcript and URL identity return without duplicates; unsent text recovers from the current tab | Core + URL + sessionStorage | real Core |
| Failure/retry | Context, export, upload, and steering failures retain usable chat state and show a local recovery action | Core error contract | component + Playwright |
| Delete/revoke | Existing guarded deletion clears stale selection and its draft key | Core database + URL + sessionStorage | component + real Core |

## Content, responsive, and accessibility invariants

- Context packs collapse to compact source chips and never push the composer outside
  the visual viewport at 320-430 px widths.
- Search, context status, attachment removal, copy, quote, export, stop, and send have
  screen-reader names and visible keyboard focus.
- Touch controls remain at least 44 px; long labels, hashes, transcripts, and memory
  summaries wrap without horizontal clipping.
- Reduced motion does not hide state changes. Approval, user-input, failure, and
  retry controls are never collapsed.
- Sensitive selections remain excluded by the existing selection boundary.
  Context text and unsent attachments are not written to browser storage.

## Competitive gap basis

Current first-party agent products commonly expose multi-source composer mentions,
context-window meters, queued or inline steering, conversation search, checkpoints,
and cross-device supervision. Nebula already has stronger engagement scope,
reviewed execution, durable approvals, artifact receipts, context snapshots, and
harness capability normalization. This pass closes the assistant-ergonomics gap
without weakening those controls.

## Explicit non-goals

- No hidden auto-approval, unrestricted shell, or background cloud execution.
- No browser-owned transcript, lineage, context memory, or evidence authority.
- No capture or display of private chain-of-thought.
- No opaque folder identifiers or vendor-specific assistant fork.
- No claim of physical-device or LAN support until those gates are exercised.
