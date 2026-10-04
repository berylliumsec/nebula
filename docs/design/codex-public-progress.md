# Codex public progress in chat

Entry: a chat turn using the Codex harness. When Codex sends public commentary,
the latest update must be visible in the running turn without opening the activity
log. Completed reasoning items count as private thinking episodes; they must not
be presented as readable updates when Codex supplies no summary text.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Select and use | The chosen Codex session and turn own the displayed progress | URL, Core session and turn | existing chat journey |
| Stream | The newest public commentary appears beside the running work status; private reasoning remains undisclosed | harness event, Core event ledger | component, browser, real Core |
| Interrupt and complete | The latest update stays in the saved activity log and the receipt counts public updates separately from thinking episodes | Core event ledger | browser, real Core |
| Refresh and reconnect | Replayed events restore the public update without duplication | Core event ledger | browser, real Core |
| Missing summary | Empty Codex summaries do not create an empty list of supposed readable updates | Codex item state | component, browser |
| Failure and retry | Existing activity retry and turn retry keep their current recovery controls | Core status and UI | existing browser journey |

No new durable state is needed. Component state owns only the existing disclosure.
Deletion and fork keep their existing activity ownership; the preview is derived
from the selected turn's events. Planned selection: focused activity-ledger and
thinking component tests, existing Codex mock browser journey across desktop
Chromium and emulated mobile Chromium/WebKit, and the relevant real-Core journey.
