"""Durable device-aware action routing with prepare/commit/apply semantics."""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from .action_registry import ACTION_CATALOG, ActionRegistry
from .database import EntityRow
from .domain import (
    ActionAuthority,
    ActionIntent,
    ActionIntentStatus,
    ActionResolutionRequest,
    DeviceCapabilitySnapshot,
    NebulaModel,
    PairedDeviceSession,
    ResourceRef,
    utc_now,
)
from .relations import RESOURCE_ENTITY_KINDS
from .storage import ConflictError, NebulaStore

HEALTH_WINDOW_SECONDS = 45
INTENT_EXPIRY_MINUTES = 5
LEASE_SECONDS = 30
TERMINAL_STATUSES = {
    ActionIntentStatus.SUCCEEDED,
    ActionIntentStatus.FAILED,
    ActionIntentStatus.COMPENSATED,
    ActionIntentStatus.RECONCILE_REQUIRED,
    ActionIntentStatus.CANCELLED,
    ActionIntentStatus.EXPIRED,
}


class ActionIntentCreateRequest(NebulaModel):
    project_id: str = Field(min_length=1, max_length=200)
    resources: list[ResourceRef] = Field(min_length=1, max_length=100)
    action_id: str = Field(min_length=1, max_length=80)
    requester: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=300)
    preferred_device_id: str | None = Field(default=None, max_length=200)
    core_mutation_committed: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class ActionIntentClaimRequest(NebulaModel):
    device_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)


class ActionIntentPrepareRequest(ActionIntentClaimRequest):
    preflight_succeeded: bool
    error: str | None = Field(default=None, max_length=2_000)


class ActionIntentCommitRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    core_mutation_committed: bool = False


class ActionIntentResultRequest(ActionIntentClaimRequest):
    succeeded: bool
    receipt: dict[str, object] | None = None
    result_refs: list[ResourceRef] = Field(default_factory=list, max_length=100)
    error: str | None = Field(default=None, max_length=2_000)
    compensation_succeeded: bool | None = None


class ActionIntentCancelRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class ActionBroker:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.registry = ActionRegistry(store)

    @staticmethod
    def healthy(device: PairedDeviceSession) -> bool:
        return bool(
            device.revoked_at is None
            and device.heartbeat_at is not None
            and (utc_now() - device.heartbeat_at).total_seconds()
            <= HEALTH_WINDOW_SECONDS
        )

    def heartbeat(
        self, device_id: str, snapshot: DeviceCapabilitySnapshot
    ) -> PairedDeviceSession:
        device = self.store.get(PairedDeviceSession, device_id)
        if device.revoked_at is not None:
            raise ConflictError("paired device is revoked")
        expected = snapshot.expected_revision or device.revision
        return self.store.update(
            PairedDeviceSession,
            device.id,
            {
                "platform": snapshot.platform,
                "app_version": snapshot.app_version,
                "capabilities": sorted(set(snapshot.capabilities)),
                "ownership_claims": snapshot.ownership_claims,
                "heartbeat_at": snapshot.heartbeat_at,
                "last_used_at": snapshot.heartbeat_at,
            },
            expected_revision=expected,
        )

    def _transition(
        self,
        intent: ActionIntent,
        changes: dict[str, object],
        event_type: str,
        *,
        actor_id: str,
        expected_revision: int,
    ) -> ActionIntent:
        updated, _ = self.store.update_with_operation_event(
            ActionIntent,
            intent.id,
            changes,
            expected_revision=expected_revision,
            operation_id=intent.id,
            operation_kind="action_intent",
            engagement_id=intent.engagement_id,
            event_type=event_type,
            event_payload={"intent_id": intent.id, "action_id": intent.action_id},
            actor_id=actor_id,
            idempotency_key=f"{event_type}:{expected_revision}",
        )
        return updated

    def create(self, request: ActionIntentCreateRequest) -> ActionIntent:
        existing = [
            item
            for item in self.store.list_entities(
                ActionIntent, engagement_id=request.project_id, limit=1_000
            )
            if item.idempotency_key == request.idempotency_key
        ]
        if existing:
            intent = existing[0]
            if (
                intent.action_id != request.action_id
                or intent.resources != request.resources
            ):
                raise ConflictError("idempotency key belongs to another action intent")
            return intent
        descriptor = next(
            (item for item in ACTION_CATALOG if item.id == request.action_id), None
        )
        if descriptor is None or descriptor.authority != ActionAuthority.DEVICE:
            raise ValueError("action intent requires a registered device action")
        devices = [
            item
            for item in self.store.list_entities(PairedDeviceSession, limit=1_000)
            if self.healthy(item)
        ]
        eligible = []
        for device in devices:
            resolved = self.registry.resolve(
                ActionResolutionRequest(
                    resources=request.resources,
                    device_id=device.id,
                    device_capabilities=device.capabilities,
                )
            )
            action = next(
                (item for item in resolved if item.id == request.action_id), None
            )
            if action is not None and action.available:
                eligible.append(device)
        if not eligible:
            required = ", ".join(descriptor.required_capabilities) or "this action"
            raise ConflictError(f"no healthy paired device provides {required}")
        claims = [
            device
            for device in eligible
            if any(
                claim.project_id == ref.project_id
                and claim.kind == ref.kind
                and claim.id == ref.id
                for claim in device.ownership_claims
                for ref in request.resources
            )
        ]
        selected = None
        if request.preferred_device_id:
            if request.preferred_device_id not in {item.id for item in eligible}:
                raise ConflictError("preferred device is not healthy and eligible")
            selected = request.preferred_device_id
        elif len(claims) == 1:
            selected = claims[0].id
        now = utc_now()
        expected_revisions = {
            f"{ref.kind.value}:{ref.id}": ref.revision
            for ref in request.resources
            if ref.revision is not None
        }
        intent = ActionIntent(
            engagement_id=request.project_id,
            resources=request.resources,
            action_id=request.action_id,
            requester=request.requester,
            eligible_device_ids=sorted(item.id for item in eligible),
            selected_device_id=selected,
            idempotency_key=request.idempotency_key,
            expected_revisions=expected_revisions,
            logical_lease_key="|".join(
                sorted(f"{ref.kind.value}:{ref.id}" for ref in request.resources)
            ),
            expires_at=now + timedelta(minutes=INTENT_EXPIRY_MINUTES),
            core_mutation_committed=request.core_mutation_committed,
            metadata=request.metadata,
        )
        created, _ = self.store.create_with_operation_event(
            intent,
            operation_id=intent.id,
            operation_kind="action_intent",
            engagement_id=intent.engagement_id,
            event_type="action_intent.queued",
            event_payload={"intent_id": intent.id, "action_id": intent.action_id},
            actor_id=request.requester,
            idempotency_key="queued",
        )
        return created

    def _current(self, intent_id: str) -> ActionIntent:
        intent = self.store.get(ActionIntent, intent_id)
        now = utc_now()
        if intent.status not in TERMINAL_STATUSES and now >= intent.expires_at:
            return self._transition(
                intent,
                {"status": ActionIntentStatus.EXPIRED, "lease_expires_at": None},
                "action_intent.expired",
                actor_id="core",
                expected_revision=intent.revision,
            )
        if (
            intent.status in {ActionIntentStatus.CLAIMED, ActionIntentStatus.PREPARED}
            and intent.lease_expires_at is not None
            and now >= intent.lease_expires_at
        ):
            return self._transition(
                intent,
                {
                    "status": ActionIntentStatus.QUEUED,
                    "selected_device_id": None,
                    "lease_expires_at": None,
                    "prepared_at": None,
                },
                "action_intent.claim_recovered",
                actor_id="core",
                expected_revision=intent.revision,
            )
        return intent

    def get(self, intent_id: str) -> ActionIntent:
        return self._current(intent_id)

    def list_intents(self, project_id: str) -> list[ActionIntent]:
        return [
            self._current(item.id)
            for item in self.store.list_entities(
                ActionIntent, engagement_id=project_id, limit=1_000
            )
        ]

    def claim(self, intent_id: str, request: ActionIntentClaimRequest) -> ActionIntent:
        intent = self._current(intent_id)
        if intent.status != ActionIntentStatus.QUEUED:
            raise ConflictError("action intent is not queued")
        if request.device_id not in intent.eligible_device_ids:
            raise ConflictError("device is not eligible for this action intent")
        if intent.selected_device_id and intent.selected_device_id != request.device_id:
            raise ConflictError("action intent belongs to another device")
        device = self.store.get(PairedDeviceSession, request.device_id)
        if not self.healthy(device):
            raise ConflictError("device is no longer healthy")
        return self._transition(
            intent,
            {
                "status": ActionIntentStatus.CLAIMED,
                "selected_device_id": request.device_id,
                "lease_expires_at": utc_now() + timedelta(seconds=LEASE_SECONDS),
            },
            "action_intent.claimed",
            actor_id=f"device:{request.device_id}",
            expected_revision=request.expected_revision,
        )

    def prepare(
        self, intent_id: str, request: ActionIntentPrepareRequest
    ) -> ActionIntent:
        intent = self._current(intent_id)
        if intent.status != ActionIntentStatus.CLAIMED:
            raise ConflictError("action intent must be claimed before prepare")
        if intent.selected_device_id != request.device_id:
            raise ConflictError("only the claiming device may prepare this action")
        device = self.store.get(PairedDeviceSession, request.device_id)
        if not self.healthy(device):
            return self._transition(
                intent,
                {
                    "status": ActionIntentStatus.FAILED,
                    "error": "claiming device became unavailable before preflight",
                    "lease_expires_at": None,
                },
                "action_intent.device_unavailable",
                actor_id="core",
                expected_revision=request.expected_revision,
            )
        if not request.preflight_succeeded:
            return self._transition(
                intent,
                {
                    "status": ActionIntentStatus.FAILED,
                    "error": request.error or "device preflight failed",
                    "lease_expires_at": None,
                },
                "action_intent.preflight_failed",
                actor_id=f"device:{request.device_id}",
                expected_revision=request.expected_revision,
            )
        return self._transition(
            intent,
            {
                "status": ActionIntentStatus.PREPARED,
                "prepared_at": utc_now(),
                "lease_expires_at": utc_now() + timedelta(seconds=LEASE_SECONDS),
            },
            "action_intent.prepared",
            actor_id=f"device:{request.device_id}",
            expected_revision=request.expected_revision,
        )

    def commit(
        self, intent_id: str, request: ActionIntentCommitRequest
    ) -> ActionIntent:
        intent = self._current(intent_id)
        if intent.status != ActionIntentStatus.PREPARED:
            raise ConflictError("action intent must be prepared before commit")
        with self.store.database.session() as session:
            for ref in intent.resources:
                entity_kind = RESOURCE_ENTITY_KINDS.get(ref.kind)
                if entity_kind is None or ref.revision is None:
                    continue
                row = session.get(EntityRow, ref.id)
                if (
                    row is None
                    or row.kind != entity_kind
                    or row.revision != ref.revision
                ):
                    raise ConflictError("resource changed before action intent commit")
        return self._transition(
            intent,
            {
                "status": ActionIntentStatus.COMMITTED,
                "committed_at": utc_now(),
                "core_mutation_committed": request.core_mutation_committed,
                "lease_expires_at": utc_now() + timedelta(seconds=LEASE_SECONDS),
            },
            "action_intent.committed",
            actor_id="core",
            expected_revision=request.expected_revision,
        )

    def result(
        self, intent_id: str, request: ActionIntentResultRequest
    ) -> ActionIntent:
        intent = self._current(intent_id)
        if intent.status != ActionIntentStatus.COMMITTED:
            raise ConflictError("action intent must be committed before result")
        if intent.selected_device_id != request.device_id:
            raise ConflictError("only the selected device may complete this action")
        device = self.store.get(PairedDeviceSession, request.device_id)
        if not self.healthy(device):
            request = request.model_copy(
                update={
                    "succeeded": False,
                    "error": "selected device was revoked or became unhealthy",
                    "compensation_succeeded": False,
                }
            )
        if request.succeeded:
            return self._transition(
                intent,
                {
                    "status": ActionIntentStatus.SUCCEEDED,
                    "receipt": request.receipt or {},
                    "result_refs": request.result_refs,
                    "lease_expires_at": None,
                },
                "action_intent.succeeded",
                actor_id=f"device:{request.device_id}",
                expected_revision=request.expected_revision,
            )
        if not intent.core_mutation_committed:
            return self._transition(
                intent,
                {
                    "status": ActionIntentStatus.FAILED,
                    "error": request.error or "native action failed",
                    "lease_expires_at": None,
                },
                "action_intent.failed",
                actor_id=f"device:{request.device_id}",
                expected_revision=request.expected_revision,
            )
        compensating = self._transition(
            intent,
            {
                "status": ActionIntentStatus.COMPENSATING,
                "error": request.error or "native action failed",
            },
            "action_intent.compensating",
            actor_id="core",
            expected_revision=request.expected_revision,
        )
        compensated = request.compensation_succeeded is True
        return self._transition(
            compensating,
            {
                "status": ActionIntentStatus.COMPENSATED
                if compensated
                else ActionIntentStatus.RECONCILE_REQUIRED,
                "lease_expires_at": None,
            },
            "action_intent.compensated"
            if compensated
            else "action_intent.reconcile_required",
            actor_id="core",
            expected_revision=compensating.revision,
        )

    def cancel(
        self, intent_id: str, request: ActionIntentCancelRequest, actor_id: str
    ) -> ActionIntent:
        intent = self._current(intent_id)
        if intent.status not in {
            ActionIntentStatus.QUEUED,
            ActionIntentStatus.CLAIMED,
            ActionIntentStatus.PREPARED,
        }:
            raise ConflictError("action intent can no longer be cancelled")
        return self._transition(
            intent,
            {
                "status": ActionIntentStatus.CANCELLED,
                "error": request.reason,
                "lease_expires_at": None,
            },
            "action_intent.cancelled",
            actor_id=actor_id,
            expected_revision=request.expected_revision,
        )
