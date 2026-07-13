"""Typed, UI-independent domain contracts for Nebula 3.

The relational store is authoritative.  These models are the stable boundary used
by the API, providers, policy engine, importers, and future GUI clients.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class StringEnum(str, Enum):
    """A string-valued enum with stable JSON serialization."""


class EngagementStatus(StringEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"
    ARCHIVED = "archived"


class RiskClass(StringEnum):
    LOCAL_READ = "local_read"
    PASSIVE = "passive"
    ACTIVE_SCAN = "active_scan"
    WORKSPACE_WRITE = "workspace_write"
    CREDENTIAL_USE = "credential_use"
    EXPLOITATION = "exploitation"
    PERSISTENCE = "persistence"
    DESTRUCTIVE = "destructive"
    SCOPE_CHANGE = "scope_change"


class FindingStatus(StringEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    CONFIRMED = "confirmed"
    ACCEPTED_RISK = "accepted-risk"
    FALSE_POSITIVE = "false-positive"
    REMEDIATED = "remediated"
    RETEST_PASSED = "retest-passed"
    RETEST_FAILED = "retest-failed"


class Severity(StringEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RunStatus(StringEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETE = "complete"


class TaskStatus(StringEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETE = "complete"


class ToolCallStatus(StringEnum):
    PROPOSED = "proposed"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    DENIED = "denied"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETE = "complete"


class ApprovalStatus(StringEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ToolPackInstallationStatus(StringEnum):
    PENDING = "pending"
    PULLING = "pulling"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"
    DISABLED = "disabled"


class ToolPackTrust(StringEnum):
    CURATED = "curated"
    TRUSTED_PUBLISHER = "trusted_publisher"
    LOCAL_UNSIGNED = "local_unsigned"


class RunnerRuntime(StringEnum):
    PODMAN = "podman"
    DOCKER = "docker"


class RunnerIsolation(StringEnum):
    ROOTLESS = "rootless"
    PODMAN_MACHINE = "podman_machine"
    DOCKER_DESKTOP_VM = "docker_desktop_vm"


class ReportStatus(StringEnum):
    DRAFT = "draft"
    REVIEW = "review"
    FINAL = "final"


class OperatorExecutionStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExecutionNetworkMode(StringEnum):
    NONE = "none"
    SCOPED = "scoped"


class ExecutionOriginKind(StringEnum):
    ASSISTANT_MESSAGE = "assistant_message"
    RERUN = "rerun"


class GeneratedDraftStatus(StringEnum):
    GENERATING = "generating"
    READY = "ready"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"


class ReportRenderStatus(StringEnum):
    QUEUED = "queued"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class CorrelationMethod(StringEnum):
    PURL = "purl"
    CPE = "cpe"
    SCANNER_CVE = "scanner_cve"
    FUZZY_BANNER = "fuzzy_banner"


class CorrelationStatus(StringEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    NOT_AFFECTED = "not_affected"


class NebulaModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        protected_namespaces=(),
        str_strip_whitespace=True,
    )


class Entity(NebulaModel):
    """Common persisted-entity fields with optimistic revision support."""

    entity_kind: ClassVar[str]
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    revision: int = Field(default=1, ge=1)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def updated_after_creation(self) -> "Entity":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        return self


class MissionGrant(NebulaModel):
    risk_classes: list[RiskClass]
    tool_names: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    granted_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    granted_by: str = Field(min_length=1)

    @field_validator("granted_at", "expires_at")
    @classmethod
    def expiry_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("grant timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def expiry_after_grant(self) -> "MissionGrant":
        if self.expires_at <= self.granted_at:
            raise ValueError("expires_at must be later than granted_at")
        return self


class ScopePolicy(Entity):
    entity_kind: ClassVar[str] = "scope_policies"
    engagement_id: str
    allowed_cidrs: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_urls: list[str] = Field(default_factory=list)
    allowed_ports: list[int] = Field(default_factory=list)
    not_before: datetime | None = None
    not_after: datetime | None = None
    prohibited_actions: list[str] = Field(default_factory=list)
    local_only: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=256)
    grants: list[MissionGrant] = Field(default_factory=list)

    @field_validator("allowed_cidrs")
    @classmethod
    def normalize_cidrs(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            normalized.append(str(ipaddress.ip_network(value, strict=False)))
        return sorted(set(normalized))

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        normalized = []
        domain_pattern = re.compile(
            r"^(?:\*\.)?(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
        )
        for value in values:
            domain = value.rstrip(".").lower()
            if not domain_pattern.fullmatch(domain):
                raise ValueError(f"invalid domain: {value}")
            normalized.append(domain)
        return sorted(set(normalized))

    @field_validator("allowed_ports")
    @classmethod
    def normalize_ports(cls, values: list[int]) -> list[int]:
        for value in values:
            if not 1 <= value <= 65535:
                raise ValueError("ports must be between 1 and 65535")
        return sorted(set(values))

    @field_validator("allowed_urls")
    @classmethod
    def normalize_urls(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            try:
                parsed = urlsplit(value)
                port = parsed.port
            except ValueError as exc:
                raise ValueError(f"invalid scoped URL: {value}") from exc
            if parsed.scheme.lower() not in {"http", "https"}:
                raise ValueError("scoped URLs must use http or https")
            if not parsed.hostname:
                raise ValueError("scoped URLs require a hostname")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("scoped URLs cannot contain credentials")
            if parsed.fragment:
                raise ValueError("scoped URLs cannot contain fragments")
            if any(ord(character) < 32 for character in value):
                raise ValueError("scoped URLs cannot contain control characters")
            try:
                host = parsed.hostname.encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise ValueError(f"invalid scoped URL hostname: {value}") from exc
            if ":" in host:
                host = f"[{host}]"
            netloc = f"{host}:{port}" if port is not None else host
            normalized.append(
                urlunsplit(
                    (
                        parsed.scheme.lower(),
                        netloc,
                        parsed.path or "/",
                        parsed.query,
                        "",
                    )
                )
            )
        return sorted(set(normalized))

    @field_validator("not_before", "not_after")
    @classmethod
    def optional_times_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("scope timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def valid_window(self) -> "ScopePolicy":
        if self.not_before and self.not_after and self.not_after <= self.not_before:
            raise ValueError("not_after must be later than not_before")
        return self


class Engagement(Entity):
    entity_kind: ClassVar[str] = "engagements"
    name: str = Field(min_length=1, max_length=300)
    description: str = ""
    status: EngagementStatus = EngagementStatus.DRAFT
    scope_policy_id: str | None = None
    client_name: str | None = None
    owner_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolPackInstallation(Entity):
    """One immutable pack version installed for the current OS user."""

    entity_kind: ClassVar[str] = "tool_pack_installations"
    publisher: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,127}$")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    version: str = Field(min_length=1, max_length=100)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str = Field(min_length=1, max_length=2048)
    trust: ToolPackTrust
    publisher_key_id: str | None = Field(default=None, max_length=200)
    runtime_profile_id: str
    image_locks: dict[str, str] = Field(default_factory=dict)
    interface_catalog_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    interface_catalog_path: str | None = None
    status: ToolPackInstallationStatus = ToolPackInstallationStatus.PENDING
    manifest_path: str
    installed_at: datetime | None = None
    verified_at: datetime | None = None
    failure_detail: str | None = Field(default=None, max_length=4000)

    @field_validator("installed_at", "verified_at")
    @classmethod
    def pack_times_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("tool-pack timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class RunnerProfile(Entity):
    """Explicitly configured local OCI runtime; executables are never PATH-resolved."""

    entity_kind: ClassVar[str] = "runner_profiles"
    name: str = Field(min_length=1, max_length=200)
    runtime: RunnerRuntime
    executable: str
    context: str | None = Field(default=None, max_length=500)
    socket: str | None = Field(default=None, max_length=2048)
    platform: str = Field(pattern=r"^linux/(amd64|arm64)$")
    isolation: RunnerIsolation
    egress_helper_image: str | None = None
    seccomp_profile: str | None = None
    enabled: bool = True
    healthy: bool = False
    last_health_at: datetime | None = None
    last_health_detail: str | None = Field(default=None, max_length=4000)

    @field_validator("executable")
    @classmethod
    def runner_executable_is_absolute(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("runner executable must be an absolute path")
        return value

    @field_validator("egress_helper_image")
    @classmethod
    def helper_image_is_digest_pinned(cls, value: str | None) -> str | None:
        if value is None:
            return None
        pattern = (
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
            r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
            r"@sha256:[0-9a-f]{64}"
        )
        if not re.fullmatch(pattern, value):
            raise ValueError("egress helper image must be digest-pinned")
        repository = value.rsplit("@", 1)[0].rsplit("/", 1)[-1]
        if ":" in repository:
            raise ValueError("egress helper image cannot include a mutable tag")
        return value

    @field_validator("seccomp_profile")
    @classmethod
    def seccomp_path_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and (not value.startswith("/") or "\x00" in value):
            raise ValueError("seccomp profile must be an absolute path")
        return value

    @field_validator("socket")
    @classmethod
    def runner_socket_is_local(cls, value: str | None) -> str | None:
        if value is not None and not (
            value.startswith("unix://") or value.startswith("/")
        ):
            raise ValueError("runner socket must be a local Unix socket")
        return value

    @field_validator("last_health_at")
    @classmethod
    def runner_health_time_must_be_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("runner health timestamp must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def runtime_matches_executable(self) -> "RunnerProfile":
        executable_name = self.executable.rsplit("/", 1)[-1]
        if executable_name not in {"docker", "podman"}:
            raise ValueError("runner executable must be docker or podman")
        if executable_name != self.runtime.value:
            raise ValueError("runner runtime must match its executable")
        return self


class EngagementToolAssignment(Entity):
    """Exact installed pack and tool allowlist granted to one engagement."""

    entity_kind: ClassVar[str] = "engagement_tool_assignments"
    engagement_id: str
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_tool_names: list[str] = Field(default_factory=list)
    enabled: bool = True
    assigned_by: str = Field(min_length=1, max_length=200)

    @field_validator("allowed_tool_names")
    @classmethod
    def normalize_assigned_tools(cls, values: list[str]) -> list[str]:
        pattern = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
        if any(not pattern.fullmatch(value) for value in values):
            raise ValueError("assigned tool names must be canonical tool identifiers")
        return sorted(set(values))


class Asset(Entity):
    entity_kind: ClassVar[str] = "assets"
    engagement_id: str
    asset_type: str = "host"
    name: str = Field(min_length=1, max_length=500)
    address: str | None = None
    hostname: str | None = None
    criticality: Severity = Severity.MEDIUM
    exposed: bool | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Service(Entity):
    entity_kind: ClassVar[str] = "services"
    engagement_id: str
    asset_id: str
    protocol: str = "tcp"
    port: int | None = Field(default=None, ge=1, le=65535)
    name: str | None = None
    product: str | None = None
    version: str | None = None
    banner: str | None = None
    cpes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Identity(Entity):
    entity_kind: ClassVar[str] = "identities"
    engagement_id: str
    principal: str
    identity_type: str = "account"
    realm: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    privileged: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SoftwareComponent(Entity):
    entity_kind: ClassVar[str] = "software_components"
    engagement_id: str
    asset_id: str | None = None
    service_id: str | None = None
    name: str
    vendor: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    purl: str | None = None
    cpes: list[str] = Field(default_factory=list)
    source_evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Observation(Entity):
    entity_kind: ClassVar[str] = "observations"
    engagement_id: str
    observation_type: str
    title: str
    body: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    service_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(Entity):
    entity_kind: ClassVar[str] = "evidence"
    engagement_id: str
    evidence_type: str
    title: str
    description: str = ""
    artifact_id: str | None = None
    finding_id: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    tool_call_id: str | None = None
    execution_id: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime = Field(default_factory=utc_now)
    captured_by: str | None = None
    source_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Artifact(Entity):
    entity_kind: ClassVar[str] = "artifacts"
    engagement_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    filename: str | None = None
    media_type: str = "application/octet-stream"
    storage_path: str
    source: str | None = None
    parent_artifact_id: str | None = None
    redacted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class Remediation(Entity):
    entity_kind: ClassVar[str] = "remediations"
    engagement_id: str
    finding_id: str | None = None
    summary: str
    details: str = ""
    references: list[str] = Field(default_factory=list)
    owner: str | None = None
    due_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Finding(Entity):
    entity_kind: ClassVar[str] = "findings"
    engagement_id: str
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    status: FindingStatus = FindingStatus.CANDIDATE
    severity: Severity = Severity.INFO
    severity_rationale: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    service_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    correlation_ids: list[str] = Field(default_factory=list)
    remediation_id: str | None = None
    cve_ids: list[str] = Field(default_factory=list)
    cwe_ids: list[str] = Field(default_factory=list)
    verifier_id: str | None = None
    verified_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cve_ids")
    @classmethod
    def normalize_cve_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(not re.fullmatch(r"CVE-\d{4}-\d{4,}", value) for value in normalized):
            raise ValueError("CVE identifiers must use the CVE-YYYY-NNNN format")
        return list(dict.fromkeys(normalized))

    @field_validator("cwe_ids")
    @classmethod
    def normalize_cwe_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(not re.fullmatch(r"CWE-\d+", value) for value in normalized):
            raise ValueError("CWE identifiers must use the CWE-NNN format")
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def confirmed_findings_are_evidence_backed(self) -> "Finding":
        if self.status == FindingStatus.CONFIRMED:
            if not self.evidence_ids:
                raise ValueError("confirmed findings require evidence")
            if not self.verifier_id or not self.verified_at:
                raise ValueError("confirmed findings require verifier attribution")
        return self


class Advisory(Entity):
    entity_kind: ClassVar[str] = "advisories"
    advisory_id: str
    source: str
    title: str
    description: str = ""
    published_at: datetime | None = None
    modified_at: datetime | None = None
    cvss: dict[str, Any] = Field(default_factory=dict)
    cwes: list[str] = Field(default_factory=list)
    affected: list[dict[str, Any]] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    kev: bool = False
    epss_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    epss_percentile: float | None = Field(default=None, ge=0.0, le=1.0)
    source_snapshot_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Correlation(Entity):
    entity_kind: ClassVar[str] = "correlations"
    engagement_id: str
    component_id: str | None = None
    service_id: str | None = None
    advisory_id: str
    method: CorrelationMethod
    status: CorrelationStatus = CorrelationStatus.CANDIDATE
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    matched_identifiers: dict[str, str] = Field(default_factory=dict)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    conflicting_evidence_ids: list[str] = Field(default_factory=list)
    analyst_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fuzzy_matches_are_not_auto_confirmed(self) -> "Correlation":
        if (
            self.method == CorrelationMethod.FUZZY_BANNER
            and self.status == CorrelationStatus.CONFIRMED
            and not self.analyst_id
        ):
            raise ValueError("fuzzy banner matches require analyst confirmation")
        return self


class RunBudget(NebulaModel):
    max_concurrency: int = Field(default=1, ge=1, le=256)
    max_delegation_depth: int = Field(default=3, ge=0, le=32)
    max_duration_seconds: int = Field(default=3600, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tool_calls: int = Field(default=100, ge=0)
    max_retries: int = Field(default=2, ge=0, le=100)
    per_target_active_operations: int = Field(default=1, ge=1, le=64)


class AgentRun(Entity):
    entity_kind: ClassVar[str] = "runs"
    engagement_id: str
    objective: str
    status: RunStatus = RunStatus.QUEUED
    supervisor_provider_id: str | None = None
    supervisor_model: str | None = None
    budget: RunBudget = Field(default_factory=RunBudget)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_event_sequence: int = Field(default=0, ge=0)
    tool_pack_digests: list[str] = Field(default_factory=list)
    tool_interface_catalog_digests: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_pack_digests", "tool_interface_catalog_digests")
    @classmethod
    def valid_pack_digests(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
            raise ValueError("tool-pack digests must be lowercase SHA-256 values")
        return list(dict.fromkeys(values))


class Task(Entity):
    entity_kind: ClassVar[str] = "tasks"
    engagement_id: str
    run_id: str
    parent_task_id: str | None = None
    specialist_role: str
    title: str
    instructions: str = ""
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    assigned_agent_id: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    risk_class: RiskClass = RiskClass.LOCAL_READ
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentAttempt(Entity):
    entity_kind: ClassVar[str] = "agent_attempts"
    engagement_id: str
    run_id: str
    task_id: str
    agent_role: str
    attempt_number: int = Field(ge=1)
    provider_profile_id: str | None = None
    model: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    tokens_used: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class ToolCall(Entity):
    entity_kind: ClassVar[str] = "tool_calls"
    engagement_id: str
    run_id: str
    task_id: str | None = None
    tool_name: str
    status: ToolCallStatus = ToolCallStatus.PROPOSED
    risk_class: RiskClass
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    approval_id: str | None = None
    idempotency_key: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class Approval(Entity):
    entity_kind: ClassVar[str] = "approvals"
    engagement_id: str
    run_id: str
    task_id: str | None = None
    tool_call_id: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    risk_class: RiskClass
    exact_request: dict[str, Any]
    target: str | None = None
    credential_class: str | None = None
    expected_effects: list[str] = Field(default_factory=list)
    policy_rationale: str
    requested_by: str
    decided_by: str | None = None
    requested_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    decision_note: str | None = None


class ModelCapabilities(NebulaModel):
    streaming: bool = False
    cancellation: bool = False
    tool_calling: bool = False
    strict_structured_output: bool = False
    parallel_tool_calls: bool = False
    vision: bool = False
    documents: bool = False
    audio: bool = False
    embeddings: bool = False
    reasoning_controls: bool = False


class ProviderPrivacy(NebulaModel):
    local_only: bool = False
    retention: str | None = None
    residency: list[str] = Field(default_factory=list)
    permits_sensitive_data: bool = False


class OperatorProfile(Entity):
    """Durable local operator attribution, independent of authentication."""

    entity_kind: ClassVar[str] = "operator_profiles"
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    role: str | None = Field(default=None, max_length=200)
    active: bool = False
    activated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
            raise ValueError("email must be a valid address")
        return normalized

    @field_validator("role")
    @classmethod
    def normalize_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @field_validator("activated_at")
    @classmethod
    def activation_time_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("activated_at must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class ProviderProfile(Entity):
    entity_kind: ClassVar[str] = "providers"
    name: str
    provider_type: str
    endpoint: str | None = None
    enabled: bool = True
    is_local: bool = False
    secret_ref: str | None = None
    model_allowlist: list[str] = Field(default_factory=list)
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    privacy: ProviderPrivacy = Field(default_factory=ProviderPrivacy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("secret_ref")
    @classmethod
    def secret_must_be_an_environment_reference(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("secret_ref must use an env:NAME reference")
        return value

    @field_validator("model_allowlist")
    @classmethod
    def normalize_model_allowlist(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("model allowlist entries cannot be empty")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def provider_policy_is_coherent(self) -> "ProviderProfile":
        if self.privacy.local_only and not self.is_local:
            raise ValueError("a local-only provider profile must be marked local")
        default_model = self.metadata.get("default_model")
        if (
            isinstance(default_model, str)
            and self.model_allowlist
            and default_model not in self.model_allowlist
        ):
            raise ValueError("default model must be present in model_allowlist")
        options = self.metadata.get("options", {})
        if isinstance(options, dict):
            for key in ("context_window", "max_output_tokens"):
                value = options.get(key)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 1
                ):
                    raise ValueError(
                        f"provider option {key} must be a positive integer"
                    )
        return self


class SourceSnapshot(Entity):
    entity_kind: ClassVar[str] = "source_snapshots"
    source: str
    fetched_at: datetime = Field(default_factory=utc_now)
    source_updated_at: datetime | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_count: int = Field(default=0, ge=0)
    artifact_id: str | None = None
    cursor: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSource(Entity):
    entity_kind: ClassVar[str] = "knowledge"
    engagement_id: str
    name: str
    source_type: str
    artifact_id: str | None = None
    status: str = "ready"
    citation: str | None = None
    document_count: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRole(StringEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChatCitation(NebulaModel):
    source_id: str
    name: str
    citation: str | None = None
    artifact_id: str | None = None
    chunk_id: str
    page: int | None = Field(default=None, ge=1)
    excerpt: str = Field(max_length=320)


class ChatTokenUsage(NebulaModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ContextOwnerType(StringEnum):
    CHAT_SESSION = "chat_session"
    AGENT_RUN = "agent_run"


class ContextSnapshotStatus(StringEnum):
    READY = "ready"
    FAILED = "failed"


class ContextSourceReference(NebulaModel):
    """A provenance pointer into an authoritative transcript or mission ledger."""

    source_kind: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=200)
    sequence: int | None = Field(default=None, ge=1)


class ContextMemoryItem(NebulaModel):
    text: str = Field(min_length=1, max_length=4_000)
    sources: list[ContextSourceReference] = Field(min_length=1, max_length=64)


class ContextMemory(NebulaModel):
    """Structured, derived working memory. It is never authoritative evidence."""

    objective: str | None = Field(default=None, max_length=10_000)
    summary: str = Field(min_length=1, max_length=20_000)
    confirmed_facts: list[ContextMemoryItem] = Field(default_factory=list)
    decisions: list[ContextMemoryItem] = Field(default_factory=list)
    constraints: list[ContextMemoryItem] = Field(default_factory=list)
    corrections: list[ContextMemoryItem] = Field(default_factory=list)
    open_questions: list[ContextMemoryItem] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)


class ContextSnapshot(Entity):
    """Immutable derived context with complete canonical-source provenance."""

    entity_kind: ClassVar[str] = "context_snapshots"
    engagement_id: str
    owner_type: ContextOwnerType
    owner_id: str
    version: int = Field(default=1, ge=1)
    status: ContextSnapshotStatus
    compacted_through: int = Field(default=0, ge=0)
    memory: ContextMemory | None = None
    source_references: list[ContextSourceReference] = Field(default_factory=list)
    provider_profile_id: str
    model: str
    prompt_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    usage: ChatTokenUsage = Field(default_factory=ChatTokenUsage)
    cost_usd: float = Field(default=0.0, ge=0)
    error: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def result_is_coherent(self) -> "ContextSnapshot":
        if self.status == ContextSnapshotStatus.READY:
            if self.memory is None or not self.source_references:
                raise ValueError("ready context snapshots require memory and sources")
            if self.error is not None:
                raise ValueError("ready context snapshots cannot contain an error")
        elif not self.error:
            raise ValueError("failed context snapshots require an error")
        return self


class ChatSession(Entity):
    """A durable engagement-scoped analyst conversation."""

    entity_kind: ClassVar[str] = "chat_sessions"
    engagement_id: str
    title: str = Field(min_length=1, max_length=300)
    provider_profile_id: str
    model: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(Entity):
    """One immutable message in a durable analyst conversation."""

    entity_kind: ClassVar[str] = "chat_messages"
    engagement_id: str
    session_id: str
    sequence: int = Field(ge=1)
    role: ChatRole
    content: str = Field(min_length=1, max_length=200_000)
    provider_profile_id: str | None = None
    model: str | None = None
    usage: ChatTokenUsage | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None
    citations: list[ChatCitation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionOrigin(NebulaModel):
    kind: ExecutionOriginKind
    message_id: str | None = Field(default=None, max_length=200)
    block_ordinal: int | None = Field(default=None, ge=0, le=10_000)
    block_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selection_start_byte: int | None = Field(default=None, ge=0, le=1_000_000)
    selection_end_byte: int | None = Field(default=None, ge=0, le=1_000_000)
    execution_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def complete_origin(self) -> "ExecutionOrigin":
        if self.kind == ExecutionOriginKind.ASSISTANT_MESSAGE:
            required = (self.message_id, self.block_ordinal, self.block_sha256)
            if any(value is None for value in required):
                raise ValueError(
                    "assistant-message origins require message, block, and hash"
                )
            if self.execution_id is not None:
                raise ValueError(
                    "assistant-message origins cannot reference an execution"
                )
            if (self.selection_start_byte is None) != (self.selection_end_byte is None):
                raise ValueError("selection byte offsets must be supplied together")
            if (
                self.selection_start_byte is not None
                and self.selection_end_byte is not None
                and self.selection_end_byte <= self.selection_start_byte
            ):
                raise ValueError("selection end must be greater than selection start")
        else:
            if not self.execution_id:
                raise ValueError("rerun origins require an execution_id")
            if any(
                value is not None
                for value in (
                    self.message_id,
                    self.block_ordinal,
                    self.block_sha256,
                    self.selection_start_byte,
                    self.selection_end_byte,
                )
            ):
                raise ValueError("rerun origins cannot contain message coordinates")
        return self


class ExecutionLimitsSnapshot(NebulaModel):
    cpu_count: float = Field(default=1.0, gt=0)
    memory_mb: int = Field(default=512, ge=32)
    pids: int = Field(default=128, ge=1)
    timeout_seconds: int = Field(default=300, ge=1)
    output_bytes_per_stream: int = Field(default=2_000_000, ge=1)


class ExecutionRuntimeSnapshot(NebulaModel):
    language: str = Field(pattern=r"^(bash|sh|python)$")
    interpreter: str = Field(min_length=1, max_length=500)
    arguments: list[str] = Field(default_factory=list, max_length=32)
    tool_pack_installation_id: str = Field(min_length=1, max_length=200)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    image: str = Field(min_length=1, max_length=1000)
    runner_profile_id: str = Field(min_length=1, max_length=200)
    runner_profile_revision: int = Field(ge=1)
    runner_runtime: RunnerRuntime
    runner_isolation: RunnerIsolation
    runner_executable: str = Field(min_length=1, max_length=2048)
    runner_platform: str = Field(pattern=r"^linux/(amd64|arm64)$")
    runner_context: str | None = Field(default=None, max_length=500)
    runner_socket: str | None = Field(default=None, max_length=2048)
    trusted: bool


class ExecutionNetworkSnapshot(NebulaModel):
    mode: ExecutionNetworkMode = ExecutionNetworkMode.NONE
    target: str | None = Field(default=None, max_length=2048)
    ports: list[int] = Field(default_factory=list, max_length=1024)
    resolved_addresses: list[str] = Field(default_factory=list, max_length=64)
    scope_policy_id: str | None = Field(default=None, max_length=200)
    scope_policy_revision: int | None = Field(default=None, ge=1)

    @field_validator("ports")
    @classmethod
    def valid_network_ports(cls, values: list[int]) -> list[int]:
        if any(
            isinstance(value, bool) or value < 1 or value > 65_535 for value in values
        ):
            raise ValueError("network ports must be integers between 1 and 65535")
        return sorted(set(values))

    @model_validator(mode="after")
    def network_fields_match_mode(self) -> "ExecutionNetworkSnapshot":
        scoped = self.mode == ExecutionNetworkMode.SCOPED
        if scoped and (
            not self.target
            or not self.ports
            or not self.resolved_addresses
            or not self.scope_policy_id
            or self.scope_policy_revision is None
        ):
            raise ValueError("scoped network execution requires a pinned policy target")
        if not scoped and any(
            (self.target, self.ports, self.resolved_addresses, self.scope_policy_id)
        ):
            raise ValueError("offline execution cannot contain network scope")
        return self


class WorkspaceChange(NebulaModel):
    path: str = Field(min_length=1, max_length=4096)
    change: str = Field(pattern=r"^(added|modified|deleted)$")
    size: int | None = Field(default=None, ge=0)


class OperatorExecution(Entity):
    """One operator-confirmed, container-isolated code execution."""

    entity_kind: ClassVar[str] = "operator_executions"
    engagement_id: str
    operator_id: str
    origin: ExecutionOrigin
    language: str = Field(pattern=r"^(bash|sh|python)$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_id: str
    source_preview: str = Field(default="", max_length=4096)
    runtime: ExecutionRuntimeSnapshot
    network: ExecutionNetworkSnapshot = Field(default_factory=ExecutionNetworkSnapshot)
    limits: ExecutionLimitsSnapshot = Field(default_factory=ExecutionLimitsSnapshot)
    workspace: str = Field(default="/workspace", pattern=r"^/workspace$")
    policy_decision: str = Field(default="allowed", max_length=100)
    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_idempotency_key: str = Field(min_length=1, max_length=300)
    status: OperatorExecutionStatus = OperatorExecutionStatus.QUEUED
    error_code: str | None = Field(default=None, max_length=100)
    error_detail: str | None = Field(default=None, max_length=4000)
    queued_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    output_truncated: bool = False
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    redacted_stdout_artifact_id: str | None = None
    redacted_stderr_artifact_id: str | None = None
    manifest_artifact_id: str | None = None
    evidence_id: str | None = None
    workspace_changes: list[WorkspaceChange] = Field(
        default_factory=list, max_length=1000
    )

    @field_validator("queued_at", "started_at", "completed_at")
    @classmethod
    def execution_times_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("execution timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class PotentialFindingDraft(NebulaModel):
    title: str = Field(min_length=1, max_length=500)
    rationale: str = Field(default="", max_length=20_000)


class GeneratedDraftContent(NebulaModel):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=50_000)
    observations: list[str] = Field(default_factory=list, max_length=100)
    potential_findings: list[PotentialFindingDraft] = Field(
        default_factory=list, max_length=100
    )
    evidence_ids: list[str] = Field(default_factory=list, max_length=500)


class GeneratedDraft(Entity):
    entity_kind: ClassVar[str] = "generated_drafts"
    engagement_id: str
    execution_id: str
    provider_profile_id: str
    model: str = Field(min_length=1, max_length=500)
    prompt_version: str = Field(min_length=1, max_length=100)
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: GeneratedDraftStatus = GeneratedDraftStatus.GENERATING
    content: GeneratedDraftContent | None = None
    observation_id: str | None = None
    provider_request_id: str | None = Field(default=None, max_length=500)
    usage: ChatTokenUsage | None = None
    error_detail: str | None = Field(default=None, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Report(Entity):
    entity_kind: ClassVar[str] = "reports"
    engagement_id: str
    title: str = Field(min_length=1, max_length=500)
    status: ReportStatus = ReportStatus.DRAFT
    executive_summary: str = ""
    finding_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    signed_off_by: str | None = None
    signed_off_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def final_reports_require_complete_signoff(self) -> "Report":
        has_operator = self.signed_off_by is not None
        has_time = self.signed_off_at is not None
        if has_operator != has_time:
            raise ValueError("report signoff requires both operator and timestamp")
        if self.status == ReportStatus.FINAL and not has_operator:
            raise ValueError("final reports require operator signoff")
        if self.status != ReportStatus.FINAL and has_operator:
            raise ValueError("only final reports may contain signoff fields")
        return self


class ReportRender(Entity):
    entity_kind: ClassVar[str] = "report_renders"
    engagement_id: str
    report_id: str
    report_revision: int = Field(ge=1)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_version: str = Field(min_length=1, max_length=100)
    renderer_version: str = Field(min_length=1, max_length=100)
    font_hashes: dict[str, str] = Field(default_factory=dict)
    status: ReportRenderStatus = ReportRenderStatus.QUEUED
    snapshot_artifact_id: str | None = None
    pdf_artifact_id: str | None = None
    warnings: list[str] = Field(default_factory=list, max_length=1000)
    generated_at: datetime | None = None
    error_detail: str | None = Field(default=None, max_length=4000)

    @field_validator("generated_at")
    @classmethod
    def render_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("render timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class RunEvent(NebulaModel):
    """An immutable, monotonically sequenced event in an agent run."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    sequence: int = Field(ge=1)
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    actor_id: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    idempotency_key: str | None = None


