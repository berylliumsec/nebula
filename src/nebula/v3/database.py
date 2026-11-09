"""SQLAlchemy 2 database bootstrap for the Nebula 3 headless core."""

from __future__ import annotations


import asyncio
import fcntl
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, cast

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    event,
    func,
    inspect,
    insert,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from .database_pool import ConnectionBudget, PoolSettings, connection_budget
from .async_utils import finish_cleanup
from .query_predicates import visible_harness_chat_title

from .domain import utc_now
from .performance import PerformanceTelemetry, install_sql_timing

CURRENT_SCHEMA_VERSION = 5
SCRATCH_PROJECT_BOOTSTRAP_KEY = "scratch_project_v1"


class Base(DeclarativeBase):
    pass


class SchemaVersionRow(Base):
    __tablename__ = "schema_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class BootstrapStateRow(Base):
    """Durable one-shot application bootstrap decisions.

    A fresh database is marked eligible before any user-facing service runs. An
    existing database is marked complete instead, so upgrading an empty or
    imported database can never unexpectedly add a project.
    """

    __tablename__ = "bootstrap_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    engagement_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProjectCreateRequestRow(Base):
    """An immutable project-create receipt retained after project deletion."""

    __tablename__ = "project_create_requests"

    actor_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    engagement_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class RuntimeInstanceRow(Base):
    """One process incarnation; IDs must never be reused after lease expiry."""

    __tablename__ = "runtime_instances"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    heartbeat_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ProjectOwnershipRow(Base):
    """The durable fencing counter survives every owner, including no owner."""

    __tablename__ = "project_ownership"
    __table_args__ = (Index("ix_project_ownership_runtime", "runtime_id"),)

    project_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    runtime_id: Mapped[str | None] = mapped_column(
        ForeignKey("runtime_instances.id"), nullable=True
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RuntimeCapacityPolicyRow(Base):
    __tablename__ = "runtime_capacity_policy"

    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    limits: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)


