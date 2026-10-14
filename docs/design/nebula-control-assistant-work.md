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

The tool boundary permits only typed `nebula.work.*` actions on the selected
project. It never gives the control assistant arbitrary filesystem, MCP, web,
browser companion, or general chat tools. Ordinary chats do not receive these
work tools. Browser and notes control are outside this slice. A read result is a
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
