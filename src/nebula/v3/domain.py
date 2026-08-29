"""Typed, UI-independent domain contracts for Nebula 3.

The relational store is authoritative.  These models are the stable boundary used
by the API, providers, policy engine, importers, and future GUI clients.
"""

from __future__ import annotations

from .diagnostics import record_caught_exception

import ipaddress
import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class StringEnum(str, Enum):
    """A string-valued enum with stable JSON serialization."""


class ResourceKind(StringEnum):
    """Stable, UI-independent names for resources that can cross surfaces."""

    PROJECT = "project"
    CONVERSATION = "conversation"
    NOTE = "note"
    SOURCE = "source"
    LIBRARY_ITEM = "library_item"
    WORKSPACE_FILE = "workspace_file"
    ASSET = "asset"
    EVIDENCE = "evidence"
    FINDING = "finding"
    REPORT = "report"
    TERMINAL_SESSION = "terminal_session"
    TERMINAL_COMMAND = "terminal_command"
    BROWSER_SESSION = "browser_session"
    BROWSER_ASSESSMENT = "browser_assessment"
    BROWSER_TAB = "browser_tab"
    BROWSER_EXCHANGE = "browser_exchange"
    MISSION = "mission"
    EXECUTION = "execution"
    APPROVAL = "approval"
    RECEIPT = "receipt"
    ARTIFACT = "artifact"


class ResourceRef(BaseModel):
    """Canonical identity passed between Core, UI, devices, and durable records."""

    model_config = ConfigDict(extra="forbid")

    project_id: str | None = Field(default=None, max_length=200)
    kind: ResourceKind
    id: str = Field(min_length=1, max_length=4096)
    revision: int | None = Field(default=None, ge=1)


class ResourceResolution(BaseModel):
    """Result of validating a canonical resource reference."""

    ref: ResourceRef
    label: str
    state: Literal["available", "deleted", "inaccessible", "wrong_project"]
    actual_project_id: str | None = None


class RelationPredicate(StringEnum):
    AFFECTS = "affects"
    SUPPORTS = "supports"
    INCLUDES = "includes"
    REFERENCES = "references"
    PRODUCED_BY = "produced_by"
    DERIVED_FROM = "derived_from"


class ResourceRelation(BaseModel):
    """One authoritative, revision-aware edge between two project resources."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=200)
    project_id: str = Field(max_length=200)
    source: ResourceRef
    predicate: RelationPredicate
    target: ResourceRef
    attribution: str | None = Field(default=None, max_length=200)
    provenance: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def endpoints_belong_to_relation_project(self) -> "ResourceRelation":
        if self.source.project_id != self.project_id:
            raise ValueError("relation source must belong to the relation project")
        if self.target.project_id != self.project_id:
            raise ValueError("relation target must belong to the relation project")
        if self.source.kind == self.target.kind and self.source.id == self.target.id:
            raise ValueError("resource relations cannot be self-referential")
        return self


class ResourceRelationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ResourceRef
    predicate: RelationPredicate
    target: ResourceRef
    attribution: str | None = Field(default=None, max_length=200)
    provenance: dict[str, Any] = Field(default_factory=dict)


class ResourceRelationSet(BaseModel):
    """Atomic desired-edge reconciliation for one source and predicate."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(max_length=200)
    source: ResourceRef
    predicate: RelationPredicate
    targets: list[ResourceRef] = Field(default_factory=list, max_length=500)
    expected_source_revision: int | None = Field(default=None, ge=1)
    attribution: str | None = Field(default=None, max_length=200)
    provenance: dict[str, Any] = Field(default_factory=dict)


class ActionAuthority(StringEnum):
    UI = "ui"
    CORE = "core"
    DEVICE = "device"


class ActionRisk(StringEnum):
    SAFE = "safe"
    MUTATING = "mutating"
    RISKY = "risky"


class ActionConfirmationPolicy(StringEnum):
    NONE = "none"
    MUTATION = "mutation"
    ALWAYS = "always"


