# Main-agent messaging

Independent main conversations can coordinate inside one project without being
recast as a parent/subagent tree. This is a separate capability from native
subagent messaging: neither side owns, starts, stops, or budgets the other.

Implementation-aligned mockups (September 22, 2026), pending operator design
approval, are on the Figma page "Subagents" in
https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=21-2:

- `A1 · Desktop · Agent messaging enabled` (`124:7`)
- `A2 · Desktop · Active peer message delivered` (`124:46`)
- `A3 · Mobile 390 · Peer message next turn` (`124:87`)
- `A4 · Agent messaging lifecycle and boundaries` (`124:113`)

The frames use Nebula semantic color variables, Geist typography, and the
subscribed `Switch Field` component adapted to Nebula typography. Visual audit:
no missing fonts, placeholders, or child overflow in the four frames.

## Operator journey and invariants

1. The operator explicitly enables **Agent messaging** in Assistant settings.
2. The main agent can list other saved, opted-in main conversations in the same
   project and sees whether each is active or idle.
3. It sends a concise, self-contained message to a returned session ID.
4. Core writes one durable `ChatAgentMessage` and one visible system transcript
   entry in the recipient conversation in the same transaction.
5. An active provider turn receives unread messages before its next routing
   step. A steerable active harness receives the message as guidance. If the
   active turn cannot accept it, or the peer is idle, the message remains queued
   for the next turn; sending never starts an idle peer.
6. The recipient transcript labels the sender as a **main agent**. The recipient
   can reply through the same scoped capability.

Messages never cross project ownership. Agents cannot discover or address
themselves, subagents, temporary assistants, archived conversations, or peers
that have disabled the setting. An unavailable or out-of-scope target produces
the same `unknown peer agent session_id` response so hidden sessions are not
disclosed.

## State authorities

- `ChatSession.metadata.allow_agent_messaging` is the durable operator choice.
- `ChatAgentMessage` is delivery authority (`pending` to `delivered`).
- The recipient `ChatMessage` with `metadata.kind = "agent_message"` is the
  operator-visible transcript authority and the next-turn model context.
- `ChatTurn` and `HarnessTurn` determine whether delivery can join an active
  turn; they do not own the message itself.
- Idempotency is scoped by sender and invocation key. A retry cannot create a
  second message or transcript entry.

## Lifecycle and failure coverage

| Transition | Visible result | Recovery rule |
| --- | --- | --- |
| Off to on | Setting remains checked after refresh | Session metadata is reloaded from Core |
| Discover | Only eligible project peers appear | Hidden targets are never named in errors |
| Send to active provider | Peer message is injected before the next tool step, in numbered parts when too long for one 8 KiB result | A message arriving after history load stays pending for routing; it is marked once the step is saved |
| Send to active harness | Current turn is steered when supported | Failed/unavailable steering leaves the durable message queued |
| Send to idle peer | Transcript callout appears; peer remains idle | Next turn receives the exact queued snapshot; a harness marks it once the vendor accepted that prompt, so a failed start or its retry keeps it |
| Retry/reconnect | One message and one transcript entry | Stable sender-scoped idempotency key |
| Disable/archive | Conversation disappears from future discovery | Already-sent transcript entries remain visible |
| Delete sender | Recipient retains content and sender ID | UI falls back to `Deleted conversation` for unread delivery |

## Capability names

Provider chats receive `list_agents`, `send_agent_message`, and
`read_agent_messages`. Harness chats receive the equivalent fixed gateway
catalog `agent.list`, `agent.send`, and `agent.read`; changing the opt-in reopens
the vendor connection between turns so its advertised tools and developer
instructions remain coherent.

