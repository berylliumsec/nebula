# Project Host / Docker execution mode

## Startup default (October 2026)

Entry: a fresh installation opens Scratch, or an operator creates a project from
the project switcher. Both start with Host selected. Scratch and API-created
projects require explicit Host acknowledgement before commands can run. The
project switcher's “Create in Host mode” action states the host filesystem and
network boundary and records the operator's acknowledgement in the same
transaction as the project, making that project ready for commands in its linked
folder as the Core OS user without Docker preparation. A project with an existing
saved policy keeps its mode, and an older project without a policy retains the
Docker fallback. Existing command sessions keep their frozen mode.

| Journey | Observable invariant | State authority | Planned test layer |
| --- | --- | --- | --- |
| Discover/create | The creation control names Host access and records acknowledgement atomically; fresh Scratch and API creation stay pending | Core database; UI presents the boundary | Component, setup, API, real Core browser |
| Select/use | Settings show the saved Host consent; a harmless command runs on the host after acknowledgement without Docker setup | Core policy and session snapshot | Runtime, real Core browser |
| Refresh/reconnect | The selected project's mode remains Host after reload and switching away and back | Core policy; URL selects project | Real Core browser |
| Change/retry | Choosing Docker persists for new sessions; an invalid or stale save leaves the prior policy visible | Core policy revision | Runtime, existing settings tests |
| Legacy/delete | Saved modes and older policy-free projects retain their prior behavior; deleting a project removes its policy | Core database | Setup, runtime, API |

Streaming, interrupt, backgrounding, and provider reconnection retain their
existing session-snapshot behavior; this change does not alter those state
transitions. Browser storage does not own execution mode. Planned acceptance uses
the production bundle on a non-loopback LAN origin in desktop Chromium, emulated
mobile Chromium, and emulated mobile WebKit. Physical-device behavior is a
separate gate. Focused test selection excludes unrelated suites.

## Contract before implementation

Entry: Settings > Project execution policy > Command runtime. This historical
contract used Docker as the default. Choosing Host required explicit acknowledgement that commands run as the
Core OS user and can reach host files, devices and the host network. Container VPN
and egress isolation cannot be represented as host guarantees. External MCP
servers keep their configured execution location.

| Journey | Invariant | Authority | Tests |
| --- | --- | --- | --- |
| Discover/select | Both modes are visible with their actual access boundaries | Project policy | UI + browser |
| Save/reload | Mode is durable, revision checked, and scoped to one project | Core DB | API + real Core |
| Start/use | Commands execute in the selected backend; Host needs no Docker image | Session snapshot | Runtime + real process |
| Interrupt/timeout | Child process group is terminated and receipts retained | Runtime manager | Process tests |
| Existing session | Mode never changes silently during a session | Frozen policy | Regression |
| Failure/retry | Unsupported mode/platform and stale saves fail visibly | API/UI | Regression |
| Project switch | Settings and readiness reflect the selected project | Core + UI | Browser |
| Revoke/delete | Mode changes affect new sessions; existing sessions explicitly retain policy | Core | Regression |

Planned gates: focused runtime/API/frontend tests; production build; desktop,
mobile Chromium and mobile WebKit project-setting persistence on LAN; harmless
host sibling-folder read and Docker isolation check. Physical device testing must
be reported separately. Preserve unrelated in-progress UI and credential edits.

## Implemented boundary and evidence

The setting controls the agent command runtime (provider chats, harness gateway
commands and agent missions). External MCP services and the dedicated Docker
Terminal retain their existing runtime ownership. Bounded workspace retrieval
continues to use the linked folder; agents use the command tool for other host
paths. Host mode reports host networking and never claims container egress/VPN
isolation. Existing command sessions retain their frozen policy; a conflicting
new tool contract requires a new session rather than silently changing modes.

- 125 focused Python tests passed; includes host consent, sibling-folder access,
  no Docker preparation, Docker path/network rejection, frozen mode, exact
  approval, real host process timeout cleanup and scope-expiry cancellation.
- Mypy passed for all 91 source files; Ruff and diagnostic audit passed for the
  change. Unrelated pre-existing credential formatting edits were preserved.
- Production UI build passed. Eight permanent production real-Core LAN profiles
  passed: Chromium desktop 1440/1024, Chromium mobile 320/390/430 and WebKit mobile
  320/390/430. All use emulation, not physical phones.
- Installed Grok 1.0.13 through real Core read HOST_AGENT_MARKER_68931 from a
  sibling backup-folder fixture with no configured Docker image; durable command
  records identify Host execution.
- UI suite initially had an updated-copy assertion and an unrelated asynchronous
  WorkbenchBrowser assertion fail. The copy assertion was updated; both affected
  suites then passed (12 tests). Remaining 74 suites passed on the initial run.
- Physical-device acceptance has not been run. No real backup device or existing
  project was switched to Host mode during testing.