class ActionDescriptor(BaseModel):
    """One shared verb after Core capability and security resolution."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    accepted_resource_kinds: list[ResourceKind] = Field(min_length=1)
    result_kind: ResourceKind | None = None
    authority: ActionAuthority
    required_capabilities: list[str] = Field(default_factory=list)
    risk: ActionRisk = ActionRisk.SAFE
    confirmation_policy: ActionConfirmationPolicy = ActionConfirmationPolicy.NONE
    available: bool = True
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def availability_has_a_reason(self) -> "ActionDescriptor":
        if self.available and self.disabled_reason is not None:
            raise ValueError("available actions cannot have a disabled reason")
        if not self.available and not self.disabled_reason:
            raise ValueError("unavailable actions require a disabled reason")
        return self


class ActionResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources: list[ResourceRef] = Field(min_length=1, max_length=100)
    device_id: str | None = Field(default=None, max_length=200)
    device_capabilities: list[str] = Field(default_factory=list, max_length=200)


class SearchScope(StringEnum):
    ACTIVE = "active"
    ALL = "all"


class SearchResult(BaseModel):
    """One ranked, canonical omnibox result with resolved applicable actions."""

    ref: ResourceRef
    project: str
    label: str
    description: str = ""
    snippet: str = ""
    breadcrumb: str = ""
    updated_at: datetime
    score: float
    actions: list[ActionDescriptor] = Field(default_factory=list)


class SearchResponse(BaseModel):
    items: list[SearchResult]
    next_cursor: str | None = None
    partial_index: bool = False


class EngagementStatus(StringEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"
    ARCHIVED = "archived"


class ScopeImportStatus(StringEnum):
    GENERATING = "generating"
    READY = "ready"
    APPLIED = "applied"
    DISCARDED = "discarded"
    FAILED = "failed"


class ScopeImportClassification(StringEnum):
    ALLOWED = "allowed"
    EXCLUDED = "excluded"
    AMBIGUOUS = "ambiguous"


class ScopeImportTargetType(StringEnum):
    CIDR = "cidr"
    DOMAIN = "domain"
    URL = "url"


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


class BrowserSessionStatus(StringEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class BrowserCaptureMode(StringEnum):
    METADATA = "metadata"
    HEADERS = "headers"
    BODIES = "bodies"


class BrowserActionKind(StringEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"
    EXTRACT = "extract"
    SCREENSHOT = "screenshot"
    REPLAY = "replay"


class BrowserActionStatus(StringEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETE = "complete"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class BrowserAutomationLeaseStatus(StringEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"
    COMPLETE = "complete"
    FAILED = "failed"


class BrowserCommandStatus(StringEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    COMPLETE = "complete"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class BrowserHandoffStatus(StringEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class BrowserAssessmentProfile(StringEnum):
    EXPLORE = "explore"
    STANDARD = "standard"
    DEEP = "deep"
    API = "api"
    VALIDATION = "validation"


class BrowserAssessmentStatus(StringEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    WAITING_OPERATOR = "waiting_operator"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETE = "complete"
    FAILED = "failed"
    REVOKED = "revoked"


class BrowserAssessmentPhase(StringEnum):
    PREFLIGHT = "preflight"
    DISCOVERY = "discovery"
    CRAWL = "crawl"
    PASSIVE_AUDIT = "passive_audit"
    ACTIVE_AUDIT = "active_audit"
    VALIDATION = "validation"
    REPORTING = "reporting"
    COMPLETE = "complete"


class BrowserAssessmentStepStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_OPERATOR = "waiting_operator"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BrowserIssueValidationStatus(StringEnum):
    UNVALIDATED = "unvalidated"
    QUEUED = "queued"
    VALIDATING = "validating"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class BrowserEngineState(StringEnum):
    READY = "ready"
    DEGRADED = "degraded"
    PREPARING = "preparing"
    UNAVAILABLE = "unavailable"


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
    INTERRUPTED = "interrupted"
    COMPLETE = "complete"


class RunBackend(StringEnum):
    NATIVE = "native"
    HARNESS = "harness"


class ChatBackend(StringEnum):
    PROVIDER = "provider"
    HARNESS = "harness"


class HarnessKind(StringEnum):
    CODEX_APP_SERVER = "codex_app_server"
    GROK_ACP = "grok_acp"
    # Retained only so historical profiles and activity records remain readable.
    CLAUDE_AGENT_SDK = "claude_agent_sdk"


PROVIDED_HARNESS_KINDS = frozenset({HarnessKind.CODEX_APP_SERVER, HarnessKind.GROK_ACP})


class HarnessConnectionMode(StringEnum):
    SPAWN = "spawn"
    ENDPOINT = "endpoint"


class HarnessTransport(StringEnum):
    STDIO = "stdio"
    UNIX = "unix"
    WEBSOCKET = "websocket"


class HarnessAuthMode(StringEnum):
    EXISTING_SESSION = "existing_session"
    SECRET_REF = "secret_ref"
    ENDPOINT_BEARER = "endpoint_bearer"


class HarnessWorkspaceAccess(StringEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


class HarnessSessionStatus(StringEnum):
    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CLOSED = "closed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class HarnessTurnOrigin(StringEnum):
    CHAT = "chat"
    MISSION = "mission"
    ANALYSIS = "analysis"


class HarnessTurnStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class HarnessInteractionKind(StringEnum):
    USER_INPUT = "user_input"
    MCP_ELICITATION = "mcp_elicitation"


class HarnessInteractionStatus(StringEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class McpTransport(StringEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class McpAuthMode(StringEnum):
    NONE = "none"
    BEARER = "bearer"
    HEADERS = "headers"


class McpApprovalMode(StringEnum):
    RISK_BASED = "risk_based"
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class McpCwdPolicy(StringEnum):
    WORKSPACE = "workspace"
    FIXED = "fixed"


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


class ToolCallOrigin(StringEnum):
    MISSION = "mission"
    CHAT = "chat"


class ApprovalStatus(StringEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ProviderVerificationStatus(StringEnum):
    VERIFIED = "verified"
    FAILED = "failed"


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


class AutomationApprovalPolicy(StringEnum):
    """When a project command requires an operator decision."""

    ALWAYS = "always"
    ON_BOUNDARY = "on_boundary"
    NEVER = "never"


class AutomationNetworkMode(StringEnum):
    """Network boundary requested by the general command primitive."""

    NONE = "none"
    PROJECT_SCOPE = "project_scope"


class AutomationSessionStatus(StringEnum):
    STARTING = "starting"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class CommandExecutionStatus(StringEnum):
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ExecutionOriginKind(StringEnum):
    ASSISTANT_MESSAGE = "assistant_message"
    RERUN = "rerun"
    SELECTION = "selection"


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
    allow_all_targets: bool = False
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
            candidate = value.strip()
            if "://" in candidate:
                parsed = urlsplit(candidate)
                if (
                    parsed.scheme.lower() not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port is not None
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError(
                        f"domain URL must contain only an HTTP(S) hostname: {value}"
                    )
                candidate = parsed.hostname
            try:
                domain = candidate.rstrip(".").encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise ValueError(f"invalid domain: {value}") from exc
            if not domain_pattern.fullmatch(domain):
                raise ValueError(f"invalid domain: {value}")
            normalized.append(domain)
        return sorted(set(normalized))

    @field_validator("allowed_ports")
    @classmethod
    def normalize_ports(cls, values: list[int]) -> list[int]:
        for value in values:
            if not 0 <= value <= 65535:
                raise ValueError("ports must be between 0 and 65535")
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
                record_caught_exception(
                    "projects",
                    "projects.domain.caught_failure_001",
                    "A handled projects operation raised an exception.",
                    exc,
                    stage="domain",
                )
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
                record_caught_exception(
                    "projects",
                    "projects.domain.caught_failure_002",
                    "A handled projects operation raised an exception.",
                    exc,
                    stage="domain",
                )
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
    workspace_path: str | None = Field(default=None, max_length=4096)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace_path")
    @classmethod
    def workspace_path_must_be_absolute(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() or candidate == Path("/"):
            raise ValueError("workspace_path must be an absolute non-root folder")
        return str(candidate)


class AutomationProjectPolicy(Entity):
    """Project-wide policy for the fixed automation command runtime."""

    entity_kind: ClassVar[str] = "automation_policies"
    engagement_id: str
    approval_policy: AutomationApprovalPolicy = AutomationApprovalPolicy.ON_BOUNDARY
    network_enabled: bool = True
    runner_profile_id: str | None = Field(default=None, max_length=200)
    max_timeout_ms: int = Field(default=300_000, ge=1_000, le=86_400_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AutomationSession(Entity):
    """One isolated OCI environment owned by an agent session."""

    entity_kind: ClassVar[str] = "automation_sessions"
    engagement_id: str
    owner_kind: str = Field(pattern=r"^(chat|mission|harness|api)$")
    owner_id: str = Field(min_length=1, max_length=200)
    runtime_image: str = Field(min_length=1, max_length=1_000)
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runner_profile_id: str = Field(min_length=1, max_length=200)
    runner_profile_revision: int = Field(ge=1)
    policy_id: str = Field(min_length=1, max_length=200)
    policy_revision: int = Field(ge=1)
    scope_policy_id: str | None = Field(default=None, max_length=200)
    scope_policy_revision: int | None = Field(default=None, ge=1)
    status: AutomationSessionStatus = AutomationSessionStatus.STARTING
    network_granted: bool = False
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    failure_detail: str | None = Field(default=None, max_length=4_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def scope_snapshot_is_complete(self) -> "AutomationSession":
        if (self.scope_policy_id is None) != (self.scope_policy_revision is None):
            raise ValueError("automation scope id and revision must be stored together")
        return self


class WorkspaceChange(NebulaModel):
    path: str = Field(min_length=1, max_length=4096)
    change: str = Field(pattern=r"^(added|modified|deleted)$")
    size: int | None = Field(default=None, ge=0)


class CommandExecution(Entity):
    """Durable receipt for one Bash process in an automation session."""

    entity_kind: ClassVar[str] = "command_executions"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    process_id: str = Field(min_length=1, max_length=200)
    command: str = Field(min_length=1, max_length=200_000)
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cwd: str = Field(default=".", min_length=1, max_length=4_096)
    network: AutomationNetworkMode = AutomationNetworkMode.NONE
    background: bool = False
    status: CommandExecutionStatus = CommandExecutionStatus.RUNNING
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy_revision: int = Field(ge=1)
    scope_policy_revision: int | None = Field(default=None, ge=1)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    exit_code: int | None = None
    stdout_artifact_id: str | None = Field(default=None, max_length=200)
    stderr_artifact_id: str | None = Field(default=None, max_length=200)
    redacted_stdout_artifact_id: str | None = Field(default=None, max_length=200)
    redacted_stderr_artifact_id: str | None = Field(default=None, max_length=200)
    observed_stdout_bytes: int = Field(default=0, ge=0)
    observed_stderr_bytes: int = Field(default=0, ge=0)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    workspace_changes: list[WorkspaceChange] = Field(
        default_factory=list, max_length=1_000
    )
    error: str | None = Field(default=None, max_length=4_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class BrowserTabState(NebulaModel):
    """Durable, non-secret representation of one native browser tab."""

    id: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=16_384)
    title: str = Field(default="New tab", max_length=500)
    position: int = Field(default=0, ge=0, le=255)
    last_scope_state: Literal[
        "in_scope", "out_of_scope", "inactive", "unconfigured", "unknown"
    ] = "unknown"
    last_scope_revision: int | None = Field(default=None, ge=1)

    @field_validator("url")
    @classmethod
    def browser_tab_url_is_network_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("browser tab URLs must use http or https")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("browser tab URLs cannot contain credentials")
        return value


class BrowserIdentity(Entity):
    """Named native storage partition; cookie material is never stored here."""

    entity_kind: ClassVar[str] = "browser_identities"
    engagement_id: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    color: str = Field(default="#7c6cff", pattern=r"^#[0-9a-fA-F]{6}$")
    storage_partition: str = Field(
        default_factory=lambda: f"browser-{uuid4()}",
        pattern=r"^browser-[0-9a-f-]{36}$",
    )
    ephemeral: bool = False
    is_default: bool = False
    revoked_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("revoked_at")
    @classmethod
    def browser_identity_revocation_is_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser identity revocation must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class BrowserSession(Entity):
    """Durable research timeline; live cookies remain in the identity partition."""

    entity_kind: ClassVar[str] = "browser_sessions"
    engagement_id: str
    name: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    status: BrowserSessionStatus = BrowserSessionStatus.ACTIVE
    capture_mode: BrowserCaptureMode = BrowserCaptureMode.HEADERS
    proxy_enabled: bool = False
    proxy_trust_acknowledged: bool = False
    tabs: list[BrowserTabState] = Field(default_factory=list, max_length=16)
    active_tab_id: str | None = Field(default=None, max_length=200)
    upstream_proxy_enabled: bool = False
    upstream_proxy_url: str | None = Field(default=None, max_length=2_048)
    upstream_proxy_credential_ref: str | None = Field(default=None, max_length=200)
    interception_enabled: bool = False
    device_owner: str | None = Field(default=None, max_length=200)
    last_seen_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("upstream_proxy_url")
    @classmethod
    def browser_upstream_proxy_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https", "socks5"}
            or not parsed.hostname
        ):
            raise ValueError("upstream proxy must use http, https, or socks5")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("upstream proxy credentials require protected storage")
        return value

    @field_validator("upstream_proxy_credential_ref")
    @classmethod
    def browser_upstream_credential_ref_is_safe(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip() or any(character.isspace() for character in value)
        ):
            raise ValueError("upstream proxy credentials require an opaque reference")
        return value

    @field_validator("last_seen_at")
    @classmethod
    def browser_last_seen_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("browser last_seen_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def browser_active_tab_exists(self) -> "BrowserSession":
        ids = [tab.id for tab in self.tabs]
        if len(ids) != len(set(ids)):
            raise ValueError("browser tab ids must be unique")
        if self.active_tab_id is not None and self.active_tab_id not in ids:
            raise ValueError("active browser tab must belong to the session")
        return self


class BrowserEngineCapability(NebulaModel):
    """Normalized readiness receipt for one desktop browser or scanner adapter."""

    adapter: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,80}$")
    display_name: str = Field(min_length=1, max_length=120)
    contract_version: str = Field(default="1", pattern=r"^[1-9][0-9]*$")
    state: BrowserEngineState
    installed_version: str | None = Field(default=None, max_length=120)
    digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    actions: list[str] = Field(default_factory=list, max_length=200)
    protocols: list[str] = Field(default_factory=list, max_length=40)
    check_families: list[str] = Field(default_factory=list, max_length=200)
    unavailability_reason: str | None = Field(default=None, max_length=2_000)
    recovery_action: str | None = Field(default=None, max_length=500)
    desktop_only: bool = True

    @model_validator(mode="after")
    def browser_engine_readiness_is_actionable(self) -> "BrowserEngineCapability":
        if self.state != BrowserEngineState.READY:
            if not self.unavailability_reason or not self.recovery_action:
                raise ValueError(
                    "unready browser engines require a reason and recovery action"
                )
        return self


class BrowserAssessmentBudget(NebulaModel):
    max_requests: int = Field(default=2_000, ge=1, le=1_000_000)
    max_actions: int = Field(default=500, ge=1, le=100_000)
    max_duration_seconds: int = Field(default=3_600, ge=60, le=86_400)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    requests_used: int = Field(default=0, ge=0)
    actions_used: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def browser_assessment_budget_is_coherent(self) -> "BrowserAssessmentBudget":
        if self.requests_used > self.max_requests:
            raise ValueError("assessment request usage exceeds its budget")
        if self.actions_used > self.max_actions:
            raise ValueError("assessment action usage exceeds its budget")
        return self


class BrowserAssessmentCoverage(NebulaModel):
    discovered_urls: int = Field(default=0, ge=0)
    visited_urls: int = Field(default=0, ge=0)
    analyzed_exchanges: int = Field(default=0, ge=0)
    discovered_forms: int = Field(default=0, ge=0)
    discovered_apis: int = Field(default=0, ge=0)
    websocket_channels: int = Field(default=0, ge=0)


class BrowserAssessment(Entity):
    """Durable authority for one guided or expert Security Browser assessment."""

    entity_kind: ClassVar[str] = "browser_assessments"
    engagement_id: str
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4_000)
    profile: BrowserAssessmentProfile = BrowserAssessmentProfile.STANDARD
    session_id: str = Field(min_length=1, max_length=200)
    identity_ids: list[str] = Field(min_length=1, max_length=32)
    primary_identity_id: str = Field(min_length=1, max_length=200)
    target_urls: list[str] = Field(min_length=1, max_length=100)
    scope_policy_id: str = Field(min_length=1, max_length=200)
    scope_policy_revision: int = Field(ge=1)
    risk_classes: list[str] = Field(default_factory=list, max_length=40)
    validation_grant_id: str | None = Field(default=None, max_length=200)
    credential_refs: list[str] = Field(default_factory=list, max_length=32)
    status: BrowserAssessmentStatus = BrowserAssessmentStatus.DRAFT
    phase: BrowserAssessmentPhase = BrowserAssessmentPhase.PREFLIGHT
    progress: float = Field(default=0, ge=0, le=1)
    budget: BrowserAssessmentBudget = Field(default_factory=BrowserAssessmentBudget)
    coverage: BrowserAssessmentCoverage = Field(
        default_factory=BrowserAssessmentCoverage
    )
    engines: list[BrowserEngineCapability] = Field(default_factory=list, max_length=16)
    run_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=1_000)
    candidate_ids: list[str] = Field(default_factory=list, max_length=1_000)
    active_step_id: str | None = Field(default=None, max_length=200)
    control_owner: Literal["nebula", "operator"] = "nebula"
    pause_reason: str | None = Field(default=None, max_length=2_000)
    failure: str | None = Field(default=None, max_length=4_000)
    recovery_action: str | None = Field(default=None, max_length=1_000)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_event_cursor: int = Field(default=0, ge=0)
    created_by: str = Field(min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_urls")
    @classmethod
    def browser_assessment_targets_are_network_urls(
        cls, values: list[str]
    ) -> list[str]:
        normalized: list[str] = []
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "assessment targets must use credential-free HTTP(S) URLs"
                )
            normalized.append(value)
        return list(dict.fromkeys(normalized))

    @field_validator("credential_refs")
    @classmethod
    def browser_assessment_credentials_are_references(
        cls, values: list[str]
    ) -> list[str]:
        if any(
            not value.strip() or any(char.isspace() for char in value)
            for value in values
        ):
            raise ValueError("assessment credentials must be opaque references")
        return list(dict.fromkeys(values))

    @field_validator("started_at", "completed_at")
    @classmethod
    def browser_assessment_time_is_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("assessment timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def browser_assessment_is_coherent(self) -> "BrowserAssessment":
        if self.primary_identity_id not in self.identity_ids:
            raise ValueError("primary assessment identity must be selected")
        if (
            self.profile == BrowserAssessmentProfile.VALIDATION
            and not self.validation_grant_id
        ):
            raise ValueError("validation profile requires an issue-specific grant")
        if self.failure and not self.recovery_action:
            raise ValueError("assessment failures require a recovery action")
        return self


class BrowserAssessmentStep(Entity):
    entity_kind: ClassVar[str] = "browser_assessment_steps"
    engagement_id: str
    assessment_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=200)
    intent: str = Field(min_length=1, max_length=4_000)
    capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,80}$")
    target: str = Field(min_length=1, max_length=16_384)
    status: BrowserAssessmentStepStatus = BrowserAssessmentStepStatus.QUEUED
    approval_id: str | None = Field(default=None, max_length=200)
    retry_classification: Literal[
        "safe_before_side_effect", "never", "operator_review"
    ] = "safe_before_side_effect"
    action_token: str | None = Field(default=None, max_length=300)
    pre_fingerprint: str | None = Field(default=None, max_length=500)
    post_fingerprint: str | None = Field(default=None, max_length=500)
    trace_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    error: str | None = Field(default=None, max_length=4_000)
    recovery_action: str | None = Field(default=None, max_length=1_000)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def browser_assessment_step_is_coherent(self) -> "BrowserAssessmentStep":
        if self.error and not self.recovery_action:
            raise ValueError("assessment step failures require a recovery action")
        if self.post_fingerprint and not self.pre_fingerprint:
            raise ValueError(
                "post-action fingerprint requires a pre-action fingerprint"
            )
        return self


class BrowserLoginFlow(Entity):
    """Recorded non-secret login workflow bound to a local browser identity."""

    entity_kind: ClassVar[str] = "browser_login_flows"
    engagement_id: str
    name: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    credential_refs: list[str] = Field(default_factory=list, max_length=32)
    success_verifier: dict[str, Any] = Field(default_factory=dict)
    health: Literal["unknown", "healthy", "expired", "failed"] = "unknown"
    last_validated_at: datetime | None = None
    failure: str | None = Field(default=None, max_length=4_000)
    recovery_action: str | None = Field(default=None, max_length=1_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("credential_refs")
    @classmethod
    def browser_login_credentials_are_references(cls, values: list[str]) -> list[str]:
        if any(
            not value.strip() or any(char.isspace() for char in value)
            for value in values
        ):
            raise ValueError("login credentials must be opaque references")
        return list(dict.fromkeys(values))

    @field_validator("last_validated_at")
    @classmethod
    def browser_login_validation_time_is_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("login validation timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def browser_login_flow_is_non_secret_and_actionable(self) -> "BrowserLoginFlow":
        bound_credentials = set(self.credential_refs)
        forbidden_keys = {
            "password",
            "secret",
            "access_token",
            "refresh_token",
            "authorization",
            "cookie",
        }
        for step in self.steps:
            if not isinstance(step, dict):
                raise ValueError("login flow steps must be structured records")
            if forbidden_keys.intersection(key.lower() for key in step):
                raise ValueError(
                    "login flow steps cannot contain secret-bearing fields"
                )
            credential_ref = step.get("credential_ref")
            if credential_ref is not None and credential_ref not in bound_credentials:
                raise ValueError("login step credential_ref must be bound to the flow")
        if self.failure and not self.recovery_action:
            raise ValueError("login flow failures require a recovery action")
        return self


class BrowserRecipeStage(NebulaModel):
    id: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "crawl",
        "passive_scan",
        "active_scan",
        "request",
        "fuzz",
        "compare",
        "validate",
        "wait_for_operator",
        "capture_evidence",
        "export",
    ]
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    configuration: dict[str, Any] = Field(default_factory=dict)
    risk_class: str = Field(default="safe", max_length=80)
    request_budget: int = Field(default=0, ge=0, le=1_000_000)


class BrowserRecipe(Entity):
    entity_kind: ClassVar[str] = "browser_recipes"
    engagement_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4_000)
    version: int = Field(default=1, ge=1)
    builtin: bool = False
    capability_requirements: list[str] = Field(default_factory=list, max_length=100)
    stages: list[BrowserRecipeStage] = Field(min_length=1, max_length=200)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def browser_recipe_is_a_dag(self) -> "BrowserRecipe":
        ids = [stage.id for stage in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("browser recipe stage ids must be unique")
        known: set[str] = set()
        for stage in self.stages:
            if any(dependency not in known for dependency in stage.depends_on):
                raise ValueError(
                    "browser recipe dependencies must reference earlier stages"
                )
            known.add(stage.id)
        return self


class BrowserIssueCandidate(Entity):
    entity_kind: ClassVar[str] = "browser_issue_candidates"
    engagement_id: str
    assessment_id: str = Field(min_length=1, max_length=200)
    rule_id: str = Field(min_length=1, max_length=200)
    check_family: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    cwe: str | None = Field(default=None, pattern=r"^CWE-[1-9][0-9]{0,4}$")
    target_url: str = Field(min_length=1, max_length=16_384)
    insertion_point: str | None = Field(default=None, max_length=1_000)
    severity: Severity
    confidence: Literal["tentative", "firm", "certain"] = "tentative"
    control_results: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    deduplication_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_status: BrowserIssueValidationStatus = (
        BrowserIssueValidationStatus.UNVALIDATED
    )
    validation_grant_id: str | None = Field(default=None, max_length=200)
    promoted_finding_id: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_url")
    @classmethod
    def browser_candidate_target_is_network_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("candidate target must be a credential-free HTTP(S) URL")
        return value


class BrowserValidationGrant(Entity):
    """Issue-specific, target-specific authority for bounded validation traffic."""

    entity_kind: ClassVar[str] = "browser_validation_grants"
    engagement_id: str
    assessment_id: str = Field(min_length=1, max_length=200)
    candidate_id: str = Field(min_length=1, max_length=200)
    target_url: str = Field(min_length=1, max_length=16_384)
    technique: str = Field(min_length=1, max_length=1_000)
    max_requests: int = Field(ge=1, le=10_000)
    requests_used: int = Field(default=0, ge=0)
    duration_seconds: int = Field(ge=30, le=3_600)
    granted_by: str = Field(min_length=1, max_length=200)
    granted_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    revoked_at: datetime | None = None
    status: Literal["active", "revoked", "expired", "consumed"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_url")
    @classmethod
    def browser_validation_target_is_network_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("validation target must be a credential-free HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def browser_validation_grant_is_coherent(self) -> "BrowserValidationGrant":
        if self.expires_at <= self.granted_at:
            raise ValueError("validation grant expiry must follow its grant time")
        if self.requests_used > self.max_requests:
            raise ValueError("validation grant request usage exceeds its budget")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked validation grants require a revocation time")
        return self


class BrowserTrafficExchange(Entity):
    """Bounded traffic index. Raw bodies live only in integrity-checked artifacts."""

    entity_kind: ClassVar[str] = "browser_traffic"
    engagement_id: str
    assessment_id: str | None = Field(default=None, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    tab_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    method: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url: str = Field(min_length=1, max_length=16_384)
    protocol: Literal["http/1.0", "http/1.1", "h2", "h3", "websocket", "unknown"] = (
        "unknown"
    )
    status_code: int | None = Field(default=None, ge=100, le=999)
    request_headers: dict[str, str] = Field(default_factory=dict)
    response_headers: dict[str, str] = Field(default_factory=dict)
    # Ordered lines preserve duplicate headers for protocol-aware replay while
    # the maps above remain the backwards-compatible summary representation.
    request_header_lines: list[tuple[str, str]] = Field(
        default_factory=list, max_length=200
    )
    response_header_lines: list[tuple[str, str]] = Field(
        default_factory=list, max_length=200
    )
    http2_pseudo_headers: list[tuple[str, str]] = Field(
        default_factory=list, max_length=16
    )
    request_body_artifact_id: str | None = Field(default=None, max_length=200)
    response_body_artifact_id: str | None = Field(default=None, max_length=200)
    request_bytes: int | None = Field(default=None, ge=0)
    response_bytes: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    timing: dict[str, int] = Field(default_factory=dict)
    rule_effect_ids: list[str] = Field(default_factory=list, max_length=200)
    scope_state: Literal["in_scope", "out_of_scope", "inactive", "unconfigured"]
    scope_policy_id: str = Field(min_length=1, max_length=200)
    scope_policy_revision: int = Field(ge=1)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    replay_of_exchange_id: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=4_000)
    blocked: bool = False
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def browser_exchange_url_is_network_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https", "ws", "wss"}
            or not parsed.hostname
        ):
            raise ValueError("traffic URLs must use http, https, ws, or wss")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("traffic URLs cannot contain credentials")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def browser_exchange_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser exchange timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class BrowserWebSocketFrame(Entity):
    entity_kind: ClassVar[str] = "browser_websocket_frames"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    exchange_id: str = Field(min_length=1, max_length=200)
    direction: Literal["client", "server"]
    opcode: Literal["text", "binary", "ping", "pong", "close"]
    payload_preview: str = Field(default="", max_length=2_000)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_artifact_id: str | None = Field(default=None, max_length=200)
    payload_bytes: int = Field(ge=0)
    observed_at: datetime = Field(default_factory=utc_now)
    truncated: bool = False

    @field_validator("observed_at")
    @classmethod
    def websocket_observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("websocket frame timestamp must include a timezone")
        return value.astimezone(timezone.utc)


class BrowserAction(Entity):
    """Inert AI/operator proposal and its deterministic execution receipt."""

    entity_kind: ClassVar[str] = "browser_actions"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    tab_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    kind: BrowserActionKind
    status: BrowserActionStatus = BrowserActionStatus.PROPOSED
    locator: dict[str, str] = Field(default_factory=dict)
    arguments: dict[str, Any] = Field(default_factory=dict)
    proposal: str = Field(min_length=1, max_length=4_000)
    proposed_by: str = Field(min_length=1, max_length=200)
    page_url: str = Field(min_length=1, max_length=16_384)
    scope_policy_id: str = Field(min_length=1, max_length=200)
    scope_policy_revision: int = Field(ge=1)
    action_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: str | None = Field(default=None, max_length=200)
    approved_at: datetime | None = None
    expires_at: datetime
    completed_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    error: str | None = Field(default=None, max_length=4_000)

    @field_validator("page_url")
    @classmethod
    def browser_action_page_is_network_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("browser actions require an http or https page")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("browser action page cannot contain credentials")
        return value

    @field_validator("approved_at", "expires_at", "completed_at")
    @classmethod
    def browser_action_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser action timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def browser_action_approval_is_coherent(self) -> "BrowserAction":
        approved = self.status in {
            BrowserActionStatus.APPROVED,
            BrowserActionStatus.EXECUTING,
            BrowserActionStatus.COMPLETE,
            BrowserActionStatus.FAILED,
        }
        if approved and (self.approved_by is None or self.approved_at is None):
            raise ValueError("approved browser actions require approval attribution")
        if self.expires_at <= self.created_at:
            raise ValueError("browser action expiry must follow creation")
        return self


class BrowserAutomationLease(Entity):
    """Run-scoped authority for native browser and proxy automation."""

    entity_kind: ClassVar[str] = "browser_automation_leases"
    engagement_id: str
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    scope_policy_id: str = Field(min_length=1, max_length=200)
    scope_policy_revision: int = Field(ge=1)
    target_urls: list[str] = Field(min_length=1, max_length=256)
    allowed_risk_classes: list[RiskClass] = Field(min_length=1, max_length=16)
    credential_refs: list[str] = Field(default_factory=list, max_length=64)
    max_commands: int = Field(default=100, ge=1, le=100_000)
    max_requests: int = Field(default=1_000, ge=1, le=1_000_000)
    max_body_bytes: int = Field(default=1_048_576, ge=0, le=8_388_608)
    commands_used: int = Field(default=0, ge=0)
    requests_used: int = Field(default=0, ge=0)
    status: BrowserAutomationLeaseStatus = BrowserAutomationLeaseStatus.ACTIVE
    expires_at: datetime
    last_heartbeat_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None
    stop_reason: str | None = Field(default=None, max_length=4_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_urls")
    @classmethod
    def browser_lease_targets_are_network_only(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError(
                    "browser automation targets must be credential-free HTTP(S) URLs"
                )
            normalized.append(value)
        return list(dict.fromkeys(normalized))

    @field_validator("expires_at", "last_heartbeat_at", "revoked_at")
    @classmethod
    def browser_lease_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(
                "browser automation lease timestamps must include a timezone"
            )
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def browser_lease_is_coherent(self) -> "BrowserAutomationLease":
        if self.expires_at <= self.created_at:
            raise ValueError("browser automation lease expiry must follow creation")
        if self.commands_used > self.max_commands:
            raise ValueError("browser automation command usage exceeds its budget")
        if self.requests_used > self.max_requests:
            raise ValueError("browser automation request usage exceeds its budget")
        return self


class BrowserCommand(Entity):
    """Durable, idempotent bridge from Core tools to the native browser worker."""

    entity_kind: ClassVar[str] = "browser_commands"
    engagement_id: str
    assessment_id: str | None = Field(default=None, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    lease_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    tab_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,80}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_page_url: str | None = Field(default=None, max_length=16_384)
    expected_tab_revision: int | None = Field(default=None, ge=1)
    status: BrowserCommandStatus = BrowserCommandStatus.QUEUED
    claimed_by_device_id: str | None = Field(default=None, max_length=200)
    claim_token: str | None = Field(default=None, max_length=200)
    claimed_at: datetime | None = None
    claim_expires_at: datetime | None = None
    expires_at: datetime
    result: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    error: str | None = Field(default=None, max_length=4_000)
    idempotency_key: str | None = Field(default=None, max_length=300)

    @field_validator("expected_page_url")
    @classmethod
    def browser_command_page_is_network_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "browser command pages must use credential-free HTTP(S) URLs"
            )
        return value

    @field_validator("claimed_at", "claim_expires_at", "expires_at")
    @classmethod
    def browser_command_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser command timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def browser_command_claim_is_coherent(self) -> "BrowserCommand":
        claimed = self.status == BrowserCommandStatus.CLAIMED
        if claimed and not self.claimed_by_device_id:
            raise ValueError("claimed browser commands require a device id")
        if claimed and not self.claim_token:
            raise ValueError("claimed browser commands require a claim token")
        if self.expires_at <= self.created_at:
            raise ValueError("browser command expiry must follow creation")
        return self


class BrowserProxyRule(Entity):
    """Declarative, bounded, run-owned native proxy rule."""

    entity_kind: ClassVar[str] = "browser_proxy_rules"
    engagement_id: str
    run_id: str = Field(min_length=1, max_length=200)
    lease_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    match: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=10_000)
    enabled: bool = True
    expires_at: datetime
    disabled_at: datetime | None = None
    disabled_reason: str | None = Field(default=None, max_length=4_000)

    @field_validator("expires_at", "disabled_at")
    @classmethod
    def browser_rule_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser proxy rule timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @field_validator("match", "action")
    @classmethod
    def browser_rule_payload_is_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False)) > 32_000:
            raise ValueError("browser proxy rule payload is too large")
        return value

    @model_validator(mode="after")
    def browser_rule_is_coherent(self) -> "BrowserProxyRule":
        if self.expires_at <= self.created_at:
            raise ValueError("browser proxy rule expiry must follow creation")
        return self


class BrowserSiteNode(Entity):
    """One normalized target-map location discovered by browser research."""

    entity_kind: ClassVar[str] = "browser_site_nodes"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=16_384)
    method: str = Field(default="GET", pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    kind: Literal["page", "api", "form", "resource", "websocket"] = "page"
    discovery_source: Literal[
        "browser", "proxy", "crawl", "repeater", "intruder", "har", "automation"
    ] = "proxy"
    status_code: int | None = Field(default=None, ge=100, le=999)
    parameter_names: list[str] = Field(default_factory=list, max_length=256)
    content_type: str | None = Field(default=None, max_length=500)
    scope_policy_id: str = Field(min_length=1, max_length=200)
    scope_policy_revision: int = Field(ge=1)
    last_exchange_id: str | None = Field(default=None, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def browser_site_url_is_network_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https", "ws", "wss"}
            or not parsed.hostname
        ):
            raise ValueError("browser site-map URLs must use HTTP(S) or WebSocket")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("browser site-map URLs cannot contain credentials")
        return value


class BrowserSiteEdge(Entity):
    entity_kind: ClassVar[str] = "browser_site_edges"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    source_node_id: str = Field(min_length=1, max_length=200)
    target_node_id: str = Field(min_length=1, max_length=200)
    relation: Literal["navigation", "link", "form", "redirect", "request"]
    discovered_by: Literal["browser", "crawl", "har", "automation"] = "browser"
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserCrawlJob(Entity):
    """Bounded crawl intent and acknowledged native-worker progress."""

    entity_kind: ClassVar[str] = "browser_crawl_jobs"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    start_url: str = Field(min_length=1, max_length=16_384)
    state: Literal[
        "draft", "queued", "running", "paused", "complete", "cancelled", "failed"
    ] = "draft"
    max_depth: int = Field(default=2, ge=0, le=10)
    max_requests: int = Field(default=100, ge=1, le=10_000)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    max_duration_seconds: int = Field(default=300, ge=1, le=3_600)
    max_body_bytes: int = Field(default=1_048_576, ge=0, le=16_777_216)
    requests_completed: int = Field(default=0, ge=0)
    nodes_discovered: int = Field(default=0, ge=0)
    checkpoint: int = Field(default=0, ge=0)
    frontier: list[tuple[str, int]] = Field(default_factory=list, max_length=10_000)
    visited_urls: list[str] = Field(default_factory=list, max_length=10_000)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = Field(default=None, max_length=4_000)

    @field_validator("start_url")
    @classmethod
    def browser_crawl_url_is_network_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("crawl URLs must use HTTP(S)")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("crawl URLs cannot contain credentials")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def browser_crawl_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser crawl timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class BrowserInterceptItem(Entity):
    """Durable receipt for a native request/response paused at a breakpoint."""

    entity_kind: ClassVar[str] = "browser_intercepts"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    tab_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    transaction_id: str = Field(min_length=1, max_length=300)
    phase: Literal["request", "response"]
    method: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url: str = Field(min_length=1, max_length=16_384)
    status_code: int | None = Field(default=None, ge=100, le=999)
    headers: list[tuple[str, str]] = Field(default_factory=list, max_length=200)
    body_artifact_id: str | None = Field(default=None, max_length=200)
    state: Literal["paused", "forwarded", "dropped", "interrupted", "expired"] = (
        "paused"
    )
    decision: Literal["forward", "drop"] | None = None
    decided_by: str | None = Field(default=None, max_length=200)
    decided_at: datetime | None = None
    expires_at: datetime
    edited_method: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    edited_url: str | None = Field(default=None, max_length=16_384)
    edited_headers: list[tuple[str, str]] = Field(default_factory=list, max_length=200)
    edited_body_artifact_id: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=4_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("expires_at", "decided_at")
    @classmethod
    def browser_intercept_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser intercept timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class BrowserRepeaterTab(Entity):
    entity_kind: ClassVar[str] = "browser_repeater_tabs"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    group: str = Field(default="Ungrouped", max_length=200)
    notes: str = Field(default="", max_length=20_000)
    protocol: Literal["http", "websocket"] = "http"
    method: str = Field(default="GET", pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url: str = Field(min_length=1, max_length=16_384)
    headers: list[tuple[str, str]] = Field(default_factory=list, max_length=200)
    body_template: str = Field(default="", max_length=65_536)
    body_artifact_id: str | None = Field(default=None, max_length=200)
    source_exchange_id: str | None = Field(default=None, max_length=200)
    history_exchange_ids: list[str] = Field(default_factory=list, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    state: Literal["draft", "queued", "running", "ready", "cancelled", "failed"] = (
        "draft"
    )
    request_count: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=4_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserRepeaterResult(Entity):
    entity_kind: ClassVar[str] = "browser_repeater_results"
    engagement_id: str
    tab_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=0)
    exchange_id: str | None = Field(default=None, max_length=200)
    status_code: int | None = Field(default=None, ge=100, le=999)
    response_headers: list[tuple[str, str]] = Field(
        default_factory=list, max_length=200
    )
    response_bytes: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    response_body_artifact_id: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=4_000)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def repeater_result_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("repeater result timestamps must include a timezone")
        return value.astimezone(timezone.utc)


class BrowserAttack(Entity):
    """Durable, resumable Intruder-style attack definition and progress."""

    entity_kind: ClassVar[str] = "browser_attacks"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    strategy: Literal["sniper", "battering_ram", "pitchfork", "cluster_bomb"]
    method: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url_template: str = Field(min_length=1, max_length=16_384)
    headers_template: list[tuple[str, str]] = Field(
        default_factory=list, max_length=200
    )
    body_template: str = Field(default="", max_length=65_536)
    positions: list[str] = Field(min_length=1, max_length=32)
    payload_sets: list[dict[str, Any]] = Field(min_length=1, max_length=32)
    transforms: list[str] = Field(default_factory=list, max_length=16)
    state: Literal[
        "draft", "queued", "running", "paused", "complete", "cancelled", "failed"
    ] = "draft"
    max_requests: int = Field(default=100, ge=1, le=100_000)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    requests_per_second: float = Field(default=2.0, gt=0, le=100.0)
    request_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    baseline_exchange_id: str | None = Field(default=None, max_length=200)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = Field(default=None, max_length=4_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at", "completed_at")
    @classmethod
    def browser_attack_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser attack timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None


class BrowserAttackResult(Entity):
    entity_kind: ClassVar[str] = "browser_attack_results"
    engagement_id: str
    attack_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=0)
    payloads: list[str] = Field(default_factory=list, max_length=32)
    exchange_id: str | None = Field(default=None, max_length=200)
    status_code: int | None = Field(default=None, ge=100, le=999)
    response_bytes: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=4_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserTokenAnalysis(Entity):
    entity_kind: ClassVar[str] = "browser_token_analyses"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    sample_count: int = Field(ge=1, le=100_000)
    token_length_min: int = Field(ge=0)
    token_length_max: int = Field(ge=0)
    unique_count: int = Field(ge=0)
    collision_count: int = Field(ge=0)
    shannon_bits_per_character: float = Field(ge=0)
    character_frequencies: dict[str, int] = Field(default_factory=dict)
    source_exchange_ids: list[str] = Field(default_factory=list, max_length=1_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserHandoff(Entity):
    """Expiring paired-device request; it never carries browser secrets."""

    entity_kind: ClassVar[str] = "browser_handoffs"
    engagement_id: str
    session_id: str = Field(min_length=1, max_length=200)
    requested_by_device_id: str = Field(min_length=1, max_length=200)
    command: Literal["navigate", "focus_tab"]
    tab_id: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=16_384)
    status: BrowserHandoffStatus = BrowserHandoffStatus.QUEUED
    expires_at: datetime
    claimed_by_device_id: str | None = Field(default=None, max_length=200)
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = Field(default=None, max_length=4_000)

    @field_validator("url")
    @classmethod
    def browser_handoff_url_is_network_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("browser handoff URLs must use http or https")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("browser handoff URLs cannot contain credentials")
        return value

    @field_validator("expires_at", "claimed_at", "completed_at")
    @classmethod
    def browser_handoff_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("browser handoff timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def browser_handoff_payload_matches_command(self) -> "BrowserHandoff":
        if self.command == "navigate" and self.url is None:
            raise ValueError("navigate handoffs require a URL")
        if self.command == "focus_tab" and self.tab_id is None:
            raise ValueError("focus handoffs require a tab id")
        if self.expires_at <= self.created_at:
            raise ValueError("browser handoff expiry must follow creation")
        return self


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
    max_duration_seconds: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_artifact_queries: int | None = Field(default=None, ge=0)
    max_retries: int = Field(default=2, ge=0, le=100)
    per_target_active_operations: int = Field(default=1, ge=1, le=64)


class AgentRun(Entity):
    entity_kind: ClassVar[str] = "runs"
    engagement_id: str
    objective: str
    status: RunStatus = RunStatus.QUEUED
    backend: RunBackend = RunBackend.NATIVE
    supervisor_provider_id: str | None = None
    supervisor_model: str | None = None
    harness_profile_id: str | None = None
    harness_session_id: str | None = None
    runtime_snapshot: dict[str, Any] = Field(default_factory=dict)
    budget: RunBudget = Field(default_factory=RunBudget)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_event_sequence: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def runtime_binding_is_coherent(self) -> "AgentRun":
        if self.backend == RunBackend.HARNESS and not self.harness_profile_id:
            raise ValueError("harness runs require harness_profile_id")
        if self.backend == RunBackend.NATIVE and any(
            value is not None
            for value in (self.harness_profile_id, self.harness_session_id)
        ):
            raise ValueError("native runs cannot reference a harness")
        return self


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
    origin: ToolCallOrigin = ToolCallOrigin.MISSION
    chat_session_id: str | None = None
    chat_turn_id: str | None = None
    task_id: str | None = None
    tool_name: str
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    vendor_tool_name: str | None = None
    status: ToolCallStatus = ToolCallStatus.PROPOSED
    risk_class: RiskClass
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | list[Any] | str | None = None
    approval_id: str | None = None
    idempotency_key: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    result_artifact_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Approval(Entity):
    entity_kind: ClassVar[str] = "approvals"
    engagement_id: str
    run_id: str
    origin: ToolCallOrigin = ToolCallOrigin.MISSION
    chat_session_id: str | None = None
    chat_turn_id: str | None = None
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


class ProviderCapabilityVerification(NebulaModel):
    """A strict tool-call contract result for one exact provider model."""

    model: str = Field(min_length=1, max_length=500)
    status: ProviderVerificationStatus
    checked_at: datetime = Field(default_factory=utc_now)
    contract_version: str = Field(default="required-tool-v1", min_length=1)
    failure_detail: str | None = Field(default=None, max_length=1_000)

    @field_validator("checked_at")
    @classmethod
    def checked_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("verification timestamp must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def result_is_coherent(self) -> "ProviderCapabilityVerification":
        if (
            self.status == ProviderVerificationStatus.VERIFIED
            and self.failure_detail is not None
        ):
            raise ValueError("verified capability records cannot contain a failure")
        if self.status == ProviderVerificationStatus.FAILED and not self.failure_detail:
            raise ValueError("failed verification requires a failure detail")
        return self


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
    capability_verifications: dict[str, ProviderCapabilityVerification] = Field(
        default_factory=dict
    )
    privacy: ProviderPrivacy = Field(default_factory=ProviderPrivacy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("secret_ref")
    @classmethod
    def secret_must_be_an_environment_reference(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"(?:env:[A-Za-z_][A-Za-z0-9_]*|(?:vault|session):[0-9a-f]{32})",
            value,
        ):
            raise ValueError("secret_ref must use env:NAME, vault:ID, or session:ID")
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
        if any(
            key != verification.model
            for key, verification in self.capability_verifications.items()
        ):
            raise ValueError("capability verification keys must match exact model IDs")
        has_verified_model = any(
            verification.status == ProviderVerificationStatus.VERIFIED
            and verification.contract_version == "required-tool-v1"
            for verification in self.capability_verifications.values()
        )
        self.capabilities.tool_calling = has_verified_model
        self.capabilities.parallel_tool_calls = False
        return self

    def tools_verified_for(self, model: str) -> bool:
        verification = self.capability_verifications.get(model)
        return bool(
            verification
            and verification.status == ProviderVerificationStatus.VERIFIED
            and verification.contract_version == "required-tool-v1"
        )


class HarnessRuntimeOption(NebulaModel):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1_000)


class HarnessModelOptions(NebulaModel):
    model: str = Field(min_length=1, max_length=500)
    reasoning_efforts: list[HarnessRuntimeOption] = Field(
        default_factory=list, max_length=32
    )
    default_reasoning_effort: str | None = Field(default=None, max_length=100)
    service_tiers: list[HarnessRuntimeOption] = Field(
        default_factory=list, max_length=32
    )
    default_service_tier: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def defaults_are_advertised(self) -> "HarnessModelOptions":
        efforts = {item.id for item in self.reasoning_efforts}
        tiers = {item.id for item in self.service_tiers}
        if (
            self.default_reasoning_effort
            and self.default_reasoning_effort not in efforts
        ):
            raise ValueError("default reasoning effort must be advertised")
        if self.default_service_tier and self.default_service_tier not in tiers:
            raise ValueError("default service tier must be advertised")
        return self


class HarnessCapabilities(NebulaModel):
    sessions: bool = True
    resume: bool = True
    steering: bool = False
    interruption: bool = True
    approvals: bool = True
    streaming: bool = True
    mcp: bool = True
    activity_replay: bool = False
    reasoning_summaries: bool = False
    plans: bool = False
    planning_mode: bool = False
    goal_monitoring: bool = False
    skill_invocation: bool = False
    modes: list[str] = Field(default_factory=list, max_length=64)
    live_command_output: bool = False
    file_diffs: bool = False
    detailed_usage: bool = False
    interactions: bool = False
    hooks: bool = False
    subagent_activity: bool = False
    subagent_control: bool = False
    checkpoint_rewind: bool = False
    enforceable_cost_limit: bool = False
    enforceable_token_limit: bool = False
    supported_native_capabilities: list[str] = Field(
        default_factory=list, max_length=64
    )
    models: list[str] = Field(default_factory=list, max_length=256)
    model_options: list[HarnessModelOptions] = Field(
        default_factory=list, max_length=256
    )
    harness_version: str | None = Field(default=None, max_length=200)
    adapter_version: str | None = Field(default=None, max_length=200)
    protocol_version: str | None = Field(default=None, max_length=200)
    checked_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=1_000)


class HarnessNativeCapabilities(NebulaModel):
    """Explicit vendor-native capabilities available beside Nebula's gateway."""

    workspace_access: HarnessWorkspaceAccess = HarnessWorkspaceAccess.NONE
    shell: bool = False
    web_search: bool = False
    web_fetch: bool = False
    browser: bool = False
    computer_use: bool = False
    image_generation: bool = False
    skills: bool = False
    subagents: bool = False


