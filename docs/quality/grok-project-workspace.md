# Grok linked project workspace

Contract recorded before implementation: select a linked project in the project
switcher, open or resume a Grok chat, and inspect an existing file through the
Nebula gateway. The saved Engagement workspace_path owns the folder identity;
Core owns the gateway and session permissions; Docker exposes the resolved folder
at /workspace. The vendor launch directory is private scratch, not project data.

| Journey | Invariant | Authority | Validation |
| --- | --- | --- | --- |
| Discover/select | Selected project resolves to its saved folder | Engagement | Existing workspace tests |
| Create/use | Grok receives authenticated gateway with file and command tools | Core adapter | Adapter regression and live smoke |
| Refresh/reconnect | Resumed sessions receive the same gateway contract | Core adapter | Resume regression |
| Failure/retry | Invalid gateway transport fails explicitly | Adapter | Regression |
| Stream/interrupt | Existing event/cancellation behavior retained | Harness | Adapter suite |
| Delete/revoke | Existing gateway policy remains authoritative | Core | No policy changes |

No layout or browser interaction changes. Production browser, real-Core and
physical-device results must be reported separately from adapter fixtures.

## Findings and validation

Grok's adapter omitted the Core gateway from ACP session/new and session/load.
After connecting it, Grok 1.0.13 rejected dotted tool names. Grok now receives
stable portable aliases, mapped back to canonical names before Core dispatch;
unknown or ambiguous names fail closed. New and resumed connections receive the
workspace instructions and trusted catalog on every turn. Other adapters retain
their existing tool names. The private launch directory remains isolated and stable under the artifact data root. Legacy Grok chats retain their transcript and receive a fresh runtime session with a bounded conversation handoff on their next message.

- Adapter and Core regressions: 78 passed, including authenticated Unix IPC
  reading an existing project file and rejecting an unknown alias.
- Sandbox and automation runtime: 76 passed; setup coverage passed separately.
- Installed Grok 1.0.13, model grok-4.6: authenticated gateway read of a temporary
  project marker returned its exact content (NEBULA_LINKED_FOLDER_73921).
- Docker with the prepared pinned image: temporary host file read at /workspace;
  container write was immediately visible in the same host directory. No network.
- Ruff, mypy and diagnostic audit passed; production UI build passed.

Physical-device and full live operator-chat acceptance have not been exercised.
The initial real-agent smoke used a controlled file-only gateway. A subsequent
real Core + installed Grok check read a project marker, saved its answer, shut
down the runtime, resumed the same native session and chat, then read a second
marker and saved that answer. Both turns completed. This does not claim a full
production browser-chat or physical-device workflow.
- Production UI / real Core on non-loopback LAN: host folder browser, project
  creation, saved workspace_path and reload selection passed in desktop Chromium
  at 1440x900 (`real-core`, one test). Mobile browsers were not rerun for this
  backend-only change; this is not full mobile-chat acceptance evidence.