class RuntimeCapacityReservationRow(Base):
    __tablename__ = "runtime_capacity_reservations"
    __table_args__ = (
        UniqueConstraint(
            "runtime_id",
            "project_id",
            "fencing_token",
            "request_key",
            name="uq_runtime_capacity_request",
        ),
        Index("ix_runtime_capacity_state", "state", "runtime_id"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    runtime_id: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    demands: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    released_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    release_reason: Mapped[str | None] = mapped_column(String(100))


class MemoryBudgetPolicyRow(Base):
    __tablename__ = "memory_budget_policies"

    scope_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MemoryJobRow(Base):
    __tablename__ = "memory_jobs"
    __table_args__ = (Index("ix_memory_jobs_state", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(200))
    resource_kind: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    launch_owner: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    launch_groups: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pending_limits: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    application_state: Mapped[str] = mapped_column(String(30), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MemoryBudgetAuditRow(Base):
    __tablename__ = "memory_budget_audit"
    __table_args__ = (Index("ix_memory_budget_audit_scope", "scope_key", "created_at"),)

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    previous: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    proposed: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RuntimeCommandRow(Base):
    """An immutable submission identity and its separately evolving outcome."""

    __tablename__ = "runtime_commands"
    __table_args__ = (
        UniqueConstraint(
            "actor_id", "idempotency_key", name="uq_runtime_command_request"
        ),
        UniqueConstraint("project_id", "sequence", name="uq_runtime_command_sequence"),
        Index("ix_runtime_command_dispatch", "project_id", "state", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    lane: Mapped[str] = mapped_column(
        String(20), nullable=False, default="work", server_default="work"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    runtime_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class DevicePairingOfferRow(Base):
    """Single-use hashed pairing offers shared across replaceable API workers."""

    __tablename__ = "device_pairing_offers"
    token_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    confirmation_mac: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)


class RuntimeDeliveryRow(Base):
    """Immutable encrypted HTTP delivery frames, ordered within one command."""

    __tablename__ = "runtime_delivery"
    __table_args__ = (
        UniqueConstraint("command_id", "sequence", name="uq_runtime_delivery_sequence"),
        Index("ix_runtime_delivery_project", "project_id", "command_id"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    command_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    content_reference: Mapped[str] = mapped_column(String(64), nullable=False)


class RuntimeReleaseRow(Base):
    __tablename__ = "runtime_releases"
    version: Mapped[str] = mapped_column(String(200), primary_key=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    install_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    staged_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RuntimeProcessRow(Base):
    __tablename__ = "runtime_processes"
    runtime_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    process_birth: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(4096), nullable=False)
    credential_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class DeploymentHostRow(Base):
    __tablename__ = "deployment_host"
    key: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    api_runtime_id: Mapped[str | None] = mapped_column(String(200))
    target_version: Mapped[str | None] = mapped_column(String(200))
    rollback_version: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(String(4000), nullable=False)


class ProjectHandoffRow(Base):
    __tablename__ = "project_handoffs"
    project_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_runtime_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_runtime_id: Mapped[str] = mapped_column(String(200), nullable=False)
    target_version: Mapped[str] = mapped_column(String(200), nullable=False)
    phase: Mapped[str] = mapped_column(String(30), nullable=False)
    admission_held: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    blockers: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RuntimeResourceRow(Base):
    __tablename__ = "runtime_resources"
    __table_args__ = (Index("ix_runtime_resource_owner", "runtime_id", "state"),)
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    runtime_id: Mapped[str] = mapped_column(String(200), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    restoration: Mapped[str] = mapped_column(String(30), nullable=False)
    detail: Mapped[str] = mapped_column(String(4000), nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class DeploymentReceiptRow(Base):
    __tablename__ = "deployment_receipts"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EntityRow(Base):
    """Versioned JSON documents validated by the Pydantic domain boundary."""

    __tablename__ = "entities"
    __table_args__ = (
        Index("ix_entities_kind_engagement", "kind", "engagement_id"),
        Index(
            "ix_entities_kind_engagement_created_id",
            "kind",
            "engagement_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_entities_artifact_tool_call_created",
            "kind",
            "engagement_id",
            "artifact_tool_call_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_entities_tool_call_recovery_created",
            "kind",
            "tool_call_status",
            "tool_call_origin",
            "created_at",
            "id",
        ),
        Index(
            "ix_entities_tool_call_turn_status_created",
            "kind",
            "tool_call_harness_turn_id",
            "engagement_id",
            "tool_call_status",
            "created_at",
            "id",
        ),
        Index("ix_entities_engagement_updated", "engagement_id", "updated_at"),
        Index(
            "ix_entities_kind_engagement_updated_id",
            "kind",
            "engagement_id",
            "updated_at",
            "id",
        ),
        Index("ix_entities_kind_updated", "kind", "updated_at"),
        Index(
            "ix_entities_automation_run_session",
            "kind",
            "automation_run_id",
            "automation_session_id",
        ),
        Index(
            "ix_entities_automation_status_expiry",
            "kind",
            "automation_status",
            "automation_expires_at",
        ),
        Index(
            "ix_entities_kind_chat_session_created",
            "kind",
            "chat_session_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_entities_kind_harness_session_created",
            "kind",
            "harness_session_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_entities_chat_turn_status",
            "kind",
            "chat_turn_status",
            "engagement_id",
            "created_at",
            "id",
            "chat_session_id",
            "chat_turn_recovery_visible",
            "chat_turn_core_resume_pending",
        ),
        Index(
            "ix_entities_project_chat_turn_status",
            "kind",
            "engagement_id",
            "chat_turn_status",
            "created_at",
            "id",
            "chat_session_id",
            "chat_turn_recovery_visible",
            "chat_turn_core_resume_pending",
        ),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    engagement_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # These nullable projections keep the JSON entity envelope authoritative
    # while making the high-volume browser worker queries indexable.  They are
    # populated only for the run-scoped browser automation entity family.
    automation_run_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    automation_session_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    automation_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    automation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The JSON envelope remains authoritative. This nullable projection only
    # makes exact-conversation reads indexable for chat-owned entity families.
    chat_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Resolve names for a page of harness sessions without reading all turns.
    harness_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    runtime_switch_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Active-turn readers use these projections without fetching large JSON envelopes.
    chat_turn_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    chat_turn_recovery_visible: Mapped[bool | None] = mapped_column(nullable=True)
    chat_turn_core_resume_pending: Mapped[bool | None] = mapped_column(nullable=True)
    # Exact result and recovery lookups must not parse settled project history.
    # The JSON envelope remains authoritative; storage maintains these on writes.
    artifact_tool_call_id: Mapped[str | None] = mapped_column(String(200))
    tool_call_status: Mapped[str | None] = mapped_column(String(40))
    tool_call_origin: Mapped[str | None] = mapped_column(String(40))
    tool_call_harness_turn_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# Keep ORM metadata aligned with the additive lookup migrations.
Index(
    "ix_entities_harness_title_project",
    EntityRow.kind,
    EntityRow.harness_session_id,
    EntityRow.engagement_id,
    EntityRow.created_at,
    EntityRow.id,
)
for _index_name, _columns in (
    (
        "ix_entities_harness_chat_title",
        (
            EntityRow.kind,
            EntityRow.harness_session_id,
            EntityRow.created_at,
            EntityRow.id,
        ),
    ),
    (
        "ix_entities_harness_chat_title_project",
        (
            EntityRow.kind,
            EntityRow.harness_session_id,
            EntityRow.engagement_id,
            EntityRow.created_at,
            EntityRow.id,
        ),
    ),
):
    Index(
        _index_name,
        *_columns,
        sqlite_where=visible_harness_chat_title(EntityRow, "sqlite"),
        postgresql_where=visible_harness_chat_title(EntityRow, "postgresql"),
    )
Index(
    "ix_entities_runtime_switch",
    EntityRow.kind,
    EntityRow.runtime_switch_status,
    EntityRow.created_at,
    EntityRow.id,
)


class WorkspaceProvenanceBlobRow(Base):
    """One immutable chunk or manifest shared by workspace observations."""

    __tablename__ = "workspace_provenance_blobs"
    __table_args__ = (
        Index("ix_workspace_provenance_blobs_engagement", "engagement_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class WorkspaceProvenanceIndexRow(Base):
    """Small overlap index; the entity row remains the receipt authority."""

    __tablename__ = "workspace_provenance_index"
    __table_args__ = (
        Index(
            "ix_workspace_provenance_overlap",
            "workspace_key",
            "started_at",
            "completed_at",
        ),
        Index("ix_workspace_provenance_index_engagement", "engagement_id"),
    )

    observation_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    engagement_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    workspace_key: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceProvenanceOwnerRow(Base):
    """Latest matching mutation for a path and exact fingerprint in a workspace."""

    __tablename__ = "workspace_provenance_owners"
    __table_args__ = (
        Index("ix_workspace_provenance_owners_engagement", "engagement_id"),
        Index("ix_workspace_provenance_owners_observation", "observation_id"),
    )

    engagement_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    workspace_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    path_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    confidence: Mapped[str] = mapped_column(String(40), nullable=False)
    observation_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class SessionProjectionRow(Base):
    """Derived display watermark, separate from authoritative execution records."""

    __tablename__ = "session_projections"

    session_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)


class ResourceRelationRow(Base):
    """Indexed authoritative edges; entity arrays are compatibility projections."""

    __tablename__ = "resource_relations"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "source_kind",
            "source_id",
            "predicate",
            "target_kind",
            "target_id",
            name="uq_resource_relations_edge",
        ),
        Index(
            "ix_resource_relations_source",
            "project_id",
            "source_kind",
            "source_id",
            "predicate",
        ),
        Index(
            "ix_resource_relations_target",
            "project_id",
            "target_kind",
            "target_id",
            "predicate",
        ),
        Index("ix_resource_relations_lineage", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    source_id: Mapped[str] = mapped_column(String(4096), nullable=False)
    source_revision: Mapped[int | None] = mapped_column(Integer)
    predicate: Mapped[str] = mapped_column(String(80), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[str] = mapped_column(String(4096), nullable=False)
    target_revision: Mapped[int | None] = mapped_column(Integer)
    attribution: Mapped[str | None] = mapped_column(String(200))
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class SearchDocumentRow(Base):
    """Privacy-filtered metadata projection used by the universal omnibox."""

    __tablename__ = "search_documents"
    __table_args__ = (
        Index("ix_search_documents_project_kind", "project_id", "resource_kind"),
        Index("ix_search_documents_updated", "updated_at"),
        Index("ix_search_documents_label", "label"),
    )

    id: Mapped[str] = mapped_column(String(4096), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resource_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(4096), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    breadcrumb: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    content: Mapped[str] = mapped_column(String(8000), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RunEventRow(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_run_events_idempotency"),
        Index("ix_run_events_replay", "run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(300), nullable=True)


class OperationEventRow(Base):
    __tablename__ = "operation_events"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "sequence", name="uq_operation_events_sequence"
        ),
        UniqueConstraint(
            "operation_id",
            "idempotency_key",
            name="uq_operation_events_idempotency",
        ),
        Index("ix_operation_events_replay", "operation_id", "sequence"),
        Index("ix_operation_events_engagement_time", "engagement_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    engagement_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(300), nullable=True)


class RunBudgetCounterRow(Base):
    __tablename__ = "run_budget_counters"

    run_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    artifact_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_microusd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ChatTurnStepEventRow(Base):
    """Append-only provider/tool history for one durable chat turn."""

    __tablename__ = "chat_turn_step_events"
    __table_args__ = (
        UniqueConstraint("turn_id", "sequence", name="uq_chat_turn_steps_sequence"),
        UniqueConstraint(
            "turn_id", "idempotency_key", name="uq_chat_turn_steps_idempotency"
        ),
        Index("ix_chat_turn_steps_replay", "turn_id", "sequence"),
        Index("ix_chat_turn_steps_step", "turn_id", "step", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_group: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)


class ChatTurnCheckpointRow(Base):
    """Append-only deterministic replay checkpoint for a chat turn."""

    __tablename__ = "chat_turn_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "turn_id", "through_step", name="uq_chat_turn_checkpoint_boundary"
        ),
        Index("ix_chat_turn_checkpoints_latest", "turn_id", "through_step"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(200), nullable=False)
    through_step: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ProviderTurnQueueRow(Base):
    """Durable admission record; provider capacity is process-local."""

    __tablename__ = "provider_turn_queue"
    __table_args__ = (
        Index("ix_provider_turn_queue_dispatch", "state", "lane", "accepted_at"),
        Index("ix_provider_turn_queue_lease", "state", "lease_expires_at"),
    )

    turn_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    lane: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    admitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# The ORM never updates or deletes an event row. The history of a deleted
# record is removed by the bulk statements in ``event_history``, which the
# database triggers refuse while the owning record exists.
@event.listens_for(RunEventRow, "before_update")
def _prevent_event_update(*_: object) -> None:
    raise RuntimeError("run events are append-only")


@event.listens_for(RunEventRow, "before_delete")
def _prevent_event_delete(*_: object) -> None:
    raise RuntimeError("run events are append-only")


@event.listens_for(OperationEventRow, "before_update")
def _prevent_operation_event_update(*_: object) -> None:
    raise RuntimeError("operation events are append-only")


@event.listens_for(OperationEventRow, "before_delete")
def _prevent_operation_event_delete(*_: object) -> None:
    raise RuntimeError("operation events are append-only")


def _prevent_chat_turn_ledger_change(*_: object) -> None:
    raise RuntimeError("chat turn ledgers are append-only")


for _append_only_model in (ChatTurnStepEventRow, ChatTurnCheckpointRow):
    event.listen(_append_only_model, "before_update", _prevent_chat_turn_ledger_change)
    event.listen(_append_only_model, "before_delete", _prevent_chat_turn_ledger_change)


class SchemaVersionError(RuntimeError):
    """Raised when a database was created by an incompatible Nebula version."""


class DatabaseBusyError(PoolTimeoutError):
    """Bounded capacity was unavailable; no automatic write replay is implied."""


class Database:
    """One async engine per Core process and task-owned units of work."""

    def __init__(
        self,
        location: str | Path,
        *,
        echo: bool = False,
        bootstrap: bool = True,
        pool_settings: PoolSettings | None = None,
        execution_runtime_id: str | None = None,
    ) -> None:
        self.url = self._normalize_url(location)
        self.performance = PerformanceTelemetry()
        self._echo = echo
        self._bootstrap = bootstrap
        self._pool_settings = pool_settings
        if execution_runtime_id is not None and bootstrap:
            raise ValueError("execution workers must validate schema without migrating")
        self.execution_runtime_id = execution_runtime_id
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._active_session: ContextVar[
            tuple[asyncio.Task | None, AsyncSession] | None
        ] = ContextVar("nebula_database_session", default=None)
        self._bulk_owner: ContextVar[asyncio.Task | None] = ContextVar(
            "nebula_database_bulk_reader", default=None
        )
        self.budget: ConnectionBudget | None = None
        self.pool_size = (
            1
            if self.url.startswith("postgresql") or self.url.endswith(":memory:")
            else 5
        )
        self.pool_state = {
            "checked_out": 0,
            "peak_checked_out": 0,
            "waiters": 0,
            "timeouts": 0,
            "connection_failures": 0,
        }
        self.engine = self._engine(self.pool_size)
        self._session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, autoflush=False
        )
        self.bulk_slots = asyncio.Semaphore(max(1, min(4, self.pool_size - 1)))
        self.performance.database_snapshot = self.pool_snapshot

    def _engine(self, size: int):
        options: dict[str, Any] = {"echo": self._echo, "pool_pre_ping": True}
        memory = self.url.startswith("sqlite") and self.url.endswith(":memory:")
        options.update(
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1 if memory else size,
            max_overflow=0,
            pool_timeout=self._pool_settings.timeout if self._pool_settings else 30,
        )
        if self.url.startswith("sqlite"):
            options["connect_args"] = {"timeout": 30}
        engine = create_async_engine(self.url, **options)
        if engine.dialect.name == "sqlite":
            event.listen(engine.sync_engine, "connect", self._configure_sqlite)
        install_sql_timing(
            engine.sync_engine, self.performance, enabled=lambda: self._initialized
        )

        def checkout(*_):
            self.pool_state["checked_out"] += 1
            self.pool_state["peak_checked_out"] = max(
                self.pool_state["peak_checked_out"], self.pool_state["checked_out"]
            )

        def checkin(*_):
            self.pool_state["checked_out"] = max(0, self.pool_state["checked_out"] - 1)

        event.listen(engine.sync_engine, "checkout", checkout)
        event.listen(engine.sync_engine, "checkin", checkin)
        if self.execution_runtime_id is not None:
            from .execution_scope import ExecutionSQLScope

            ExecutionSQLScope(self.execution_runtime_id).install(engine.sync_engine)
        return engine

    @staticmethod
    def _normalize_url(location: str | Path) -> str:
        value = str(location)
        if value in {"sqlite://", "sqlite+pysqlite://", "sqlite+aiosqlite://"}:
            return "sqlite+aiosqlite:///:memory:"
        for scheme in ("postgresql://", "postgres://"):
            if value.startswith(scheme):
                return "postgresql+psycopg://" + value[len(scheme) :]
        if value.startswith(("sqlite://", "sqlite+pysqlite://")):
            return value.replace(value.split(":", 1)[0], "sqlite+aiosqlite", 1)
        if "://" in value:
            return value
        if value == ":memory:":
            return "sqlite+aiosqlite:///:memory:"
        path = Path(value).expanduser().resolve()
        return f"sqlite+aiosqlite:///{path}"

    @staticmethod
    def _normalize_sync_url(location: str | Path) -> str:
        """URL for standalone Alembic/import commands, never runtime sessions."""
        return Database._normalize_url(location).replace(
            "sqlite+aiosqlite:", "sqlite+pysqlite:", 1
        )

    @staticmethod
    def _configure_sqlite(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA database_list")
            for _, name, location in cursor.fetchall():
                if name == "main" and location:
                    for path in (
                        Path(location),
                        Path(location + "-wal"),
                        Path(location + "-shm"),
                    ):
                        if path.exists():
                            path.chmod(0o600)
        finally:
            cursor.close()

    async def initialize(self) -> None:
        async with self._initialize_lock:
            if self._initialized:
                return
            try:
                if self.engine.dialect.name == "postgresql":
                    settings = self._pool_settings or PoolSettings.from_environment()
                    async with self.engine.connect() as connection:
                        values = (
                            await connection.execute(
                                text("""
                            SELECT current_setting('max_connections')::int AS maximum,
                              current_setting('superuser_reserved_connections')::int +
                              COALESCE(current_setting('reserved_connections', true), '0')::int AS reserved,
                              (SELECT rolconnlimit FROM pg_roles WHERE rolname = session_user) AS role_limit,
                              (SELECT datconnlimit FROM pg_database WHERE datname = current_database()) AS database_limit
                        """)
                            )
                        ).one()
                    await self.engine.dispose()
                    self.budget = connection_budget(
                        settings,
                        max_connections=values.maximum,
                        postgres_reserved=values.reserved,
                        role_limit=values.role_limit,
                        database_limit=values.database_limit,
                    )
                    self._pool_settings = settings
                    self.pool_size = self.budget.pool_size
                    self.engine = self._engine(self.pool_size)
                    self._session_factory.configure(bind=self.engine)
                    self.bulk_slots = asyncio.Semaphore(
                        max(1, min(4, self.pool_size - 1))
                    )
                else:
                    database = self.engine.url.database
                    if database and database != ":memory:":
                        path = Path(database)
                        await asyncio.to_thread(
                            path.parent.mkdir, parents=True, exist_ok=True, mode=0o700
                        )
                        await asyncio.to_thread(path.parent.chmod, 0o700)
                if self._bootstrap:
                    await self.bootstrap()
                self._initialized = True
            except BaseException:
                self.pool_state["connection_failures"] += 1
                await self.engine.dispose()
                raise

    async def bootstrap(self) -> int:
        # Advisory-lock waits use the async driver. SQLite's nonblocking kernel
        # lock is polled without leaving a worker or file descriptor behind if
        # startup is cancelled while another process is migrating.
        descriptor = None
        database = self.engine.url.database
        try:
            if (
                self.engine.dialect.name == "sqlite"
                and database
                and database != ":memory:"
            ):
                descriptor = os.open(
                    database + ".bootstrap.lock",
                    os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:  # diagnostic-expected: another bootstrap owns the lock; retry cooperatively and let cancellation close the descriptor.
                        await asyncio.sleep(0.05)
            async with self.engine.connect() as connection:
                postgres = self.engine.dialect.name == "postgresql"
                if postgres:
                    await connection.execute(
                        text(
                            "SELECT pg_advisory_lock(hashtext('nebula:bootstrap'), hashtext(current_schema()))"
                        )
                    )
                    await connection.commit()
                try:
                    return await connection.run_sync(self._bootstrap_on_connection)
                finally:
                    if postgres:
                        await connection.rollback()
                        await connection.execute(
                            text(
                                "SELECT pg_advisory_unlock(hashtext('nebula:bootstrap'), hashtext(current_schema()))"
                            )
                        )
                        await connection.commit()
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _bootstrap_on_connection(connection: Any) -> int:
        empty = not inspect(connection).get_table_names()
        connection.commit()
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
        try:
            cast(Table, BootstrapStateRow.__table__).create(connection, checkfirst=True)
            marker = connection.scalar(
                select(BootstrapStateRow.key).where(
                    BootstrapStateRow.key == SCRATCH_PROJECT_BOOTSTRAP_KEY
                )
            )
            if marker is None:
                connection.execute(
                    insert(BootstrapStateRow).values(
                        key=SCRATCH_PROJECT_BOOTSTRAP_KEY,
                        status="eligible" if empty else "complete",
                        engagement_id=None,
                        created_at=utc_now(),
                        completed_at=None if empty else utc_now(),
                    )
                )
            connection.commit()
            config = Config()
            config.set_main_option(
                "script_location", str(Path(__file__).with_name("migrations"))
            )
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
            current = connection.scalar(select(func.max(SchemaVersionRow.version)))
            if current is not None and current > CURRENT_SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema {current} is newer than supported schema {CURRENT_SCHEMA_VERSION}"
                )
            if current is not None and current < 1:
                raise SchemaVersionError(f"no migration path from schema {current}")
            versions = (
                [CURRENT_SCHEMA_VERSION]
                if current is None
                else range(current + 1, CURRENT_SCHEMA_VERSION + 1)
            )
            for version in versions:
                connection.execute(
                    insert(SchemaVersionRow).values(
                        version=version, applied_at=utc_now()
                    )
                )
            connection.commit()
            return CURRENT_SCHEMA_VERSION
        except BaseException:
            connection.rollback()
            raise

    async def connect(self) -> AsyncConnection:
        """Acquire a ledger connection with the same bounded runtime budget."""
        await self.initialize()
        self.pool_state["waiters"] += 1
        started = time.perf_counter()
        try:
            return await self.engine.connect()
        except PoolTimeoutError as exc:
            self.pool_state["timeouts"] += 1
            raise DatabaseBusyError("Database connection capacity is busy") from exc
        except BaseException:
            self.pool_state["connection_failures"] += 1
            raise
        finally:
            self.pool_state["waiters"] -= 1
            self.performance.record(
                "db.pool.acquire", (time.perf_counter() - started) * 1000
            )

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        connection = await self.connect()
        try:
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def bulk(self) -> AsyncIterator[None]:
        """Share bounded read admission without inverting transaction locks."""
        task = asyncio.current_task()
        if self._bulk_owner.get() is task or self.task_session() is not None:
            yield
            return
        await self.initialize()
        with self.performance.span("db.bulk.wait"):
            await self.bulk_slots.acquire()
        token = self._bulk_owner.set(task)
        try:
            yield
        finally:
            self._bulk_owner.reset(token)
            self.bulk_slots.release()

    @asynccontextmanager
    async def session(self, *, bulk: bool = False) -> AsyncIterator[AsyncSession]:
        if bulk:
            async with self.bulk():
                async with self.session() as session:
                    yield session
            return
        current = self._active_session.get()
        if current is not None and current[0] is asyncio.current_task():
            # Read helpers inside a unit of work see that unit's own writes.
            # Child tasks inherit context, but never inherit its Session.
            yield current[1]
            return
        await self.initialize()
        session = self._session_factory()
        owner = self._active_session.set((asyncio.current_task(), session))
        self.pool_state["waiters"] += 1
        started = time.perf_counter()
        try:
            try:
                await session.connection()
            except (PoolTimeoutError, asyncio.CancelledError):
                raise
            except BaseException:
                self.pool_state["connection_failures"] += 1
                raise
            finally:
                self.pool_state["waiters"] -= 1
                self.performance.record(
                    "db.pool.acquire", (time.perf_counter() - started) * 1000
                )
            with self.performance.span("db.session"):
                yield session
                with self.performance.span("db.commit"):
                    await session.commit()
        except PoolTimeoutError as exc:
            self.pool_state["timeouts"] += 1
            await session.rollback()
            raise DatabaseBusyError("Database connection capacity is busy") from exc
        except BaseException:
            await session.rollback()
            raise
        finally:
            self._active_session.reset(owner)
            # Repeated caller cancellation must still join connection cleanup.
            await finish_cleanup(session.close())

    def task_session(self) -> AsyncSession | None:
        current = self._active_session.get()
        return (
            current[1]
            if current is not None and current[0] is asyncio.current_task()
            else None
        )

    async def current_schema_version(self) -> int:
        async with self.session() as session:
            value = await session.scalar(select(func.max(SchemaVersionRow.version)))
            if value is None:
                raise SchemaVersionError("database has not been bootstrapped")
            return int(value)

    async def health(self) -> dict[str, Any]:
        async with self.session() as session:
            await session.execute(text("SELECT 1"))
            version = await session.scalar(select(func.max(SchemaVersionRow.version)))
            result = {
                "database": "ok",
                "dialect": self.engine.dialect.name,
                "schema_version": version,
            }
            if self.engine.dialect.name == "sqlite":
                result["journal_mode"] = await session.scalar(
                    text("PRAGMA journal_mode")
                )
            return result

    def pool_snapshot(self) -> dict[str, Any]:
        return {
            "budget": self.budget.snapshot() if self.budget else None,
            "configured_size": self.pool_size,
            "max_overflow": 0,
            **self.pool_state,
        }

    async def dispose(self) -> None:
        await self.engine.dispose()
        self._initialized = False


class DatabaseInUseError(RuntimeError):
    """Raised when maintenance needs a database no other connection has open."""


def vacuum_sqlite(path: Path) -> dict[str, Any]:
    """Rewrite a SQLite database file so the pages deletes freed leave it.

    SQLite keeps freed pages inside the file; only VACUUM returns them to the
    disk. It refuses while any other connection, such as a running Core, has
    the database open. It needs free disk for a temporary copy of the database
    and a write-ahead log of the same size.
    """

    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(f"database not found: {database}")
    wal = Path(f"{database}-wal")

    def stored_bytes() -> int:
        return sum(item.stat().st_size for item in (database, wal) if item.exists())

    before = stored_bytes()
    started = time.monotonic()
    connection = sqlite3.connect(database, isolation_level=None, timeout=1)
    try:
        # Exclusive locking keeps the lock this transaction takes until the
        # connection closes: nothing can open the database mid-rewrite, and a
        # connection that already has it open makes this refuse.
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:  # diagnostic-expected: an open database is refused as DatabaseInUseError for the caller to report.
            if "locked" not in str(exc):
                raise
            raise DatabaseInUseError(
                "the database is open elsewhere; stop Nebula Core and retry"
            ) from exc
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        connection.execute("COMMIT")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    database.chmod(0o600)
    return {
        "database": str(database),
        "bytes_before": before,
        "bytes_after": stored_bytes(),
        "free_pages_before": free_pages,
        "seconds": round(time.monotonic() - started, 1),
    }