class HarnessProfile(Entity):
    entity_kind: ClassVar[str] = "harnesses"
    name: str = Field(min_length=1, max_length=200)
    kind: HarnessKind
    connection_mode: HarnessConnectionMode = HarnessConnectionMode.SPAWN
    transport: HarnessTransport = HarnessTransport.STDIO
    executable: str | None = Field(default=None, max_length=4096)
    endpoint: str | None = Field(default=None, max_length=4096)
    auth_mode: HarnessAuthMode = HarnessAuthMode.EXISTING_SESSION
    secret_ref: str | None = None
    default_model: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    privacy: ProviderPrivacy = Field(default_factory=ProviderPrivacy)
    capabilities: HarnessCapabilities = Field(default_factory=HarnessCapabilities)
    native_capabilities: HarnessNativeCapabilities = Field(
        default_factory=HarnessNativeCapabilities
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("executable")
    @classmethod
    def executable_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("/"):
            raise ValueError("harness executable must be an absolute path")
        return value

    @field_validator("secret_ref")
    @classmethod
    def secret_is_opaque(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"(?:env:[A-Za-z_][A-Za-z0-9_]*|(?:vault|session):[0-9a-f]{32})",
            value,
        ):
            raise ValueError("secret_ref must use env:NAME, vault:ID, or session:ID")
        return value

    @model_validator(mode="after")
    def connection_is_supported(self) -> "HarnessProfile":
        native = self.native_capabilities
        if self.kind == HarnessKind.GROK_ACP:
            if self.connection_mode != HarnessConnectionMode.SPAWN:
                raise ValueError("Grok ACP currently supports managed stdio only")
            if not self.executable:
                raise ValueError("spawned Grok ACP harnesses require an executable")
            if self.auth_mode != HarnessAuthMode.EXISTING_SESSION:
                raise ValueError("Grok ACP uses the existing cached Grok login")
        if self.kind == HarnessKind.CLAUDE_AGENT_SDK and (
            native.browser or native.computer_use or native.image_generation
        ):
            raise ValueError(
                "Claude Agent SDK profiles do not support browser, computer-use, "
                "or image-generation capabilities"
            )
        if (
            self.kind == HarnessKind.CLAUDE_AGENT_SDK
            and native.shell
            and self.auth_mode == HarnessAuthMode.SECRET_REF
        ):
            raise ValueError(
                "Claude native shell requires existing-session authentication so "
                "an API key is not present in the vendor process environment"
            )
        if self.kind == HarnessKind.CODEX_APP_SERVER and native.web_fetch:
            raise ValueError(
                "Codex uses web_search for both discovery and page retrieval; "
                "web_fetch is Claude-only"
            )
        if self.auth_mode != HarnessAuthMode.EXISTING_SESSION and not self.secret_ref:
            raise ValueError("selected harness authentication requires secret_ref")
        if self.auth_mode == HarnessAuthMode.EXISTING_SESSION and self.secret_ref:
            raise ValueError(
                "existing-session authentication cannot store a secret_ref"
            )
        if self.connection_mode == HarnessConnectionMode.SPAWN:
            if self.endpoint is not None:
                raise ValueError("spawned harnesses cannot define endpoint")
            if self.transport != HarnessTransport.STDIO:
                raise ValueError("spawned harnesses must use stdio")
            if (
                self.kind in {HarnessKind.CODEX_APP_SERVER, HarnessKind.GROK_ACP}
                and not self.executable
            ):
                raise ValueError("spawned command harnesses require executable")
            if self.auth_mode == HarnessAuthMode.ENDPOINT_BEARER:
                raise ValueError("spawned harnesses cannot use endpoint bearer auth")
        else:
            if self.kind != HarnessKind.CODEX_APP_SERVER:
                raise ValueError("endpoint mode is currently supported only for Codex")
            if self.executable is not None or not self.endpoint:
                raise ValueError(
                    "endpoint harnesses require endpoint and no executable"
                )
            if self.transport not in {
                HarnessTransport.UNIX,
                HarnessTransport.WEBSOCKET,
            }:
                raise ValueError("Codex endpoints must use unix or websocket")
            if self.transport == HarnessTransport.UNIX:
                if not self.endpoint.startswith("unix://"):
                    raise ValueError("unix harness endpoints must use unix://")
                parsed = urlsplit(self.endpoint)
                if not parsed.path.startswith("/") or parsed.query or parsed.fragment:
                    raise ValueError(
                        "unix harness endpoints require an absolute socket path"
                    )
            else:
                parsed = urlsplit(self.endpoint)
                if parsed.scheme != "ws" or parsed.hostname not in {
                    "127.0.0.1",
                    "::1",
                    "localhost",
                }:
                    raise ValueError(
                        "websocket harness endpoints must be loopback ws://"
                    )
                if (
                    parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError(
                        "websocket harness endpoints cannot embed credentials or query data"
                    )
            if self.auth_mode == HarnessAuthMode.SECRET_REF:
                raise ValueError(
                    "endpoint harnesses use endpoint_bearer authentication"
                )
        return self


class McpToolSnapshot(NebulaModel):
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10_000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = True
    credentialed: bool | None = None
    annotations_complete: bool = False


class McpCapabilitySnapshot(NebulaModel):
    protocol_version: str | None = Field(default=None, max_length=100)
    tools: list[McpToolSnapshot] = Field(default_factory=list, max_length=2_000)
    resources: bool = False
    prompts: bool = False
    instructions: str | None = Field(default=None, max_length=10_000)
    checked_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=1_000)


class McpServerProfile(Entity):
    entity_kind: ClassVar[str] = "mcp_servers"
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+$")
    transport: McpTransport
    command: str | None = Field(default=None, max_length=4096)
    arguments: list[str] = Field(default_factory=list, max_length=128)
    url: str | None = Field(default=None, max_length=4096)
    auth_mode: McpAuthMode = McpAuthMode.NONE
    bearer_secret_ref: str | None = None
    header_secret_refs: dict[str, str] = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    environment_secret_refs: dict[str, str] = Field(default_factory=dict)
    cwd_policy: McpCwdPolicy = McpCwdPolicy.WORKSPACE
    cwd: str | None = Field(default=None, max_length=4096)
    enabled: bool = False
    required: bool = False
    trusted_stdio: bool = False
    startup_timeout_seconds: float = Field(default=10, gt=0, le=120)
    tool_timeout_seconds: float = Field(default=60, gt=0, le=900)
    enabled_tools: list[str] = Field(default_factory=list, max_length=2_000)
    disabled_tools: list[str] = Field(default_factory=list, max_length=2_000)
    default_approval: McpApprovalMode = McpApprovalMode.RISK_BASED
    tool_overrides: dict[str, McpApprovalMode] = Field(default_factory=dict)
    capabilities: McpCapabilitySnapshot = Field(default_factory=McpCapabilitySnapshot)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("command", "cwd")
    @classmethod
    def local_paths_are_absolute(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("/"):
            raise ValueError("MCP local paths must be absolute")
        return value

    @field_validator(
        "bearer_secret_ref",
        "header_secret_refs",
        "environment_secret_refs",
    )
    @classmethod
    def secrets_are_references(cls, value: Any) -> Any:
        values = value.values() if isinstance(value, dict) else [value]
        for item in values:
            if item is not None and not re.fullmatch(
                r"(?:env:[A-Za-z_][A-Za-z0-9_]*|(?:vault|session):[0-9a-f]{32})",
                item,
            ):
                raise ValueError(
                    "MCP secrets must use env:, vault:, or session: references"
                )
        return value

    @field_validator("arguments")
    @classmethod
    def literal_arguments_are_bounded(cls, value: list[str]) -> list[str]:
        if any(len(item) > 8_192 for item in value):
            raise ValueError("MCP arguments must be at most 8192 characters each")
        return value

    @field_validator("header_secret_refs")
    @classmethod
    def header_names_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) for name in value
        ):
            raise ValueError("MCP secret header names must be valid HTTP field names")
        return value

    @field_validator("environment_secret_refs")
    @classmethod
    def secret_environment_names_are_valid(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in value):
            raise ValueError(
                "MCP secret environment names must be portable identifiers"
            )
        return value

    @field_validator("environment")
    @classmethod
    def literal_environment_is_nonsecret(cls, value: dict[str, str]) -> dict[str, str]:
        for name, item in value.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("MCP environment names must be portable identifiers")
            if any(
                token in name.lower()
                for token in ("secret", "token", "password", "credential", "api_key")
            ):
                raise ValueError(
                    "credential-like MCP environment values require environment_secret_refs"
                )
            if len(item) > 8_192:
                raise ValueError(
                    "MCP environment values must be at most 8192 characters"
                )
        return value

    @model_validator(mode="after")
    def transport_is_coherent(self) -> "McpServerProfile":
        if self.transport == McpTransport.STDIO:
            if not self.command or self.url is not None:
                raise ValueError("stdio MCP servers require command and no URL")
            if self.auth_mode != McpAuthMode.NONE:
                raise ValueError("stdio MCP authentication uses environment references")
            if self.enabled and not self.trusted_stdio:
                raise ValueError("enabled stdio MCP servers require trusted_stdio")
            if self.cwd_policy == McpCwdPolicy.FIXED and not self.cwd:
                raise ValueError("fixed MCP cwd policy requires cwd")
            if self.cwd_policy == McpCwdPolicy.WORKSPACE and self.cwd is not None:
                raise ValueError("workspace MCP cwd policy cannot define cwd")
        else:
            if self.command is not None or self.arguments or not self.url:
                raise ValueError(
                    "HTTP MCP servers require URL and no command arguments"
                )
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("MCP URL must use http or https")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "MCP URLs cannot embed credentials, query data, or fragments"
                )
            if parsed.scheme == "http" and parsed.hostname not in {
                "127.0.0.1",
                "::1",
                "localhost",
            }:
                raise ValueError("non-loopback MCP HTTP servers require HTTPS")
            if self.cwd is not None or self.environment or self.environment_secret_refs:
                raise ValueError("HTTP MCP servers cannot define process settings")
        if self.auth_mode == McpAuthMode.BEARER and not self.bearer_secret_ref:
            raise ValueError("bearer MCP authentication requires bearer_secret_ref")
        if self.auth_mode == McpAuthMode.HEADERS and not self.header_secret_refs:
            raise ValueError("header MCP authentication requires header_secret_refs")
        if set(self.enabled_tools).intersection(self.disabled_tools):
            raise ValueError("an MCP tool cannot be both enabled and disabled")
        return self


