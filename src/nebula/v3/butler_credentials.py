"""Write-only credential handoffs for the requesting browser."""

from __future__ import annotations

from .async_utils import call_async

import re
from typing import Any

from .domain import RiskClass
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_MANAGED_REFERENCE = re.compile(r"^(?:vault|session):[0-9a-f]{32}$")
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_DELETE = {
    "type": "object",
    "properties": {
        "reference": {
            "type": "string",
            "pattern": r"^(?:vault|session):[0-9a-f]{32}$",
        }
    },
    "required": ["reference"],
    "additionalProperties": False,
}


class ButlerCredentials:
    """Prepare credential changes without sending secret values through Butler."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def create(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "credential_create",
                "project_id": self.project_id,
            },
        }

    def delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        reference = arguments["reference"]
        if not _MANAGED_REFERENCE.fullmatch(reference):
            raise ValueError("choose an exact Nebula-managed credential reference")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "credential_delete",
                "project_id": self.project_id,
                "reference": reference,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.credentials.create",
                "Open write-only credential entry on the requesting browser. Butler never receives the secret. The browser confirms a new reference and status; attach that reference to the intended provider or integration separately.",
                _EMPTY,
                self.create,
            ),
            (
                "nebula.credentials.delete",
                "Ask the requesting browser to confirm deletion of one exact Nebula-managed vault or session credential reference. Environment and systemd references are managed outside Nebula. The browser checks status before and after deletion.",
                _DELETE,
                self.delete,
            ),
        )
        result: list[AnalysisTool] = []
        for name, description, schema, handler in operations:

            async def run(
                arguments: dict[str, Any], *, selected_handler: Any = handler
            ) -> dict[str, Any]:
                return await call_async(selected_handler, arguments)

            result.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=RiskClass.WORKSPACE_WRITE,
                        idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                    ),
                    run,
                )
            )
        return result
