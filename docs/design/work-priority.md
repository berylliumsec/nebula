# Work priority

The operator marks a chat or a whole project **high priority** instead of
telling Butler. Wherever work waits for shared capacity, high-priority work is
taken first. Nothing already running is interrupted and no capacity is set
aside, so normal work runs at full speed when nothing high is waiting.

## Setting it

| Target | How | Stored as |
|---|---|---|
| Chat | `PATCH /api/v1/chat-sessions/{id}` with `priority: "high" \| "normal"` (allowed while a response runs) | `ChatSession.metadata.priority = "high"` |
| Project | `PUT /api/v1/engagements/{id}/priority` with `priority` | `Engagement.metadata.priority = "high"` |
| Butler | `nebula.work.set_priority` with `priority` and an optional `session_id`; no confirmation | the same routes |

A chat is high when it, the chat it reports to (through subagent and
goal-child links), or its project is marked. Forks start at normal priority.
`work_priority.is_high` answers this and caches it for five seconds per
process; a change through Core's routes clears the cache.

## Where it applies

| Waiting point | Rule |
|---|---|
| Host-wide provider, background and tool slots (`RuntimeCapacity`) | A high request that does not fit records a `waiting` reservation row, refreshed on every poll. A normal request waits while taking its share would leave a fresh high waiter without room. A waiter unseen for 10 s stops counting and is removed. |
| Host pressure line (`HostLoadGate`) | High waiters queue ahead of normal ones, each group first in, first out. Provider and harness turns. |
| Host command queue | High commands (label `priority: high`) come first, then the existing fewest-running-project, oldest-first order. |
| Project worker admission | Projects that are high, or hold a high chat, get a worker first and are never the idle worker retired to make room. |
| Butler | Work reads show `priority` and list high chats first; the Butler board puts high chats (or chats in high projects) first in each section. |

Not changed, because chats do not compete there: per-chat follow-up queues,
per-project command lanes, goal dispatch, the per-parent subagent limit, and the
per-process semaphores inside one project worker.