class HarnessSession(Entity):
    entity_kind: ClassVar[str] = "harness_sessions"
    engagement_id: str
    harness_profile_id: str
    external_session_id: str | None = Field(default=None, max_length=500)
    model: str = Field(min_length=1, max_length=500)
    status: HarnessSessionStatus = HarnessSessionStatus.STARTING
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=64)
    mcp_snapshot: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    adapter_version: str | None = Field(default=None, max_length=200)
    last_turn_id: str | None = Field(default=None, max_length=200)
    last_activity_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class LibraryItem(Entity):
    """A reusable knowledge item owned by the local Nebula workspace."""

    entity_kind: ClassVar[str] = "library_items"
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


class ScopeImportCandidate(NebulaModel):
    id: str = Field(min_length=1, max_length=64)
    target_type: ScopeImportTargetType
    classification: ScopeImportClassification
    raw_value: str = Field(min_length=1, max_length=2048)
    normalized_value: str | None = Field(default=None, max_length=2048)
    source_location: str = Field(default="document", max_length=500)
    source_excerpt: str = Field(default="", max_length=1000)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class ScopeImportProvenance(NebulaModel):
    backend_kind: Literal["provider", "harness"] = "provider"
    provider_profile_id: str = Field(min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    prompt_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime = Field(default_factory=utc_now)
    provider_request_ids: list[str] = Field(default_factory=list, max_length=20)


class ScopeImport(Entity):
    entity_kind: ClassVar[str] = "scope_imports"
    engagement_id: str
    artifact_id: str
    filename: str = Field(min_length=1, max_length=255)
    source_type: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_scope_revision: int = Field(default=0, ge=0)
    status: ScopeImportStatus = ScopeImportStatus.GENERATING
    candidates: list[ScopeImportCandidate] = Field(
        default_factory=list, max_length=2000
    )
    warnings: list[str] = Field(default_factory=list, max_length=2000)
    provenance: ScopeImportProvenance | None = None
    usage: ChatTokenUsage = Field(default_factory=ChatTokenUsage)
    error_detail: str | None = Field(default=None, max_length=4000)
    applied_candidate_ids: list[str] = Field(default_factory=list, max_length=2000)
    applied_scope_policy_id: str | None = Field(default=None, max_length=200)
    applied_scope_revision: int | None = Field(default=None, ge=1)
    applied_at: datetime | None = None
    applied_by: str | None = Field(default=None, max_length=200)
    discarded_at: datetime | None = None
    discarded_by: str | None = Field(default=None, max_length=200)


class HarnessDetailedUsage(NebulaModel):
    """Provider-neutral usage and timing exposed by local harnesses."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    duration_api_ms: int | None = Field(default=None, ge=0)
    num_turns: int | None = Field(default=None, ge=0)
    context_window: int | None = Field(default=None, ge=0)
    context_used: int | None = Field(default=None, ge=0)
    model_usage: dict[str, Any] = Field(default_factory=dict)
    rate_limit: dict[str, Any] = Field(default_factory=dict)

    def basic(self) -> ChatTokenUsage:
        return ChatTokenUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
        )


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


class HarnessTurn(Entity):
    entity_kind: ClassVar[str] = "harness_turns"
    engagement_id: str
    harness_session_id: str
    origin: HarnessTurnOrigin
    chat_session_id: str | None = None
    chat_turn_id: str | None = None
    run_id: str | None = None
    external_turn_id: str | None = Field(default=None, max_length=500)
    status: HarnessTurnStatus = HarnessTurnStatus.QUEUED
    prompt: str = Field(min_length=1, max_length=250_000)
    response: str | None = Field(default=None, max_length=500_000)
    usage: ChatTokenUsage = Field(default_factory=ChatTokenUsage)
    tool_call_ids: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = Field(default=None, max_length=1_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def origin_binding_is_coherent(self) -> "HarnessTurn":
        if self.origin == HarnessTurnOrigin.CHAT:
            if not self.chat_session_id or not self.chat_turn_id or self.run_id:
                raise ValueError("chat harness turns require chat bindings only")
        elif self.origin == HarnessTurnOrigin.MISSION and (
            not self.run_id or self.chat_turn_id
        ):
            raise ValueError("mission harness turns require run_id and no chat_turn_id")
        elif self.origin == HarnessTurnOrigin.ANALYSIS and (
            self.chat_session_id or self.chat_turn_id or self.run_id
        ):
            raise ValueError(
                "analysis harness turns cannot bind chat or mission owners"
            )
        return self


class HarnessInteraction(Entity):
    """A durable question or structured elicitation blocking a harness turn."""

    entity_kind: ClassVar[str] = "harness_interactions"
    engagement_id: str
    harness_turn_id: str
    harness_session_id: str
    origin: HarnessTurnOrigin
    kind: HarnessInteractionKind
    status: HarnessInteractionStatus = HarnessInteractionStatus.PENDING
    vendor_request_id: str = Field(min_length=1, max_length=500)
    item_id: str | None = Field(default=None, max_length=500)
    chat_session_id: str | None = Field(default=None, max_length=200)
    run_id: str | None = Field(default=None, max_length=200)
    prompt: str = Field(default="Input required", max_length=4_000)
    questions: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    response_schema: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] | None = None
    contains_secret: bool = False
    auto_resolution_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def owner_and_resolution_are_coherent(self) -> "HarnessInteraction":
        if self.origin == HarnessTurnOrigin.CHAT:
            if not self.chat_session_id or self.run_id is not None:
                raise ValueError("chat interactions require only chat_session_id")
        elif not self.run_id or self.chat_session_id is not None:
            raise ValueError("mission interactions require only run_id")
        terminal = self.status != HarnessInteractionStatus.PENDING
        if terminal != (self.resolved_at is not None):
            raise ValueError(
                "resolved_at is required exactly when an interaction is terminal"
            )
        if self.contains_secret and self.response is not None:
            raise ValueError("secret interaction answers cannot be persisted")
        return self


class ChatSession(Entity):
    """A durable engagement-scoped analyst conversation."""

    entity_kind: ClassVar[str] = "chat_sessions"
    engagement_id: str
    title: str = Field(min_length=1, max_length=300)
    backend: ChatBackend = ChatBackend.PROVIDER
    provider_profile_id: str | None = None
    harness_profile_id: str | None = None
    harness_session_id: str | None = None
    parent_session_id: str | None = Field(default=None, max_length=200)
    forked_from_message_id: str | None = Field(default=None, max_length=200)
    model: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def backend_binding_is_coherent(self) -> "ChatSession":
        if self.backend == ChatBackend.PROVIDER:
            if not self.provider_profile_id or any(
                value is not None
                for value in (self.harness_profile_id, self.harness_session_id)
            ):
                raise ValueError(
                    "provider chat sessions require only provider_profile_id"
                )
        elif (
            not self.harness_profile_id
            or not self.harness_session_id
            or self.provider_profile_id is not None
        ):
            raise ValueError(
                "harness chat sessions require harness profile and session"
            )
        return self


class ChatContentBlock(NebulaModel):
    """One ordered, durable block in a multimodal chat message."""

    type: Literal["text", "code", "image", "artifact", "citation", "activity"]
    text: str | None = Field(default=None, max_length=200_000)
    language: str | None = Field(default=None, max_length=100)
    artifact_id: str | None = Field(default=None, max_length=200)
    media_type: str | None = Field(default=None, max_length=200)
    alt: str | None = Field(default=None, max_length=1_000)
    activity_id: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def required_reference_is_present(self) -> "ChatContentBlock":
        if self.type in {"text", "code"} and self.text is None:
            raise ValueError(f"{self.type} blocks require text")
        if self.type in {"image", "artifact"} and not self.artifact_id:
            raise ValueError(f"{self.type} blocks require artifact_id")
        if self.type == "activity" and not self.activity_id:
            raise ValueError("activity blocks require activity_id")
        return self


class ChatTurnStatus(StringEnum):
    ROUTING = "routing"
    WAITING_APPROVAL = "waiting_approval"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ChatTurn(Entity):
    """A durable, idempotently resumable provider response."""

    entity_kind: ClassVar[str] = "chat_turns"
    engagement_id: str
    session_id: str
    backend: ChatBackend = ChatBackend.PROVIDER
    provider_profile_id: str | None = None
    harness_turn_id: str | None = None
    model: str
    status: ChatTurnStatus = ChatTurnStatus.ROUTING
    tools_enabled: bool = False
    max_tool_calls: int = Field(default=5, ge=0, le=5)
    max_artifact_queries: int = Field(default=20, ge=0, le=200)
    next_step: int = Field(default=0, ge=0, le=205)
    execution_tool_calls: int = Field(default=0, ge=0, le=5)
    artifact_queries: int = Field(default=0, ge=0, le=200)
    tool_call_ids: list[str] = Field(default_factory=list)
    tool_history: list[dict[str, Any]] = Field(default_factory=list)
    approval_id: str | None = None
    scope_policy_id: str | None = None
    scope_revision: int | None = Field(default=None, ge=1)
    request_snapshot: dict[str, Any] = Field(default_factory=dict)
    usage: ChatTokenUsage = Field(default_factory=ChatTokenUsage)
    final_message_id: str | None = None
    error: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def backend_binding_is_coherent(self) -> "ChatTurn":
        if self.backend == ChatBackend.PROVIDER and not self.provider_profile_id:
            raise ValueError("provider chat turns require provider_profile_id")
        if self.backend == ChatBackend.HARNESS and self.provider_profile_id is not None:
            raise ValueError("harness chat turns cannot reference a provider")
        return self


class ChatMessage(Entity):
    """One immutable message in a durable analyst conversation."""

    entity_kind: ClassVar[str] = "chat_messages"
    engagement_id: str
    session_id: str
    sequence: int = Field(ge=1)
    role: ChatRole
    content: str = Field(min_length=1, max_length=200_000)
    content_blocks: list[ChatContentBlock] = Field(default_factory=list, max_length=64)
    source_message_id: str | None = Field(default=None, max_length=200)
    provider_profile_id: str | None = None
    model: str | None = None
    usage: ChatTokenUsage | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None
    citations: list[ChatCitation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PairedDeviceSession(Entity):
    """Revocable browser-device session; only token hashes are durable."""

    entity_kind: ClassVar[str] = "paired_device_sessions"
    name: str = Field(min_length=1, max_length=200)
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    csrf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime = Field(default_factory=utc_now)
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None = None
    platform: str | None = Field(default=None, max_length=100)
    app_version: str | None = Field(default=None, max_length=100)
    capabilities: list[str] = Field(default_factory=list, max_length=128)
    ownership_claims: list[ResourceRef] = Field(default_factory=list, max_length=100)
    heartbeat_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def expiry_order_is_valid(self) -> "PairedDeviceSession":
        if self.idle_expires_at > self.absolute_expires_at:
            raise ValueError("idle expiry cannot exceed absolute expiry")
        return self


class DeviceCapabilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1, max_length=100)
    app_version: str = Field(min_length=1, max_length=100)
    capabilities: list[str] = Field(default_factory=list, max_length=128)
    ownership_claims: list[ResourceRef] = Field(default_factory=list, max_length=100)
    heartbeat_at: datetime = Field(default_factory=utc_now)
    expected_revision: int | None = Field(default=None, ge=1)


class ActionIntentStatus(StringEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    PREPARED = "prepared"
    COMMITTED = "committed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    RECONCILE_REQUIRED = "reconcile_required"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ActionIntent(Entity):
    """Durable device action with explicit prepare, commit, and apply states."""

    entity_kind: ClassVar[str] = "action_intents"
    engagement_id: str
    resources: list[ResourceRef] = Field(min_length=1, max_length=100)
    action_id: str = Field(min_length=1, max_length=80)
    requester: str = Field(min_length=1, max_length=200)
    eligible_device_ids: list[str] = Field(default_factory=list, max_length=100)
    selected_device_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=300)
    expected_revisions: dict[str, int] = Field(default_factory=dict)
    logical_lease_key: str = Field(min_length=1, max_length=500)
    lease_expires_at: datetime | None = None
    status: ActionIntentStatus = ActionIntentStatus.QUEUED
    expires_at: datetime
    prepared_at: datetime | None = None
    committed_at: datetime | None = None
    receipt: dict[str, Any] | None = None
    result_refs: list[ResourceRef] = Field(default_factory=list, max_length=100)
    error: str | None = Field(default=None, max_length=2_000)
    core_mutation_committed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def intent_lifecycle_is_coherent(self) -> "ActionIntent":
        if self.expires_at <= self.created_at:
            raise ValueError("action intent expiry must follow creation")
        if (
            self.selected_device_id
            and self.selected_device_id not in self.eligible_device_ids
        ):
            raise ValueError("selected device must be eligible")
        if (
            self.status
            in {
                ActionIntentStatus.CLAIMED,
                ActionIntentStatus.PREPARED,
                ActionIntentStatus.COMMITTED,
            }
            and not self.selected_device_id
        ):
            raise ValueError("active action intents require a selected device")
        if (
            self.status in {ActionIntentStatus.PREPARED, ActionIntentStatus.COMMITTED}
            and not self.prepared_at
        ):
            raise ValueError("prepared action intents require prepared_at")
        if self.status == ActionIntentStatus.COMMITTED and not self.committed_at:
            raise ValueError("committed action intents require committed_at")
        return self


class HandoffStatus(StringEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class HandoffEnvelope(Entity):
    """Reference-only handoff; selected or unsaved bytes never enter this entity."""

    entity_kind: ClassVar[str] = "handoff_envelopes"
    engagement_id: str
    source_refs: list[ResourceRef] = Field(default_factory=list, max_length=20)
    action_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    target_ref: ResourceRef | None = None
    origin_device_id: str = Field(min_length=1, max_length=200)
    source_hashes: dict[str, str] = Field(default_factory=dict, max_length=20)
    source_labels: dict[str, str] = Field(default_factory=dict, max_length=20)
    transient: bool = False
    status: HandoffStatus = HandoffStatus.PENDING
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_by_device_id: str | None = Field(default=None, max_length=200)
    consume_idempotency_key: str | None = Field(default=None, max_length=300)

    @field_validator("source_hashes")
    @classmethod
    def handoff_hashes_are_sha256(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values.values()):
            raise ValueError("handoff source hashes must be lowercase SHA-256 values")
        return values

    @field_validator("source_labels")
    @classmethod
    def handoff_labels_are_bounded(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not value.strip() or len(value) > 300 for value in values.values()):
            raise ValueError("handoff source labels must contain 1 to 300 characters")
        return values

    @model_validator(mode="after")
    def handoff_is_bounded_and_coherent(self) -> "HandoffEnvelope":
        if self.expires_at <= self.created_at:
            raise ValueError("handoff expiry must follow creation")
        if any(ref.project_id != self.engagement_id for ref in self.source_refs):
            raise ValueError("handoff sources must belong to its project")
        if self.target_ref and self.target_ref.project_id != self.engagement_id:
            raise ValueError("handoff target must belong to its project")
        allowed_keys = {f"{ref.kind.value}:{ref.id}" for ref in self.source_refs}
        if not set(self.source_hashes).issubset(allowed_keys):
            raise ValueError("handoff hashes must identify a source reference")
        if not set(self.source_labels).issubset(allowed_keys):
            raise ValueError("handoff labels must identify a source reference")
        if self.status == HandoffStatus.CONSUMED and not self.consumed_at:
            raise ValueError("consumed handoffs require a consumption timestamp")
        return self


class ExecutionOrigin(NebulaModel):
    kind: ExecutionOriginKind
    message_id: str | None = Field(default=None, max_length=200)
    block_ordinal: int | None = Field(default=None, ge=0, le=10_000)
    block_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selection_start_byte: int | None = Field(default=None, ge=0, le=1_000_000)
    selection_end_byte: int | None = Field(default=None, ge=0, le=1_000_000)
    execution_id: str | None = Field(default=None, max_length=200)
    source_kind: str | None = Field(default=None, pattern=r"^[a-z0-9._-]{1,100}$")
    source_id: str | None = Field(default=None, max_length=200)
    source_label: str | None = Field(default=None, min_length=1, max_length=500)
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

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
            if any(
                value is not None
                for value in (
                    self.source_kind,
                    self.source_id,
                    self.source_label,
                    self.source_sha256,
                )
            ):
                raise ValueError(
                    "assistant-message origins cannot contain selection-source metadata"
                )
        elif self.kind == ExecutionOriginKind.RERUN:
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
                    self.source_kind,
                    self.source_id,
                    self.source_label,
                    self.source_sha256,
                )
            ):
                raise ValueError("rerun origins cannot contain other origin metadata")
        else:
            if not self.source_kind or not self.source_label or not self.source_sha256:
                raise ValueError(
                    "selection origins require source kind, label, and SHA-256"
                )
            if any(
                value is not None
                for value in (
                    self.message_id,
                    self.block_ordinal,
                    self.block_sha256,
                    self.selection_start_byte,
                    self.selection_end_byte,
                    self.execution_id,
                )
            ):
                raise ValueError(
                    "selection origins cannot contain message or execution coordinates"
                )
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
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image: str = Field(min_length=1, max_length=1000)
    runner_profile_id: str = Field(min_length=1, max_length=200)
    runner_profile_revision: int = Field(ge=1)
    runner_runtime: RunnerRuntime
    runner_isolation: RunnerIsolation
    runner_executable: str = Field(min_length=1, max_length=2048)
    runner_platform: str = Field(pattern=r"^linux/(amd64|arm64)$")
    runner_context: str | None = Field(default=None, max_length=500)
    runner_socket: str | None = Field(default=None, max_length=2048)


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


class SuggestedCommand(NebulaModel):
    title: str = Field(min_length=1, max_length=500)
    rationale: str = Field(default="", max_length=20_000)
    command: str = Field(min_length=1, max_length=50_000)
    language: str = Field(default="bash", pattern=r"^(bash|sh|python)$")
    network_target: str | None = Field(default=None, max_length=500)
    network_ports: list[int] = Field(default_factory=list, max_length=100)


class GeneratedDraftContent(NebulaModel):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=50_000)
    observations: list[str] = Field(default_factory=list, max_length=100)
    potential_findings: list[PotentialFindingDraft] = Field(
        default_factory=list, max_length=100
    )
    evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    next_step: SuggestedCommand | None = None


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


class AIWritingProvenance(NebulaModel):
    """Review metadata for AI-assisted prose that an operator chose to keep."""

    backend_kind: Literal["provider", "harness"] = "provider"
    provider_profile_id: str = Field(min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    prompt_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction: str = Field(min_length=1, max_length=4000)
    generated_at: datetime = Field(default_factory=utc_now)
    provider_request_id: str | None = Field(default=None, max_length=500)

    @field_validator("generated_at")
    @classmethod
    def writing_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("AI writing timestamps must include a timezone")
        return value.astimezone(timezone.utc)


class ReportNoteTransform(NebulaModel):
    """An editable, report-local rendering of a linked project note."""

    observation_id: str = Field(min_length=1, max_length=200)
    source_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(max_length=100_000)
    provenance: AIWritingProvenance


class Report(Entity):
    entity_kind: ClassVar[str] = "reports"
    engagement_id: str
    title: str = Field(min_length=1, max_length=500)
    status: ReportStatus = ReportStatus.DRAFT
    executive_summary: str = ""
    finding_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    note_transforms: list[ReportNoteTransform] = Field(
        default_factory=list, max_length=500
    )
    artifact_ids: list[str] = Field(default_factory=list)
    executive_summary_provenance: AIWritingProvenance | None = None
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
        transformed_ids = [item.observation_id for item in self.note_transforms]
        if len(transformed_ids) != len(set(transformed_ids)):
            raise ValueError(
                "report note transforms must reference unique observations"
            )
        if any(item not in self.observation_ids for item in transformed_ids):
            raise ValueError("report note transforms require a selected observation")
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
    AutomationProjectPolicy,
    AutomationSession,
    CommandExecution,
    RunnerProfile,
    Asset,
    Service,
    Identity,
    BrowserIdentity,
    BrowserSession,
    BrowserAssessment,
    BrowserAssessmentStep,
    BrowserLoginFlow,
    BrowserRecipe,
    BrowserIssueCandidate,
    BrowserValidationGrant,
    BrowserTrafficExchange,
    BrowserWebSocketFrame,
    BrowserAction,
    BrowserAutomationLease,
    BrowserCommand,
    BrowserProxyRule,
    BrowserCrawlJob,
    BrowserSiteNode,
    BrowserSiteEdge,
    BrowserInterceptItem,
    BrowserRepeaterTab,
    BrowserRepeaterResult,
    BrowserAttack,
    BrowserAttackResult,
    BrowserTokenAnalysis,
    BrowserHandoff,
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
    HarnessProfile,
    McpServerProfile,
    HarnessSession,
    SourceSnapshot,
    KnowledgeSource,
    LibraryItem,
    ScopeImport,
    ChatSession,
    ChatTurn,
    ChatMessage,
    PairedDeviceSession,
    ActionIntent,
    HandoffEnvelope,
    HarnessTurn,
    HarnessInteraction,
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
