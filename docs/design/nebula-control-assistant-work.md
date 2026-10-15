# Butler phase 1: reactive project control

Butler is the selected project's second-in-command. The operator opens its
persistent text panel or full-screen Voice view and asks it to inspect or change
work. It can act on that request while other chats and missions keep running in
Core. Proactive monitoring and unsolicited actions belong to phase 2.

## Capability contract

The provider and harness receive the same four tools:

1. `nebula.capabilities.search` finds a project action by intent and returns a
   short description. An empty search lists categories.
2. `nebula.capabilities.describe` returns one action's exact JSON schema and
   whether it is a read or change.
3. `nebula.capabilities.read` runs a described read against Core.
4. `nebula.capabilities.change` runs a described change after an operator
   request. Core's existing handler enforces project ownership, policy, and
   revision checks.

This keeps dozens of action schemas out of every model request. The registry
contains the existing help, settings, navigation, chat, subagent, goal, queue,
schedule, approval, and mission handlers. Ordinary chats do not receive these
four tools. Each action description explains its effects and limitations; the
short system prompt says how to discover and invoke an action, how to read back
state, and how to report a pending action without calling it complete.

For example, to direct a chat, Butler searches for chat actions, describes
`nebula.work.overview` and `nebula.work.chat`, reads the project roster and
target chat, then describes `nebula.work.message_chat` and calls change with
the exact session ID and text. `delivery=next` queues a durable follow-up;
`delivery=steer` guides an active harness turn. The returned status is reported
as queued or active until a later Core read shows completion.

## Operator journey and authorities

| Step | Observable invariant | Authority | Focused evidence |
| --- | --- | --- | --- |
| Open | One Butler panel and a usable Voice button appear on desktop and phone; the phone composer and header are centered | Browser UI | Component, desktop and mobile browser |
| Discover | Butler can find an action and its exact arguments without guessing IDs | Core capability registry | Python and real-Core model request |
| Select | A named main chat, delegated subagent, goal child, or mission resolves within the selected project | Core records | Python and real-Core browser |
| Inspect | Status, pending work, exact target, revision, and recent output come from Core | Core database | Python and real-Core browser |
| Change | Start, message, steer, stop, fork, delete, retry, rename, archive, settings, goals, queues, schedules, and approval actions use existing Core paths | Core service and policy | Python and selected real-Core browser |
| Read back | A saved change returns its durable ID, status, and link; queued, started, pending browser action, and completed remain distinct | Core and requesting browser | Python, component, real-Core browser |
| Recover | A failed or stale action leaves prior state intact and gives the next valid step | Core errors and UI | Python, browser |
| Reconnect | Butler history and delegated work are reloaded from Core without replaying mutations | Core database | Real-Core LAN browser |

The URL owns navigation, Core owns project records and work execution, and the
browser owns only panel presentation and device actions such as opening a page
or changing its theme. Browser actions are pending until that browser applies
and confirms them. A saved Assistant setting applies to the next message, not
to a running or queued turn. Deleting a chat requires its exact saved title and
revision; a retry returns a saved linked retry instead of starting duplicate
work. Secret answers remain in the private chat or mission UI.

The reactive phase includes main chats, subagents, missions, goals, approvals,
questions, follow-up queues, schedules, and per-chat Assistant settings in the
selected project. Managed Assistant browser automation and notes control are
deferred. Physical iPhone microphone and speaker acceptance is a separate
gate from mobile browser emulation. The installed iPhone shell mounts Butler;
Voice requires a trusted HTTPS origin and a configured OpenAI Realtime key.