class OperationEvent(NebulaModel):
    """An immutable event for operator workflows outside an AgentRun."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    operation_id: str = Field(min_length=1, max_length=200)
    operation_kind: str = Field(min_length=1, max_length=80)
    engagement_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    actor_id: str | None = Field(default=None, max_length=200)
    occurred_at: datetime = Field(default_factory=utc_now)
    idempotency_key: str | None = Field(default=None, max_length=300)

    @field_validator("occurred_at")
    @classmethod
    def event_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamps must include a timezone")
        return value.astimezone(timezone.utc)


ENTITY_MODELS: tuple[type[Entity], ...] = (
    Engagement,
    ScopePolicy,
    ToolPackInstallation,
    RunnerProfile,
    EngagementToolAssignment,
    Asset,
    Service,
    Identity,
    SoftwareComponent,
    Observation,
    Finding,
    Evidence,
    Artifact,
    Advisory,
    Correlation,
    Remediation,
    AgentRun,
    Task,
    AgentAttempt,
    ToolCall,
    Approval,
    OperatorProfile,
    ProviderProfile,
    SourceSnapshot,
    KnowledgeSource,
    ChatSession,
    ChatMessage,
    ContextSnapshot,
    OperatorExecution,
    GeneratedDraft,
    Report,
    ReportRender,
)

ENTITY_MODEL_BY_KIND: dict[str, type[Entity]] = {
    model.entity_kind: model for model in ENTITY_MODELS
}


def entity_engagement_id(entity: Entity) -> str | None:
    """Return an entity's owning engagement without importing storage concerns."""

    if isinstance(entity, Engagement):
        return entity.id
    value = getattr(entity, "engagement_id", None)
    return value if isinstance(value, str) else None
