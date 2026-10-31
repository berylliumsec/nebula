"""Project-bound terminal discovery and browser-owned stop handoff for Butler."""

from __future__ import annotations

from typing import Any

from .container_terminal import ContainerTerminalService
from .domain import RiskClass
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_LIST = {"type": "object", "properties": {}, "additionalProperties": False}
_STOP = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_created_at": {"type": "string", "minLength": 1, "maxLength": 100},
    },
    "required": ["session_id", "expected_created_at"],
    "additionalProperties": False,
}


class ButlerTerminal:
    """Expose live session identity, never connection tickets, to interactive Butler."""

    def __init__(self, service: ContainerTerminalService, project_id: str) -> None:
        self.service = service
        self.project_id = project_id

    async def list_sessions(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        page = await self.service.list_sessions(self.project_id)
        return page.model_dump(mode="json")

    async def stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page = await self.service.list_sessions(self.project_id)
        target = next(
            (
                session
                for session in page.sessions
                if session.session_id == arguments["session_id"]
            ),
            None,
        )
        if target is None:
            raise ValueError("terminal is no longer active in the selected Project")
        created_at = target.model_dump(mode="json")["created_at"]
        if created_at != arguments["expected_created_at"]:
            raise ValueError("terminal changed; read its current identity first")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "terminal_stop",
                "project_id": self.project_id,
                "session_id": target.session_id,
                "expected_created_at": created_at,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name="nebula.terminal.list",
                    description=(
                        "List live terminals in the selected Project without issuing "
                        "WebSocket tickets or exposing terminal output. Read one exact "
                        "session identity before requesting a stop. Opening the Terminal "
                        "destination in the requesting browser starts or reconnects it."
                    ),
                    input_schema=_LIST,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.LOCAL_READ,
                    idempotency=IdempotencyBehavior.SAFE,
                ),
                self.list_sessions,
            ),
            AnalysisTool(
                ToolSpec(
                    name="nebula.terminal.stop",
                    description=(
                        "Ask the requesting browser to confirm stopping one exact live "
                        "Project terminal, or retry removal when Core reports it as "
                        "stopping. Running commands and installed container packages "
                        "will be lost; Project workspace files remain. The browser "
                        "rechecks Core state before stopping and reads it back."
                    ),
                    input_schema=_STOP,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.stop,
            ),
        ]
