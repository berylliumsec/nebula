"""Requesting-browser handoffs for sensitive diagnostics exports."""

from __future__ import annotations

from typing import Any

from .domain import RiskClass
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


class ButlerDiagnostics:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def actions(self) -> list[AnalysisTool]:
        async def export(_arguments: dict[str, Any]) -> dict[str, Any]:
            return {
                "state": "pending_browser_apply",
                "client_action": {
                    "kind": "diagnostics_export",
                    "project_id": self.project_id,
                },
            }

        return [
            AnalysisTool(
                ToolSpec(
                    name="nebula.diagnostics.export",
                    description="Ask the requesting browser to confirm and download a sensitive diagnostics ZIP. Logs, settings and build metadata stay on that device; Butler receives no ZIP bytes. A browser download event is not proof of the operating system's final save location.",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                export,
            )
        ]
