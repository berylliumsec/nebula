"""Project-bound Butler handoff for a requesting-device transcript export."""

from __future__ import annotations

from typing import Any

from .chat_subagents import is_subagent_session
from .domain import ChatSession, RiskClass
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_EXPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_revision": {"type": "integer", "minimum": 1},
    },
    "required": ["session_id", "expected_revision"],
    "additionalProperties": False,
}


class ButlerChatExport:
    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    def prepare(self, arguments: dict[str, Any]) -> dict[str, Any]:
        chat = self.store.get(ChatSession, arguments["session_id"])
        if (
            chat.engagement_id != self.project_id
            or chat.metadata.get("control_assistant") is True
            or chat.metadata.get("side_chat") is True
            or chat.metadata.get("temporary_assistant") is True
            or is_subagent_session(chat)
            or isinstance(chat.metadata.get("delegated_from_goal_id"), str)
        ):
            raise ValueError("choose a saved main chat in the selected project")
        if chat.revision != arguments["expected_revision"]:
            raise ValueError("chat changed; read its current revision before exporting")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "chat_export",
                "project_id": self.project_id,
                "session_id": chat.id,
                "expected_revision": chat.revision,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        async def run(arguments: dict[str, Any]) -> dict[str, Any]:
            return self.prepare(arguments)

        return [
            AnalysisTool(
                ToolSpec(
                    name="nebula.chats.export",
                    description=(
                        "Request a Markdown export of one listed saved main chat to the "
                        "operator's current browser. Pass the chat's current revision "
                        "from nebula.work.chats or nebula.work.chat. The requesting "
                        "browser confirms sensitive data, reads the complete transcript "
                        "from Core, and starts the local download. The model never "
                        "receives the export. Interactive Butler only."
                    ),
                    input_schema=_EXPORT_SCHEMA,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.LOCAL_READ,
                    idempotency=IdempotencyBehavior.SAFE,
                ),
                run,
            )
        ]
