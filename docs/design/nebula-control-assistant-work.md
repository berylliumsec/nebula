# Nebula assistant work controls

Entry: open the persistent Nebula assistant in the shared browser shell with a
project selected. The conversation remains separate from ordinary agent chats.
An operator can ask what is running, start an assistant chat or mission, send
guidance to an existing chat or mission, and stop work. The assistant uses Core
owned tool calls, so a browser disconnect does not own the delegated work.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | The assistant names chats and missions as available work controls and can list configured runtimes | UI copy and Core catalog | Component and browser |
| Select | The selected project is fixed by the control conversation; a named target is resolved from a bounded project roster | Core project and entity records | Unit and real Core |
| Inspect | A bounded overview and detail read show current status, recent output, pending actions, and exact IDs without exposing credentials | Core sessions, turns, runs and queue | Unit and real Core |
| Start | A new chat or mission uses an enabled, listed runtime and returns its saved identity and current status | Core chat or mission service | Real Core |
| Guide | Sending to a chat queues a durable follow-up; live harness steering applies only to the selected active turn. Mission steering uses the existing mission API | Core queue and harness or mission service | Real Core |
| Interrupt | Stop targets only the selected chat turn or mission, then reads back its state | Core turn or run | Real Core |
| Refresh and reconnect | The work roster and control transcript return from Core; a queued message or active run is not recreated | Core database; browser owns only panel state | Real Core and browser |
| Failure and retry | A failed or ambiguous action remains visible, identifies its target, and gives a safe next step | Core errors and tool result | Unit and browser |
| Delete or revoke | No delete or revoke tool is included in this slice; existing product controls retain those transitions | Core | Not applicable: no delete action added |

The initial tool boundary permits only typed `nebula.work.*` actions on the selected
project. Ordinary chats do not receive these work tools. Browser and notes
control are outside this slice. A read result is a
snapshot, and a submitted message or started run is reported with its observed
Core status, not as completed work. Non-idempotent starts are not retried by the
assistant; each queued chat follow-up carries its own idempotency key.

Lifecycle: discover, select, inspect, start, guide, interrupt, refresh, reconnect,
failure and retry are required. Background work is required and Core owned.
Forking and deletion remain in their existing product workflows. The installed
iPhone shell does not mount this assistant; browser phone layouts do.

Planned verification: focused tool and service tests for authorization, project
binding, roster, queue, start, steering, stop and read-back; provider and harness
tool boundary tests; component tests for discoverability and browser action
behavior; selected Playwright desktop Chromium and mobile Chromium/WebKit cases;
production build and isolated real-Core LAN journey with synthetic data.

## Per-chat assistant settings expansion

Entry: an operator opens Nebula assistant settings from the persistent panel,
or asks Nebula assistant to inspect or change another project chat's Assistant
settings. The panel links to the same full chat settings controls used by
ordinary conversations. A chat can be chosen from a named project roster; an
opaque ID is not required. These choices affect the next message, not an
already queued or running turn.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Nebula assistant exposes its own Assistant settings and can list configurable project chats | UI and Core catalog | Component and browser |
| Select | A named target resolves to one saved chat in the selected project; ambiguous names require clarification | Core ChatSession | Unit and real Core |
| Inspect | The assistant reports the saved runtime, model, effort, speed, mode, cyber access, knowledge, skills, subagents, messaging, and MCP choices with availability reasons | Core chat, harness and capability catalogs | Unit and real Core |
| Change | Every supplied choice is validated against the enabled profile and project catalog, saved with a revision guard, then read back | Core ChatSession and profile catalogs | Unit and real Core |
| Use | The next visible UI turn uses those saved choices, while existing approvals and tool policies still govern execution | Core request, provider or harness session | Real Core and browser |
| Refresh and reconnect | The same settings and chat identity return after reload; a stale UI value does not overwrite a newer Core change | Core ChatSession plus URL | Real Core and browser |
| Failure and retry | A conflict or unsupported choice explains the valid next action and leaves the prior settings intact | Core error contract | Unit and browser |
| Delete or revoke | Deleting a chat removes its settings; disabled profiles and MCP servers cannot be newly selected | Core ChatSession and catalogs | Real Core |

The special product-control tools remain available only to the control
conversation. General command, MCP, knowledge, skill, subagent and messaging
capabilities are opt-in per conversation and run under their normal policy and
approval path. A product-control tool's policy repair exception must not become
a blanket permission bypass for general tools. The selected project binds every
target and runtime; a cross-project chat or MCP server is never accepted.

The URL owns full-chat navigation, Core owns saved conversation settings and
transcripts, provider and harness sessions own actual execution, and React owns
only unsent selections and panel presentation. Browser storage can restore an
unsent first-turn preference but cannot override a saved Core choice. Discover,
select, inspect, change, use, stream, interrupt, refresh, reconnect, failure and
retry are required. Forking follows the existing chat workflow. Physical phone
testing remains a separate gate from mobile browser emulation.
