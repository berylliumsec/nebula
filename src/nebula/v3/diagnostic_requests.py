"""Bounded browser diagnostics inputs shared by host and embedded APIs."""

from typing import Any, Literal
from pydantic import Field
from .domain import NebulaModel


class DiagnosticsSettingsRequest(NebulaModel):
    schema_: Literal["nebula.diagnostics-settings/v1"] = Field(
        default="nebula.diagnostics-settings/v1", alias="schema"
    )
    global_level: Literal["debug", "info", "warning", "error", "critical"]
    feature_levels: dict[
        str, Literal["debug", "info", "warning", "error", "critical"]
    ] = Field(default_factory=dict, max_length=64)
    sensitive_detail_capture: bool = False


class BrowserDiagnosticStackFrame(NebulaModel):
    module: str = Field(min_length=1, max_length=128)
    function: str = Field(min_length=1, max_length=128)
    line: int = Field(ge=0, le=10_000_000)


class BrowserDiagnosticEvent(NebulaModel):
    schema_: Literal["nebula.diagnostic/v1"] = Field(
        default="nebula.diagnostic/v1", alias="schema"
    )
    level: Literal["debug", "info", "warning", "error", "critical"]
    feature: Literal["interface"] = "interface"
    event_code: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$",
    )
    message: str = Field(min_length=1, max_length=2_048)
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    parent_operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    error_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    project_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    execution_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    outcome: str | None = Field(default=None, max_length=64)
    stage: str | None = Field(default=None, max_length=128)
    duration_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    retryable: bool | None = None
    safe_failure_cause: str | None = Field(default=None, max_length=2_048)
    reason_code: str | None = Field(default=None, min_length=1, max_length=64)
    operator_detail: str | None = Field(default=None, max_length=2_048)
    impact: str | None = Field(default=None, max_length=2_048)
    remediation_id: str | None = Field(default=None, max_length=160)
    sensitive_detail_available: bool | None = None
    sensitive_detail_expires_at: str | None = Field(default=None, max_length=64)
    exception_type: str | None = Field(default=None, min_length=1, max_length=128)
    stack_frames: list[BrowserDiagnosticStackFrame] = Field(
        default_factory=list, max_length=32
    )
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)


class BrowserDiagnosticBatch(NebulaModel):
    events: list[BrowserDiagnosticEvent] = Field(min_length=1, max_length=100)
