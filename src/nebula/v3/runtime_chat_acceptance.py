"""Typed chat acceptance is an optional transport for the same immutable intent."""

from dataclasses import dataclass

from .database import RuntimeChatBindingRow, RuntimeCommandRow
from .execution_scope import current_execution_lease
from .runtime_coordination import CoordinationConflict, LeaseLost

CHAT_ACCEPT_COMMAND = "chat.accept/v1"
CHAT_ACCEPT_FEATURE = "chat.accept/v1"
HTTP_INVOKE_COMMAND = "http.invoke/v1"


@dataclass(frozen=True)
class ChatBinding:
    command_id: str
    project_id: str
    turn_id: str
    session_id: str


async def bind_chat_command(session, *, command_id, turn_id, session_id):
    if command_id is None:
        return
    lease = current_execution_lease()
    if lease is None:
        raise LeaseLost("Chat acceptance requires exact project ownership")
    command = await session.get(RuntimeCommandRow, command_id)
    if command is None or (
        command.name,
        command.state,
        command.project_id,
        command.runtime_id,
        command.fencing_token,
    ) != (
        CHAT_ACCEPT_COMMAND,
        "running",
        lease.project_id,
        lease.runtime_id,
        lease.fencing_token,
    ):
        raise CoordinationConflict("Chat acceptance does not match its claimed request")
    existing = await session.get(RuntimeChatBindingRow, command_id)
    if existing is not None:
        if (existing.project_id, existing.turn_id, existing.session_id) != (
            lease.project_id,
            turn_id,
            session_id,
        ):
            raise CoordinationConflict(
                "This request already accepted another chat turn"
            )
        return
    session.add(
        RuntimeChatBindingRow(
            command_id=command_id,
            project_id=lease.project_id,
            turn_id=turn_id,
            session_id=session_id,
            runtime_id=lease.runtime_id,
            fencing_token=lease.fencing_token,
        )
    )
