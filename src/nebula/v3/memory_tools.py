"""Typed Butler actions using the same authenticated routes as Settings."""

from __future__ import annotations

import re

from .domain import RiskClass
from .memory_policy import MemoryBudgetUpdate, MemoryRunningUpdate
from .settings_assistant_api import SettingsAssistantApi
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_JOB = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,200}$"}


def memory_budget_tools(
    gateway: SettingsAssistantApi | None, project_id: str
) -> list[AnalysisTool]:
    async def call(method, path, body=None):
        if gateway is None:
            raise ValueError("The authenticated Core memory-budget API is unavailable")
        return await gateway.request(
            project_id, method=method, path="memory-budgets" + path, body=body
        )

    async def read(_arguments):
        return await call("GET", "")

    async def usage(_arguments):
        settings = await call("GET", "")
        result = await call("GET", "/jobs")
        page = result.get("result")
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            page = {
                "items": [],
                "at_limit": True,
                "display_limit": 0,
                "summary_only": True,
            }
        jobs = page["items"]
        for job in jobs:
            proof = job.get("evidence") or {}
            current = proof.get("current_bytes")
            ceiling = (proof.get("applied") or job["snapshot"]["limits"])["max_bytes"]
            job["usage_ratio"] = current / ceiling if current is not None else None
            job["approaching_limit"] = (
                current >= 0.8 * ceiling if current is not None else None
            )
        return {
            "settings": settings.get("result")
            or {
                "truncated": True,
                "read_back": settings.get("read_back", False),
                "detail": "The full policy exceeds the assistant response bound. Review Memory budgets before changing it; job observations remain independently available.",
            },
            "jobs": jobs,
            "at_limit": page.get("at_limit", False),
            "display_limit": page.get("display_limit", 100),
            "summary_only": page.get("summary_only", False),
            "detail": "Usage is a timestamped observation, not an additional limit. Missing measurements are unknown. Charged jobs appear first; at_limit means this bounded page may omit jobs, including when its response cannot fit. Never describe it as a complete roster. Use verify_memory_job for complete saved enforcement evidence.",
        }

    async def update(arguments):
        body = MemoryBudgetUpdate.model_validate(arguments).model_dump(mode="json")
        return await call("PUT", "", body)

    def identity(arguments):
        value = arguments.get("job_id", "")
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,200}", value
        ):
            raise ValueError("choose a job identity returned by memory_usage")
        return value

    async def verify(arguments):
        return await call("GET", "/jobs/" + identity(arguments))

    async def apply(arguments):
        job_id = identity(arguments)
        body = MemoryRunningUpdate.model_validate(
            {key: value for key, value in arguments.items() if key != "job_id"}
        )
        return await call(
            "POST", "/jobs/" + job_id + "/apply", body.model_dump(mode="json")
        )

    apply_schema = MemoryRunningUpdate.model_json_schema()
    apply_schema["properties"]["job_id"] = _JOB
    apply_schema["required"] = [*apply_schema["required"], "job_id"]
    items = [
        (
            "nebula.settings.memory_budget",
            "Read configured and effective host memory budgets, administrator ceilings, Core reserve, policy revision and runner capabilities. Large profile metadata is a marked preview; preserve the complete policy and never construct an update from a truncated excerpt. Save affects new jobs; worker application requires observed evidence.",
            _EMPTY,
            read,
            True,
        ),
        (
            "nebula.settings.memory_usage",
            "Inspect a bounded job summary with reservations, measured usage, peaks and jobs approaching their memory limits. Report at_limit as a partial roster and use verify_memory_job for full evidence. Unknown measurements and pending or failed worker application must be reported accurately.",
            _EMPTY,
            usage,
            True,
        ),
        (
            "nebula.settings.set_memory_budget",
            "Save revisioned host memory budgets through Core and read them back. Read memory_budget first and preserve unrelated policy fields. Values are bytes: 1 GiB = 1073741824. Applies to new jobs; never retry or resume failed work automatically.",
            MemoryBudgetUpdate.model_json_schema(),
            update,
            False,
        ),
        (
            "nebula.settings.verify_memory_job",
            "Read the durable effective policy, kernel application evidence, usage and outcome of a job returned by memory_usage. Configuration alone is not proof of enforcement.",
            {
                "type": "object",
                "properties": {"job_id": _JOB},
                "required": ["job_id"],
                "additionalProperties": False,
            },
            verify,
            True,
        ),
        (
            "nebula.settings.apply_memory_limit",
            "Explicitly change one running job's limits using its current revision. Lowering memory, swap or pressure thresholds can terminate it and requires acknowledge_possible_termination=true. Report pending, failed or unsupported application; read verify_memory_job after the action. No automatic retry or goal resume.",
            apply_schema,
            apply,
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
