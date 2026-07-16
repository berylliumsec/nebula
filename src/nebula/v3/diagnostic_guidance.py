"""Deterministic, offline operator guidance for correlated diagnostics."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

CATALOG_SCHEMA = "nebula.diagnostic-remediation-catalog/v1"
INCIDENT_SCHEMA = "nebula.diagnostic-incident/v1"
REASON_CODES = frozenset(
    {
        "transport_closed",
        "protocol_invalid",
        "dependency_unavailable",
        "authentication_failed",
        "timeout",
        "rate_limited",
        "permission_denied",
        "storage_write_failed",
        "integrity_failed",
        "stale_state",
        "invalid_input",
        "cancelled",
        "unknown_internal_fault",
    }
)


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosticAction(_ContractModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    label: str = Field(min_length=1, max_length=120)
    kind: Literal["navigate", "health_check", "retry"]
    confirmation_required: bool = False
    enabled: bool = True
    disabled_reason: str | None = Field(default=None, max_length=500)
    destination: str | None = Field(default=None, max_length=500)


class DiagnosticGuidance(_ContractModel):
    remediation_id: str = Field(min_length=3, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    affected_operation: str = Field(min_length=1, max_length=300)
    cause: str = Field(min_length=1, max_length=2_048)
    impact: str = Field(min_length=1, max_length=2_048)
    confirmed_safe_state: str = Field(min_length=1, max_length=2_048)
    steps: list[str] = Field(min_length=1, max_length=12)
    verification: str = Field(min_length=1, max_length=2_048)
    help_article: str | None = Field(default=None, max_length=160)


class DiagnosticIncident(_ContractModel):
    schema_: Literal["nebula.diagnostic-incident/v1"] = Field(
        default="nebula.diagnostic-incident/v1", alias="schema"
    )
    error_id: str = Field(min_length=1, max_length=128)
    status: Literal["active", "historical", "resolved"] = "active"
    primary: dict[str, Any]
    related_records: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    guidance: DiagnosticGuidance
    actions: list[DiagnosticAction] = Field(default_factory=list, max_length=12)
    facts: dict[str, str | int | float | bool] = Field(default_factory=dict)
    sensitive_detail_available: bool = False
    sensitive_detail_expires_at: str | None = None


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    path = Path(__file__).with_name("diagnostic_guidance.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_catalog(value)
    return value


def validate_catalog(value: Mapping[str, Any]) -> None:
    if value.get("schema") != CATALOG_SCHEMA:
        raise ValueError("unsupported diagnostic remediation catalog schema")
    reasons = value.get("reason_families")
    features = value.get("features")
    if not isinstance(reasons, Mapping) or set(reasons) != REASON_CODES:
        raise ValueError("diagnostic catalog reason coverage is incomplete")
    if not isinstance(features, Mapping):
        raise ValueError("diagnostic catalog feature coverage is missing")
    from .diagnostics import FEATURE_FILES
    from .operator_help import operator_help_articles

    if set(features) != set(FEATURE_FILES):
        raise ValueError("diagnostic catalog feature coverage is incomplete")
    help_articles = {article.article_id for article in operator_help_articles()}
    allowed_destinations = {"/", "/settings", "/project", "/findings", "/reports"}
    for reason, raw in reasons.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"diagnostic reason {reason} must be an object")
        required = {
            "title",
            "cause",
            "impact",
            "confirmed_safe_state",
            "steps",
            "verification",
            "retryable",
        }
        if not required.issubset(raw):
            raise ValueError(f"diagnostic reason {reason} is incomplete")
        steps = raw.get("steps")
        if (
            not isinstance(steps, list)
            or not steps
            or not all(isinstance(item, str) and item.strip() for item in steps)
        ):
            raise ValueError(f"diagnostic reason {reason} has invalid recovery steps")
        if not str(raw.get("verification") or "").strip():
            raise ValueError(f"diagnostic reason {reason} needs verification text")
    for feature, raw in features.items():
        if not isinstance(raw, Mapping) or not all(
            str(raw.get(key) or "").strip()
            for key in ("label", "operation", "destination", "help_article")
        ):
            raise ValueError(f"diagnostic feature {feature} is incomplete")
        destination = str(raw["destination"])
        destination_path = destination.split("?", 1)[0].split("#", 1)[0]
        if destination_path not in allowed_destinations:
            raise ValueError(f"diagnostic feature {feature} has an unsafe destination")
        if str(raw["help_article"]) not in help_articles:
            raise ValueError(f"diagnostic feature {feature} has a broken help article")


def reason_code_for(
    exception: BaseException | None = None,
    *,
    feature: str | None = None,
    event_code: str | None = None,
    status_code: int | None = None,
    supplied: str | None = None,
) -> str:
    if supplied in REASON_CODES:
        return supplied
    name = type(exception).__name__.lower() if exception is not None else ""
    code = " ".join(filter(None, (event_code, name))).lower()
    if status_code == 429 or "rate" in code and "limit" in code:
        return "rate_limited"
    if status_code == 401 or any(
        token in code for token in ("authentication", "credential", "unauthorized")
    ):
        return "authentication_failed"
    if status_code == 403 or any(
        token in code for token in ("permission", "denied", "privacy", "policy")
    ):
        return "permission_denied"
    if status_code in {408, 504} or "timeout" in code or "timedout" in code:
        return "timeout"
    if any(token in code for token in ("integrity", "digest", "signature", "checksum")):
        return "integrity_failed"
    if any(
        token in code
        for token in ("conflict", "stale", "stateerror", "state_error", "revision")
    ):
        return "stale_state"
    if isinstance(exception, PermissionError):
        return "permission_denied"
    if isinstance(exception, (ConnectionError, BrokenPipeError, EOFError)) or any(
        token in code for token in ("transport", "disconnect", "closed", "endofstream")
    ):
        return "transport_closed"
    if any(token in code for token in ("protocol", "malformed", "decode", "parse")):
        return "protocol_invalid"
    if (
        status_code is not None
        and status_code in {502, 503}
        or any(
            token in code for token in ("unavailable", "notavailable", "not_available")
        )
    ):
        return "dependency_unavailable"
    if isinstance(exception, OSError) and feature in {
        "storage",
        "workspace",
        "diagnostics",
        "evidence",
        "reports",
    }:
        return "storage_write_failed"
    if isinstance(exception, (ValueError, LookupError, UnicodeError)) or any(
        token in code
        for token in ("invalid", "validation", "unsupported", "configuration")
    ):
        return "invalid_input"
    if any(token in code for token in ("cancelled", "canceled", "interrupted")):
        return "cancelled"
    return "unknown_internal_fault"


def guidance_for(
    feature: str,
    reason_code: str,
    *,
    operator_detail: str | None = None,
    impact: str | None = None,
    remediation_id: str | None = None,
) -> DiagnosticGuidance:
    catalog = load_catalog()
    feature_entry = (
        catalog["features"].get(feature) or catalog["features"]["diagnostics"]
    )
    reason_entry = (
        catalog["reason_families"].get(reason_code)
        or catalog["reason_families"]["unknown_internal_fault"]
    )
    return DiagnosticGuidance(
        remediation_id=remediation_id or f"{feature}.{reason_code}",
        title=str(reason_entry["title"]),
        affected_operation=str(feature_entry["operation"]),
        cause=operator_detail or str(reason_entry["cause"]),
        impact=impact or str(reason_entry["impact"]),
        confirmed_safe_state=str(reason_entry["confirmed_safe_state"]),
        steps=[str(item) for item in reason_entry["steps"]],
        verification=str(reason_entry["verification"]),
        help_article=str(feature_entry["help_article"]),
    )


def actions_for_record(record: Mapping[str, Any]) -> list[DiagnosticAction]:
    feature = str(record.get("feature") or "diagnostics")
    catalog = load_catalog()
    feature_entry = (
        catalog["features"].get(feature) or catalog["features"]["diagnostics"]
    )
    actions = [
        DiagnosticAction(
            id="open_affected_view",
            label=f"Open {feature_entry['label']}",
            kind="navigate",
            destination=str(feature_entry["destination"]),
        )
    ]
    if feature not in {"interface", "notes", "findings"}:
        actions.append(
            DiagnosticAction(
                id="run_health_check",
                label="Run health check",
                kind="health_check",
                confirmation_required=True,
            )
        )
    metadata = record.get("metadata")
    durable_retry = (
        record.get("retryable") is True
        and isinstance(metadata, Mapping)
        and metadata.get("entity_type") == "harness_turn"
        and isinstance(metadata.get("entity_id"), str)
    )
    actions.append(
        DiagnosticAction(
            id="retry_operation",
            label="Retry failed operation",
            kind="retry",
            confirmation_required=True,
            enabled=durable_retry,
            disabled_reason=None
            if durable_retry
            else "No durably retained failed operation is linked to this incident.",
        )
    )
    return actions


def _correlation_values(record: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("error_id", "request_id", "operation_id", "parent_operation_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            values.add(value)
    return values


def _primary_record(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    source_rank = {"core": 0, "desktop": 1, "browser": 2, "interface": 3}
    return min(
        records,
        key=lambda item: (
            source_rank.get(str(item.get("source") or ""), 4),
            0 if not str(item.get("event_code") or "").startswith("interface.") else 1,
            str(item.get("timestamp") or ""),
        ),
    )


def _facts(record: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    result: dict[str, str | int | float | bool] = {}
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        for key in (
            "provider",
            "transport",
            "http_status",
            "model_id",
            "state",
            "component",
            "adapter",
            "version",
        ):
            value = metadata.get(key)
            if isinstance(value, (str, int, float, bool)):
                result[key] = value
    for key in ("feature", "stage", "run_id", "execution_id", "session_id"):
        value = record.get(key)
        if isinstance(value, str):
            result[key] = value
    return result


def resolve_incidents(records: Sequence[Mapping[str, Any]]) -> list[DiagnosticIncident]:
    bounded = [dict(record) for record in records[:500] if isinstance(record, Mapping)]
    groups: list[list[dict[str, Any]]] = []
    correlations: list[set[str]] = []
    for record in bounded:
        values = _correlation_values(record)
        indexes = [index for index, known in enumerate(correlations) if values & known]
        if not indexes:
            groups.append([record])
            correlations.append(values)
            continue
        first = indexes[0]
        groups[first].append(record)
        correlations[first].update(values)
        for index in reversed(indexes[1:]):
            groups[first].extend(groups.pop(index))
            correlations[first].update(correlations.pop(index))
    incidents: list[DiagnosticIncident] = []
    for group in groups:
        primary = dict(_primary_record(group))
        error_id = next(
            (
                str(record["error_id"])
                for record in group
                if isinstance(record.get("error_id"), str)
            ),
            "historical-"
            + hashlib.sha256(
                json.dumps(primary, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:24],
        )
        feature = str(primary.get("feature") or "diagnostics")
        reason = reason_code_for(
            feature=feature,
            event_code=str(primary.get("event_code") or ""),
            status_code=(primary.get("metadata") or {}).get("http_status")
            if isinstance(primary.get("metadata"), Mapping)
            else None,
            supplied=str(primary.get("reason_code") or ""),
        )
        operator_detail = primary.get("operator_detail") or primary.get(
            "safe_failure_cause"
        )
        guidance = guidance_for(
            feature,
            reason,
            operator_detail=str(operator_detail) if operator_detail else None,
            impact=str(primary.get("impact")) if primary.get("impact") else None,
            remediation_id=str(primary.get("remediation_id"))
            if primary.get("remediation_id")
            else None,
        )
        historical = not primary.get("reason_code")
        incidents.append(
            DiagnosticIncident(
                error_id=error_id,
                status="historical" if historical else "active",
                primary=primary,
                related_records=[
                    item for item in group if item is not primary and item != primary
                ],
                guidance=guidance,
                actions=actions_for_record(primary),
                facts=_facts(primary),
                sensitive_detail_available=bool(
                    primary.get("sensitive_detail_available")
                ),
                sensitive_detail_expires_at=primary.get("sensitive_detail_expires_at")
                if isinstance(primary.get("sensitive_detail_expires_at"), str)
                else None,
            )
        )
    return sorted(
        incidents,
        key=lambda incident: str(incident.primary.get("timestamp") or ""),
        reverse=True,
    )


def semantic_event_code(feature: str, reason_code: str) -> str:
    safe_feature = re.sub(r"[^a-z0-9-]", "-", feature.lower())
    return f"{safe_feature}.{reason_code}"


__all__ = [
    "CATALOG_SCHEMA",
    "INCIDENT_SCHEMA",
    "REASON_CODES",
    "DiagnosticAction",
    "DiagnosticGuidance",
    "DiagnosticIncident",
    "actions_for_record",
    "guidance_for",
    "load_catalog",
    "reason_code_for",
    "resolve_incidents",
    "semantic_event_code",
    "validate_catalog",
]
