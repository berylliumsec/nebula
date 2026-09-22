"""Durable, project-scoped messaging between independent main chat agents.

Peer messaging is separate from parent/child subagent messaging. A main agent
can discover only non-subagent conversations in its own project, send a bounded
message, and read messages addressed to it. Core writes each incoming message
to the recipient transcript for operator visibility and next-turn delivery.
When the recipient is already working, Core injects the unread inbox before the
provider's next routing step or steers a supported harness turn.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Collection, Iterable
from uuid import NAMESPACE_URL, uuid5

from .chat_subagents import is_subagent_session
from .domain import (
    ChatAgentMessage,
    ChatAgentMessageStatus,
    ChatBackend,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from .runtime_platform import RuntimeToolComponents
from .storage import ConflictError, NotFoundError
from .tools import InvalidToolArguments, ToolExecutionResult, ToolInvocation, ToolSpec

if TYPE_CHECKING:
    from .storage import NebulaStore


AGENT_MESSAGE_TOOL_NAMES = frozenset(
    {"list_agents", "send_agent_message", "read_agent_messages"}
)
AGENT_MESSAGE_ROUTING_INSTRUCTIONS = """

Peer agents: list_agents discovers other independent main conversations in this
project. send_agent_message shares a concise finding, request, or coordination
decision with one of them. read_agent_messages returns messages they sent you;
Core also delivers unread messages before your next tool step. Agents cannot
message subagents, archived conversations, temporary assistants, themselves, or
conversations in another project. Do not send progress chatter. Sending a
message does not start an idle agent; it receives the message on its next turn."""

_UNFINISHED = {
    ChatTurnStatus.ROUTING,
    ChatTurnStatus.WAITING_APPROVAL,
    ChatTurnStatus.WAITING_CALLBACK,
    ChatTurnStatus.FINALIZING,
}


class AgentMessageService:
    """Own peer discovery, durable delivery, and active-turn notification."""

    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.harness_steer: Callable[[str, str], Awaitable[bool]] | None = None

    @staticmethod
    def _eligible(session: ChatSession) -> bool:
        return not (
            is_subagent_session(session)
            or session.metadata.get("temporary_assistant") is True
            or isinstance(session.metadata.get("archived_at"), str)
        )

    @classmethod
    def _addressable(cls, session: ChatSession) -> bool:
        return (
            cls._eligible(session)
            and session.metadata.get("allow_agent_messaging") is True
        )

    def _invoking_session(self, invocation: ToolInvocation) -> ChatSession:
        if not invocation.chat_session_id:
            raise InvalidToolArguments("agent messaging requires a chat session")
        session = self.store.get(ChatSession, invocation.chat_session_id)
        if not self._addressable(session):
            raise InvalidToolArguments(
                "agent messaging is turned off for this conversation"
            )
        if session.engagement_id != invocation.engagement_id:
            raise InvalidToolArguments("agent messaging is limited to this project")
        return session

    def peers(self, session: ChatSession) -> list[dict[str, Any]]:
        turns = self.store.find_entities(
            ChatTurn,
            {"status": [item.value for item in _UNFINISHED]},
            engagement_id=session.engagement_id,
        )
        active = {turn.session_id: turn.status.value for turn in turns}
        peers: list[dict[str, Any]] = []
        offset = 0
        while page := self.store.list_entities(
            ChatSession,
            engagement_id=session.engagement_id,
            offset=offset,
            limit=1_000,
        ):
            offset += len(page)
            for candidate in page:
                if candidate.id == session.id or not self._addressable(candidate):
                    continue
                peers.append(
                    {
                        "session_id": candidate.id,
                        "title": candidate.title,
                        "backend": candidate.backend.value,
                        "model": candidate.model,
                        "state": active.get(candidate.id, "idle"),
                    }
                )
        return sorted(
            peers, key=lambda item: (item["title"].casefold(), item["session_id"])
        )

    def pending(self, recipient_session_id: str) -> list[ChatAgentMessage]:
        return sorted(
            self.store.find_entities(
                ChatAgentMessage,
                {
                    "recipient_session_id": recipient_session_id,
                    "status": ChatAgentMessageStatus.PENDING.value,
                },
            ),
            key=lambda item: (item.created_at, item.id),
        )

    def _mark_delivered(self, messages: Iterable[ChatAgentMessage]) -> None:
        for message in messages:
            for _ in range(3):
                latest = self.store.get(ChatAgentMessage, message.id)
                if latest.status != ChatAgentMessageStatus.PENDING:
                    break
                try:
                    self.store.update(
                        ChatAgentMessage,
                        latest.id,
                        {
                            "status": ChatAgentMessageStatus.DELIVERED,
                            "delivered_at": utc_now(),
                        },
                        expected_revision=latest.revision,
                    )
                    break
                except ConflictError:
                    continue

    def inbox(self, recipient_session_id: str, *, mark: bool = True) -> dict[str, Any]:
        recipient = self.store.get(ChatSession, recipient_session_id)
        messages = self.pending(recipient.id)
        views: list[dict[str, Any]] = []
        for message in messages:
            try:
                sender = self.store.get(ChatSession, message.sender_session_id)
                sender_title = sender.title
            except NotFoundError:
                sender_title = "Deleted conversation"
            views.append(
                {
                    "message_id": message.id,
                    "sender_session_id": message.sender_session_id,
                    "sender_title": sender_title,
                    "content": message.content,
                    "sent_at": message.created_at.isoformat(),
                }
            )
        if mark and messages:
            self._mark_delivered(messages)
        return {
            "messages": views,
            "note": (
                "Messages from independent peer agents in this project. Act on "
                "their content when relevant; reply with send_agent_message."
                if views
                else "No new messages from peer agents."
            ),
        }

    def mark_history_delivered(
        self, session_id: str, loaded_message_ids: Collection[str]
    ) -> None:
        """Mark only messages present in the provider turn's loaded transcript."""

        pending = [
            message
            for message in self.pending(session_id)
            if message.transcript_message_id in loaded_message_ids
        ]
        if pending:
            self._mark_delivered(pending)

    def mark_message_ids_delivered(self, message_ids: Collection[str]) -> None:
        """Mark the exact inbox snapshot inserted into a harness turn."""

        messages: list[ChatAgentMessage] = []
        for message_id in dict.fromkeys(message_ids):
            try:
                messages.append(self.store.get(ChatAgentMessage, message_id))
            except NotFoundError:
                continue
        if messages:
            self._mark_delivered(messages)

    def routing_delivery(
        self, turn: ChatTurn, tool_names: Collection[str]
    ) -> tuple[str, dict[str, Any], str] | None:
        if "read_agent_messages" not in tool_names:
            return None
        messages = self.pending(turn.session_id)
        if not messages:
            return None
        output = self.inbox(turn.session_id)
        count = len(output["messages"])
        return (
            "read_agent_messages",
            output,
            f"{count} message{'' if count == 1 else 's'} from peer agents",
        )

    def list_output(self, invocation: ToolInvocation) -> dict[str, Any]:
        session = self._invoking_session(invocation)
        return {
            "agents": self.peers(session),
            "unread": self.inbox(session.id, mark=False)["messages"],
            "note": "Use a session_id from this list with send_agent_message.",
        }

    def read_output(self, invocation: ToolInvocation) -> dict[str, Any]:
        return self.inbox(self._invoking_session(invocation).id)

    def _existing(
        self, sender_session_id: str, idempotency_key: str | None
    ) -> ChatAgentMessage | None:
        if not idempotency_key:
            return None
        matches = self.store.find_entities(
            ChatAgentMessage,
            {
                "sender_session_id": sender_session_id,
                "idempotency_key": idempotency_key,
            },
        )
        return matches[0] if matches else None

    async def send(
        self,
        invocation: ToolInvocation,
        recipient_session_id: str,
        content: str,
    ) -> dict[str, Any]:
        sender = self._invoking_session(invocation)
        content = content.strip()
        if not content:
            raise InvalidToolArguments("message must say something")
        if len(content) > 20_000:
            raise InvalidToolArguments("message must be at most 20000 characters")
        try:
            recipient = self.store.get(ChatSession, recipient_session_id)
        except NotFoundError as exc:
            raise InvalidToolArguments("unknown peer agent session_id") from exc
        if (
            recipient.engagement_id != sender.engagement_id
            or recipient.id == sender.id
            or not self._addressable(recipient)
        ):
            raise InvalidToolArguments("unknown peer agent session_id")

        key = (
            invocation.idempotency_key
            or hashlib.sha256(
                json.dumps(
                    [sender.id, recipient.id, content],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        existing = self._existing(sender.id, key)
        if existing is not None:
            return self._send_view(existing)
        message_id = str(
            uuid5(NAMESPACE_URL, f"nebula:agent-message:{sender.id}:{key}")
        )
        transcript_id = str(
            uuid5(NAMESPACE_URL, f"nebula:agent-message-transcript:{message_id}")
        )
        message = ChatAgentMessage(
            id=message_id,
            engagement_id=sender.engagement_id,
            sender_session_id=sender.id,
            recipient_session_id=recipient.id,
            content=content,
            transcript_message_id=transcript_id,
            idempotency_key=key,
        )
        for attempt in range(3):
            current = self.store.get(ChatSession, recipient.id)
            if (
                current.engagement_id != sender.engagement_id
                or current.id == sender.id
                or not self._addressable(current)
            ):
                # Recheck after the initial lookup so an archive/disable racing
                # this send wins through the session revision below.
                raise InvalidToolArguments("unknown peer agent session_id")
            stored = self.store.list_session_entities(ChatMessage, recipient.id)
            recorded = current.metadata.get("last_sequence")
            sequence = (
                max(
                    [item.sequence for item in stored]
                    + [recorded if isinstance(recorded, int) else 0]
                )
                + 1
            )
            transcript = ChatMessage(
                id=transcript_id,
                engagement_id=sender.engagement_id,
                session_id=recipient.id,
                sequence=sequence,
                role=ChatRole.SYSTEM,
                content=content,
                metadata={
                    "kind": "agent_message",
                    "agent_message_id": message.id,
                    "sender_session_id": sender.id,
                    "sender_title": sender.title,
                },
            )
            try:
                with self.store.transaction() as transaction:
                    transaction.update(
                        ChatSession,
                        current.id,
                        {
                            "metadata": {
                                **current.metadata,
                                "message_count": sequence,
                                "last_sequence": sequence,
                            }
                        },
                        expected_revision=current.revision,
                    )
                    transaction.add(message)
                    transaction.add(transcript)
                break
            except ConflictError:
                existing = self._existing(sender.id, key)
                if existing is not None:
                    return self._send_view(existing)
                if attempt == 2:
                    raise
        steered = False
        if recipient.backend == ChatBackend.HARNESS and self.harness_steer is not None:
            steered = await self.harness_steer(
                recipient.id,
                f'Peer agent message from "{sender.title}" ({sender.id}):\n\n{content}',
            )
            if steered:
                self._mark_delivered([message])
        return {
            **self._send_view(self.store.get(ChatAgentMessage, message.id)),
            "delivery": "steered" if steered else "queued",
            "note": (
                "Delivered to its active turn."
                if steered
                else "Saved in its conversation. An active provider agent reads it "
                "before its next step; an idle agent reads it on its next turn."
            ),
        }

    def _send_view(self, message: ChatAgentMessage) -> dict[str, Any]:
        return {
            "message_id": message.id,
            "recipient_session_id": message.recipient_session_id,
            "status": message.status.value,
        }


class AgentMessageBroker:
    def __init__(self, service: AgentMessageService) -> None:
        self.service = service

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Any | None = None,
    ) -> ToolExecutionResult:
        del scope, approval
        if invocation.tool_name == "list_agents":
            return ToolExecutionResult(output=self.service.list_output(invocation))
        if invocation.tool_name == "read_agent_messages":
            return ToolExecutionResult(output=self.service.read_output(invocation))
        if invocation.tool_name == "send_agent_message":
            return ToolExecutionResult(
                output=await self.service.send(
                    invocation,
                    str(invocation.arguments.get("session_id") or ""),
                    str(invocation.arguments.get("message") or ""),
                )
            )
        raise InvalidToolArguments(
            f"unsupported agent messaging capability {invocation.tool_name!r}"
        )


def _spec(name: str, description: str, properties: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        budget_class="artifact_query",
    )


def agent_message_specs() -> dict[str, ToolSpec]:
    specs = [
        _spec(
            "list_agents",
            "List independent main agents in this project and any unread messages "
            "they sent you. Subagents and archived conversations are excluded.",
            {},
        ),
        _spec(
            "send_agent_message",
            "Send a concise finding, request, or coordination decision to another "
            "independent main agent in this project. This does not start an idle agent.",
            {
                "session_id": {
                    "type": "string",
                    "description": "Recipient session_id returned by list_agents.",
                },
                "message": {
                    "type": "string",
                    "description": "Self-contained content; the peer cannot see this conversation.",
                },
            },
        ),
        _spec(
            "read_agent_messages",
            "Read messages from peer main agents that you have not received yet.",
            {},
        ),
    ]
    return {spec.name: spec for spec in specs}


def agent_message_components(
    service: AgentMessageService,
    *,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
) -> RuntimeToolComponents:
    specs = agent_message_specs()
    digest = hashlib.sha256(
        json.dumps(
            {name: spec.model_dump(mode="json") for name, spec in specs.items()},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return RuntimeToolComponents(
        broker=AgentMessageBroker(service),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:skill-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs=specs,
        runtime_digest=f"agent-messages-{digest[:16]}",
    )


__all__ = [
    "AGENT_MESSAGE_ROUTING_INSTRUCTIONS",
    "AGENT_MESSAGE_TOOL_NAMES",
    "AgentMessageBroker",
    "AgentMessageService",
    "agent_message_components",
    "agent_message_specs",
]
