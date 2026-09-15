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
| Mobile/keyboard | Floating dialog fits small screens; focus contained/restored; controls reachable | native dialog/CSS | Chromium/WebKit production |

No split pane, automatic main-chat submission, or saved sidebar entry. Backgrounding
keeps the popup within its lease; closing, navigation and reload discard it. Tests
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
one hour after creation, checking once per minute while running. Normal service
audit retention is unchanged; this is a disposable chat, not a zero-retention
inference mode. Nothing from the popup is promoted to the main conversation.
