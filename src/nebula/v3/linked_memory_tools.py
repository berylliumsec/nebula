"""Typed Butler actions for linked memory, over the authenticated Core routes."""

from __future__ import annotations

from .domain import RiskClass
from .linked_memory import LinkedMemoryWrite
from .settings_assistant_api import SettingsAssistantApi
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def linked_memory_tools(
    gateway: SettingsAssistantApi | None, project_id: str
) -> list[AnalysisTool]:
    async def call(method, suffix="", body=None):
        if gateway is None:
            raise ValueError("The authenticated Core linked-memory API is unavailable")
        return await gateway.request(
            project_id,
            method=method,
            path="engagements/{project}/linked-memory" + suffix,
            body=body,
        )

    async def read(_arguments):
        settings = await call("GET")
        status = await call("GET", "/status")
        return {"settings": settings.get("result"), "status": status.get("result")}

    async def update(arguments):
        body = LinkedMemoryWrite.model_validate(arguments).model_dump(mode="json")
        return await call("PUT", "", body)

    items = [
        (
            "nebula.settings.linked_memory",
            "Read this project's linked memory stage (off or shadow), its revision, "
            "the Core-wide kill switch, and curation status: completed goals waiting, "
            "card and link counts, model spend, whether curation is paused and why, "
            "the curation helper's state, and the last error. Linked memory curates "
            "memory cards from completed goals in the background; in shadow it "
            "injects nothing into any chat. This is not RAM or memory budgets.",
            _EMPTY,
            read,
            True,
        ),
        (
            "nebula.settings.set_linked_memory",
            "Change this project's linked memory stage only when the operator asks "
            "in this conversation. Read linked_memory first and pass its revision as "
            "expected_revision. Stages: off, or shadow (curate completed goals in "
            "the background, inject nothing). Core reads the saved stage back; "
            "report the read-back, not the request. Goals completed before the "
            "first enable are not curated.",
            LinkedMemoryWrite.model_json_schema(),
            update,
            False,
        ),
    ]
    return [
        AnalysisTool(
            ToolSpec(
                name=name,
                description=description,
                input_schema=schema,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ
                if read_only
                else RiskClass.WORKSPACE_WRITE,
                idempotency=IdempotencyBehavior.SAFE
                if read_only
                else IdempotencyBehavior.NON_IDEMPOTENT,
            ),
            handler,
        )
        for name, description, schema, handler, read_only in items
    ]
