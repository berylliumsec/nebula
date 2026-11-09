"""Typed chat acceptance is an optional transport for the same immutable intent."""

from dataclasses import dataclass

from .database import RuntimeChatBindingRow, RuntimeCommandRow
from .execution_scope import current_execution_lease
from .runtime_coordination import CoordinationConflict, LeaseLost

CHAT_ACCEPT_COMMAND = "chat.accept/v1"
CHAT_ACCEPT_FEATURE = "chat.accept/v1"
CHAT_RESUME_COMMAND = "chat.resume/v1"
CHAT_RESUME_FEATURE = "chat.resume/v1"
CHAT_COMMANDS = frozenset((CHAT_ACCEPT_COMMAND, CHAT_RESUME_COMMAND))
HTTP_INVOKE_COMMAND = "http.invoke/v1"


@dataclass(frozen=True)
class ChatBinding:
    command_id: str
    project_id: str
    turn_id: str
    session_id: str
    stream_after: int | None = None
    ready: bool = True


async def bind_chat_command(
    session, *, command_id, turn_id, session_id, name=CHAT_ACCEPT_COMMAND
):
    if command_id is None:
        return
    lease = current_execution_lease()
    if lease is None:
        raise LeaseLost("Chat acceptance requires exact project ownership")
    command = await session.get(RuntimeCommandRow, command_id)
    if (
        command is None
        or (
            command.name,
            command.state,
            command.project_id,
            command.runtime_id,
            command.fencing_token,
        )
        != (
            name,
            "running",
            lease.project_id,
            lease.runtime_id,
            lease.fencing_token,
        )
        or name not in CHAT_COMMANDS
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


async def bind_stream_cursor(session, *, command_id, turn_id, after):
    """Publish the receipt's segment boundary with its first committed frame."""
    lease = current_execution_lease()
    binding = await session.get(RuntimeChatBindingRow, command_id)
    command = await session.get(RuntimeCommandRow, command_id)
    if (
        lease is None
        or binding is None
        or command is None
        or (
            binding.project_id,
            binding.runtime_id,
            binding.fencing_token,
            binding.turn_id,
            command.state,
        )
        != (
            lease.project_id,
            lease.runtime_id,
            lease.fencing_token,
            turn_id,
            "running",
        )
        or command.name not in CHAT_COMMANDS
    ):
        raise CoordinationConflict("Chat stream does not match its accepted request")
    if binding.stream_after is not None and binding.stream_after != after:
        raise CoordinationConflict("The accepted stream boundary is immutable")
    binding.stream_after = after
