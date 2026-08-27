"""Durable, privacy-preserving resource handoffs between surfaces and devices."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .database import EntityRow
from .domain import (
    Engagement,
    HandoffEnvelope,
    HandoffStatus,
    ResourceKind,
    ResourceRef,
    utc_now,
)
from .relations import RESOURCE_ENTITY_KINDS
from .storage import ConflictError, NebulaStore

HANDOFF_TTL = timedelta(hours=24)


class HandoffCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(max_length=200)
    source_refs: list[ResourceRef] = Field(default_factory=list, max_length=20)
    action_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    target_ref: ResourceRef | None = None
    origin_device_id: str = Field(min_length=1, max_length=200)
    source_hashes: dict[str, str] = Field(default_factory=dict, max_length=20)
    source_labels: dict[str, str] = Field(default_factory=dict, max_length=20)
    transient: bool = False


class HandoffConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    device_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=300)


class HandoffCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class HandoffSourceState(BaseModel):
    ref: ResourceRef
    state: Literal["available", "changed", "deleted", "origin_required"]
    label: str


class HandoffResolution(BaseModel):
    envelope: HandoffEnvelope
    sources: list[HandoffSourceState]
    recovery: Literal["ready", "resume_origin", "preserve_or_recapture"]


class HandoffService:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    @staticmethod
    def _key(ref: ResourceRef) -> str:
        return f"{ref.kind.value}:{ref.id}"

    def _validate_ref(self, project_id: str, ref: ResourceRef) -> None:
        if ref.project_id != project_id:
            raise ValueError("handoff references must belong to the requested project")
        entity_kind = RESOURCE_ENTITY_KINDS.get(ref.kind)
        if entity_kind is None:
            if ref.kind in {ResourceKind.WORKSPACE_FILE, ResourceKind.BROWSER_TAB}:
                return
            raise ValueError(f"unsupported handoff resource kind: {ref.kind.value}")
        with self.store.database.session() as session:
            row = session.get(EntityRow, ref.id)
            if row is None or row.kind != entity_kind:
                raise ValueError(
                    f"handoff source no longer exists: {ref.kind.value}/{ref.id}"
                )
            actual_project = (
                row.id if ref.kind == ResourceKind.PROJECT else row.engagement_id
            )
            if actual_project != project_id:
                raise ValueError("handoff reference belongs to another project")
            if ref.revision is not None and ref.revision != row.revision:
                raise ConflictError(
                    f"revision conflict: expected {ref.revision}, found {row.revision}"
                )

    def create(
        self, request: HandoffCreateRequest, *, actor_id: str
    ) -> HandoffEnvelope:
        self.store.get(Engagement, request.project_id)
        if not request.source_refs and not request.transient:
            raise ValueError(
                "handoffs require a durable source or a transient-source marker"
            )
        for ref in request.source_refs:
            self._validate_ref(request.project_id, ref)
        if request.target_ref:
            self._validate_ref(request.project_id, request.target_ref)
        envelope = HandoffEnvelope(
            engagement_id=request.project_id,
            source_refs=request.source_refs,
            action_id=request.action_id,
            target_ref=request.target_ref,
            origin_device_id=request.origin_device_id,
            source_hashes=request.source_hashes,
            source_labels=request.source_labels,
            transient=request.transient,
            expires_at=utc_now() + HANDOFF_TTL,
        )
        self.store.create_with_operation_event(
            envelope,
            operation_id=envelope.id,
            operation_kind="handoff",
            engagement_id=request.project_id,
            event_type="handoff.created",
            event_payload={
                "handoff_id": envelope.id,
                "action_id": envelope.action_id,
                "source_count": len(envelope.source_refs),
                "transient": envelope.transient,
            },
            actor_id=actor_id,
            idempotency_key=f"handoff-created:{envelope.id}",
        )
        return envelope

    def _expire(self, envelope: HandoffEnvelope) -> HandoffEnvelope:
        if envelope.status != HandoffStatus.PENDING or envelope.expires_at > utc_now():
            return envelope
        updated, _ = self.store.update_with_operation_event(
            HandoffEnvelope,
            envelope.id,
            {"status": HandoffStatus.EXPIRED},
            expected_revision=envelope.revision,
            operation_id=envelope.id,
            operation_kind="handoff",
            engagement_id=envelope.engagement_id,
            event_type="handoff.expired",
            event_payload={"handoff_id": envelope.id},
            actor_id="core",
            idempotency_key=f"handoff-expired:{envelope.id}",
        )
        return updated

    def get(self, handoff_id: str) -> HandoffEnvelope:
        return self._expire(self.store.get(HandoffEnvelope, handoff_id))

    def list(self, project_id: str, *, limit: int = 100) -> list[HandoffEnvelope]:
        return [
            self._expire(item)
            for item in self.store.list_entities(
                HandoffEnvelope, engagement_id=project_id, limit=limit
            )
        ]

    def resolve(
        self, handoff_id: str, *, current_device_id: str | None
    ) -> HandoffResolution:
        envelope = self.get(handoff_id)
        sources: list[HandoffSourceState] = []
        for ref in envelope.source_refs:
            key = self._key(ref)
            label = envelope.source_labels.get(key, ref.id)
            entity_kind = RESOURCE_ENTITY_KINDS.get(ref.kind)
            state: Literal["available", "changed", "deleted", "origin_required"]
            if entity_kind is None:
                state = "origin_required" if envelope.transient else "available"
            else:
                with self.store.database.session() as session:
                    row = session.get(EntityRow, ref.id)
                if row is None or row.kind != entity_kind:
                    state = "deleted"
                elif ref.revision is not None and row.revision != ref.revision:
                    state = "changed"
                else:
                    durable_hash = next(
                        (
                            str(row.payload.get(field))
                            for field in ("sha256", "command_sha256", "source_sha256")
                            if row.payload.get(field)
                        ),
                        None,
                    )
                    expected_hash = envelope.source_hashes.get(key)
                    state = (
                        "changed"
                        if durable_hash
                        and expected_hash
                        and durable_hash != expected_hash
                        else "available"
                    )
            sources.append(HandoffSourceState(ref=ref, state=state, label=label))
        unavailable = any(item.state in {"changed", "deleted"} for item in sources)
        recovery: Literal["ready", "resume_origin", "preserve_or_recapture"]
        if envelope.transient:
            recovery = (
                "resume_origin"
                if current_device_id != envelope.origin_device_id
                else "preserve_or_recapture"
            )
        elif unavailable:
            recovery = "preserve_or_recapture"
        else:
            recovery = "ready"
        return HandoffResolution(envelope=envelope, sources=sources, recovery=recovery)

    def consume(
        self, handoff_id: str, request: HandoffConsumeRequest
    ) -> HandoffEnvelope:
        envelope = self.get(handoff_id)
        if envelope.status == HandoffStatus.CONSUMED:
            if envelope.consume_idempotency_key == request.idempotency_key:
                return envelope
            raise ConflictError("handoff was already consumed")
        if envelope.status != HandoffStatus.PENDING:
            raise ConflictError(f"cannot consume a {envelope.status.value} handoff")
        updated, _ = self.store.update_with_operation_event(
            HandoffEnvelope,
            envelope.id,
            {
                "status": HandoffStatus.CONSUMED,
                "consumed_at": utc_now(),
                "consumed_by_device_id": request.device_id,
                "consume_idempotency_key": request.idempotency_key,
            },
            expected_revision=request.expected_revision,
            operation_id=envelope.id,
            operation_kind="handoff",
            engagement_id=envelope.engagement_id,
            event_type="handoff.consumed",
            event_payload={"handoff_id": envelope.id, "device_id": request.device_id},
            actor_id=f"device:{request.device_id}",
            idempotency_key=request.idempotency_key,
        )
        return updated

    def cancel(
        self, handoff_id: str, request: HandoffCancelRequest, *, actor_id: str
    ) -> HandoffEnvelope:
        envelope = self.get(handoff_id)
        if envelope.status == HandoffStatus.CANCELLED:
            return envelope
        if envelope.status != HandoffStatus.PENDING:
            raise ConflictError(f"cannot cancel a {envelope.status.value} handoff")
        updated, _ = self.store.update_with_operation_event(
            HandoffEnvelope,
            envelope.id,
            {"status": HandoffStatus.CANCELLED},
            expected_revision=request.expected_revision,
            operation_id=envelope.id,
            operation_kind="handoff",
            engagement_id=envelope.engagement_id,
            event_type="handoff.cancelled",
            event_payload={"handoff_id": envelope.id},
            actor_id=actor_id,
            idempotency_key=f"handoff-cancelled:{envelope.id}",
        )
        return updated
