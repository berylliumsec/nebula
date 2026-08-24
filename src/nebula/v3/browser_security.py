"""Durable, scope-bound security-browser workflow services.

Cookie jars and live webviews are native runtime state. This module owns the
non-secret research timeline and enforces every mutation that can cause network
or browser effects.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Literal

from pydantic import Field, field_validator

from .domain import (
    BrowserAction,
    BrowserActionKind,
    BrowserActionStatus,
    BrowserCaptureMode,
    BrowserHandoff,
    BrowserHandoffStatus,
    BrowserIdentity,
    BrowserSession,
    BrowserSessionStatus,
    BrowserTabState,
    BrowserTrafficExchange,
    BrowserWebSocketFrame,
    Engagement,
    Evidence,
    NebulaModel,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from .policy import PolicyEffect, PolicyEngine, PolicyRequest
from .storage import NebulaStore


MAX_HEADERS = 200
MAX_HEADER_NAME = 256
MAX_HEADER_VALUE = 8_192
ACTION_LIFETIME = timedelta(minutes=10)
HANDOFF_LIFETIME = timedelta(minutes=5)
SENSITIVE_HEADER_FRAGMENTS = (
    "authorization",
    "cookie",
    "csrf",
    "xsrf",
    "api-key",
    "apikey",
    "token",
)


class BrowserWorkflowError(ValueError):
    """A browser workflow request is invalid or violates scope/state policy."""


class BrowserWorkspace(NebulaModel):
    identities: list[BrowserIdentity]
    sessions: list[BrowserSession]
    traffic: list[BrowserTrafficExchange]
    frames: list[BrowserWebSocketFrame]
    actions: list[BrowserAction]
    handoffs: list[BrowserHandoff]


class BrowserIdentityCreateRequest(NebulaModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    color: str = Field(default="#7c6cff", pattern=r"^#[0-9a-fA-F]{6}$")
    ephemeral: bool = False


class BrowserSessionCreateRequest(NebulaModel):
    name: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    capture_mode: BrowserCaptureMode = BrowserCaptureMode.HEADERS


class BrowserSessionSyncRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    tabs: list[BrowserTabState] = Field(max_length=16)
    active_tab_id: str | None = Field(default=None, max_length=200)
    device_owner: str = Field(min_length=1, max_length=200)


class BrowserCaptureSettingsRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    capture_mode: BrowserCaptureMode
    proxy_enabled: bool
    interception_enabled: bool
    upstream_proxy_enabled: bool = False
    upstream_proxy_url: str | None = Field(default=None, max_length=2_048)


class BrowserTrafficRecordRequest(NebulaModel):
    tab_id: str = Field(min_length=1, max_length=200)
    method: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url: str = Field(min_length=1, max_length=16_384)
    protocol: Literal["http/1.0", "http/1.1", "h2", "h3", "websocket", "unknown"] = "unknown"
    status_code: int | None = Field(default=None, ge=100, le=999)
    request_headers: dict[str, str] = Field(default_factory=dict)
    response_headers: dict[str, str] = Field(default_factory=dict)
    request_body_artifact_id: str | None = Field(default=None, max_length=200)
    response_body_artifact_id: str | None = Field(default=None, max_length=200)
    request_bytes: int | None = Field(default=None, ge=0)
    response_bytes: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    replay_of_exchange_id: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=4_000)
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request_headers", "response_headers")
    @classmethod
    def headers_are_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_HEADERS:
            raise ValueError(f"browser traffic permits at most {MAX_HEADERS} headers")
        if any(len(name) > MAX_HEADER_NAME or len(item) > MAX_HEADER_VALUE for name, item in value.items()):
            raise ValueError("browser traffic header name or value is too large")
        return value


class BrowserWebSocketFrameRecordRequest(NebulaModel):
    exchange_id: str = Field(min_length=1, max_length=200)
    direction: Literal["client", "server"]
    opcode: Literal["text", "binary", "ping", "pong", "close"]
    payload_preview: str = Field(default="", max_length=2_000)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_bytes: int = Field(ge=0)
    truncated: bool = False


class BrowserActionProposalRequest(NebulaModel):
    tab_id: str = Field(min_length=1, max_length=200)
    kind: BrowserActionKind
    locator: dict[str, str] = Field(default_factory=dict)
    arguments: dict[str, Any] = Field(default_factory=dict)
    proposal: str = Field(min_length=1, max_length=4_000)
    proposed_by: str = Field(min_length=1, max_length=200)
    page_url: str = Field(min_length=1, max_length=16_384)


class BrowserActionDecisionRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    operator_id: str = Field(min_length=1, max_length=200)
    decision: Literal["approve", "reject"]


class BrowserActionExecutionRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    device_id: str = Field(min_length=1, max_length=200)


class BrowserActionResultRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    device_id: str = Field(min_length=1, max_length=200)
    state: Literal["complete", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    error: str | None = Field(default=None, max_length=4_000)


class BrowserHandoffCreateRequest(NebulaModel):
    requested_by_device_id: str = Field(min_length=1, max_length=200)
    command: Literal["navigate", "focus_tab"]
    tab_id: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=16_384)


class BrowserHandoffClaimRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    desktop_device_id: str = Field(min_length=1, max_length=200)


class BrowserHandoffResultRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    desktop_device_id: str = Field(min_length=1, max_length=200)
    state: Literal["complete", "failed"]
    error: str | None = Field(default=None, max_length=4_000)


def redact_browser_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove reusable secrets while retaining equality/difference information."""

    redacted: dict[str, str] = {}
    for name, value in headers.items():
        normalized = name.strip().lower()
        if any(fragment in normalized for fragment in SENSITIVE_HEADER_FRAGMENTS):
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            redacted[name] = f"<redacted:sha256:{digest}>"
        else:
            redacted[name] = value
    return redacted


def _action_digest(request: BrowserActionProposalRequest, session: BrowserSession, scope: ScopePolicy) -> str:
    payload = {
        "session_id": session.id,
        "identity_id": session.identity_id,
        "tab_id": request.tab_id,
        "kind": request.kind.value,
        "locator": request.locator,
        "arguments": request.arguments,
        "page_url": request.page_url,
        "scope_policy_id": scope.id,
        "scope_policy_revision": scope.revision,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class BrowserSecurityService:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.policy = PolicyEngine()

    def workspace(self, engagement_id: str) -> BrowserWorkspace:
        self.store.get(Engagement, engagement_id)
        identities = self.store.list_entities(BrowserIdentity, engagement_id=engagement_id, limit=1_000)
        sessions = self.store.list_entities(BrowserSession, engagement_id=engagement_id, limit=1_000)
        if not identities:
            identity = BrowserIdentity(
                engagement_id=engagement_id,
                name="Default identity",
                description="Project-isolated browser profile",
                is_default=True,
                # Preserve the pre-identity Project profile instead of logging the
                # operator out during migration. Native treats this reserved
                # partition as the historical Project-only storage key.
                storage_partition="browser-00000000-0000-0000-0000-000000000000",
            )
            self._create(identity, "browser_identity.created", "system")
            identities = [identity]
        if not sessions:
            session = BrowserSession(
                engagement_id=engagement_id,
                name="Research session",
                identity_id=identities[0].id,
            )
            self._create(session, "browser_session.created", "system")
            sessions = [session]
        return BrowserWorkspace(
            identities=identities,
            sessions=sessions,
            traffic=self.store.list_entities(BrowserTrafficExchange, engagement_id=engagement_id, limit=1_000),
            frames=self.store.list_entities(BrowserWebSocketFrame, engagement_id=engagement_id, limit=1_000),
            actions=self.store.list_entities(BrowserAction, engagement_id=engagement_id, limit=1_000),
            handoffs=self.store.list_entities(BrowserHandoff, engagement_id=engagement_id, limit=1_000),
        )

    def create_identity(self, engagement_id: str, request: BrowserIdentityCreateRequest, actor_id: str) -> BrowserIdentity:
        self.store.get(Engagement, engagement_id)
        identity = BrowserIdentity(engagement_id=engagement_id, **request.model_dump())
        return self._create(identity, "browser_identity.created", actor_id)

    def create_session(self, engagement_id: str, request: BrowserSessionCreateRequest, actor_id: str) -> BrowserSession:
        identity = self._owned(BrowserIdentity, request.identity_id, engagement_id)
        if identity.revoked_at is not None:
            raise BrowserWorkflowError("revoked browser identities cannot start sessions")
        session = BrowserSession(engagement_id=engagement_id, **request.model_dump())
        return self._create(session, "browser_session.created", actor_id)

    def sync_session(self, session_id: str, request: BrowserSessionSyncRequest) -> BrowserSession:
        session = self.store.get(BrowserSession, session_id)
        if session.status != BrowserSessionStatus.ACTIVE:
            raise BrowserWorkflowError("only active browser sessions can synchronize tabs")
        candidate = session.model_copy(update={"tabs": request.tabs, "active_tab_id": request.active_tab_id})
        BrowserSession.model_validate(candidate.model_dump())
        updated, _ = self.store.update_with_operation_event(
            BrowserSession,
            session.id,
            {"tabs": request.tabs, "active_tab_id": request.active_tab_id, "device_owner": request.device_owner, "last_seen_at": utc_now()},
            expected_revision=request.expected_revision,
            operation_id=session.id,
            operation_kind="browser_session",
            engagement_id=session.engagement_id,
            event_type="browser_session.tabs_synced",
            event_payload={"tab_count": len(request.tabs), "active_tab_id": request.active_tab_id},
            actor_id=request.device_owner,
        )
        return updated

    def update_capture_settings(self, session_id: str, request: BrowserCaptureSettingsRequest, actor_id: str) -> BrowserSession:
        session = self.store.get(BrowserSession, session_id)
        candidate = session.model_copy(update={
            "capture_mode": request.capture_mode,
            "proxy_enabled": request.proxy_enabled,
            "interception_enabled": request.interception_enabled,
            "upstream_proxy_enabled": request.upstream_proxy_enabled,
            "upstream_proxy_url": request.upstream_proxy_url,
        })
        BrowserSession.model_validate(candidate.model_dump())
        updated, _ = self.store.update_with_operation_event(
            BrowserSession,
            session.id,
            {
                "capture_mode": request.capture_mode,
                "proxy_enabled": request.proxy_enabled,
                "interception_enabled": request.interception_enabled,
                "upstream_proxy_enabled": request.upstream_proxy_enabled,
                "upstream_proxy_url": request.upstream_proxy_url,
            },
            expected_revision=request.expected_revision,
            operation_id=session.id,
            operation_kind="browser_session",
            engagement_id=session.engagement_id,
            event_type="browser_session.capture_settings_changed",
            event_payload={
                "capture_mode": request.capture_mode.value,
                "proxy_enabled": request.proxy_enabled,
                "interception_enabled": request.interception_enabled,
                "upstream_proxy_enabled": request.upstream_proxy_enabled,
            },
            actor_id=actor_id,
        )
        return updated

    def record_traffic(self, session_id: str, request: BrowserTrafficRecordRequest, actor_id: str) -> BrowserTrafficExchange:
        session = self.store.get(BrowserSession, session_id)
        if session.status != BrowserSessionStatus.ACTIVE:
            raise BrowserWorkflowError("traffic can be recorded only for an active browser session")
        if request.tab_id not in {tab.id for tab in session.tabs}:
            raise BrowserWorkflowError("traffic tab does not belong to the browser session")
        scope = self._scope(session.engagement_id)
        self._require_in_scope(scope, request.url, "browser.capture", RiskClass.PASSIVE)
        if session.capture_mode != BrowserCaptureMode.BODIES and (request.request_body_artifact_id or request.response_body_artifact_id):
            raise BrowserWorkflowError("body artifacts require explicit body capture mode")
        exchange = BrowserTrafficExchange(
            engagement_id=session.engagement_id,
            session_id=session.id,
            tab_id=request.tab_id,
            identity_id=session.identity_id,
            scope_state="in_scope",
            scope_policy_id=scope.id,
            scope_policy_revision=scope.revision,
            request_headers=redact_browser_headers(request.request_headers),
            response_headers=redact_browser_headers(request.response_headers),
            **request.model_dump(
                exclude={"tab_id", "request_headers", "response_headers"}
            ),
        )
        return self._create(exchange, "browser_traffic.recorded", actor_id)

    def record_websocket_frame(
        self,
        session_id: str,
        request: BrowserWebSocketFrameRecordRequest,
        actor_id: str,
    ) -> BrowserWebSocketFrame:
        session = self.store.get(BrowserSession, session_id)
        exchange = self._owned(
            BrowserTrafficExchange, request.exchange_id, session.engagement_id
        )
        if exchange.session_id != session.id or exchange.protocol != "websocket":
            raise BrowserWorkflowError(
                "WebSocket frame exchange does not belong to this browser session"
            )
        # Message content is body data. Metadata/header modes retain only its
        # digest, length, direction, and opcode.
        preview = (
            request.payload_preview
            if session.capture_mode == BrowserCaptureMode.BODIES
            else ""
        )
        frame = BrowserWebSocketFrame(
            engagement_id=session.engagement_id,
            session_id=session.id,
            exchange_id=exchange.id,
            direction=request.direction,
            opcode=request.opcode,
            payload_preview=preview,
            payload_sha256=request.payload_sha256,
            payload_bytes=request.payload_bytes,
            truncated=request.truncated or preview != request.payload_preview,
        )
        return self._create(frame, "browser_websocket_frame.recorded", actor_id)

    def propose_action(self, session_id: str, request: BrowserActionProposalRequest) -> BrowserAction:
        session = self.store.get(BrowserSession, session_id)
        if session.status != BrowserSessionStatus.ACTIVE:
            raise BrowserWorkflowError("actions require an active browser session")
        if request.tab_id not in {tab.id for tab in session.tabs}:
            raise BrowserWorkflowError("action tab does not belong to the browser session")
        scope = self._scope(session.engagement_id)
        self._require_in_scope(scope, request.page_url, f"browser.{request.kind.value}", RiskClass.PASSIVE)
        if request.kind not in {BrowserActionKind.NAVIGATE, BrowserActionKind.SCREENSHOT, BrowserActionKind.REPLAY} and not request.locator:
            raise BrowserWorkflowError("element actions require a semantic locator")
        if len(json.dumps(request.locator)) > 4_000 or len(json.dumps(request.arguments)) > 8_000:
            raise BrowserWorkflowError("browser action locator or arguments are too large")
        if request.kind == BrowserActionKind.FILL:
            if set(request.arguments) != {"non_secret_text"}:
                raise BrowserWorkflowError(
                    "fill actions require only an explicit non_secret_text argument"
                )
            text = request.arguments.get("non_secret_text")
            if not isinstance(text, str) or len(text) > 4_000:
                raise BrowserWorkflowError("fill text must be a bounded string")
        if request.kind == BrowserActionKind.NAVIGATE:
            target = request.arguments.get("url")
            if not isinstance(target, str):
                raise BrowserWorkflowError("navigate actions require a URL argument")
            self._require_in_scope(scope, target, "browser.navigate", RiskClass.PASSIVE)
        if request.kind == BrowserActionKind.REPLAY:
            target = request.arguments.get("url")
            method = request.arguments.get("method")
            headers = request.arguments.get("headers", {})
            body = request.arguments.get("body", "")
            if not isinstance(target, str) or not isinstance(method, str):
                raise BrowserWorkflowError("replay actions require URL and method arguments")
            if method.upper() not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
                raise BrowserWorkflowError("replay method is not supported")
            if not isinstance(headers, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
                raise BrowserWorkflowError("replay headers must be a string map")
            if any(any(fragment in key.lower() for fragment in SENSITIVE_HEADER_FRAGMENTS) for key in headers):
                raise BrowserWorkflowError("replay headers cannot contain reusable secrets")
            if not isinstance(body, str) or len(body.encode("utf-8")) > 64 * 1024:
                raise BrowserWorkflowError("replay body must be a string no larger than 64 KiB")
            self._require_in_scope(scope, target, "browser.replay", RiskClass.ACTIVE_SCAN)
        action = BrowserAction(
            engagement_id=session.engagement_id,
            session_id=session.id,
            identity_id=session.identity_id,
            scope_policy_id=scope.id,
            scope_policy_revision=scope.revision,
            action_sha256=_action_digest(request, session, scope),
            expires_at=utc_now() + ACTION_LIFETIME,
            **request.model_dump(),
        )
        return self._create(action, "browser_action.proposed", request.proposed_by)

    def decide_action(self, action_id: str, request: BrowserActionDecisionRequest) -> BrowserAction:
        action = self.store.get(BrowserAction, action_id)
        if action.status != BrowserActionStatus.PROPOSED:
            raise BrowserWorkflowError("only proposed browser actions can be decided")
        if utc_now() >= action.expires_at:
            return self._transition_action(action, request.expected_revision, BrowserActionStatus.EXPIRED, "browser_action.expired", request.operator_id)
        status = BrowserActionStatus.APPROVED if request.decision == "approve" else BrowserActionStatus.REJECTED
        changes: dict[str, Any] = {"status": status}
        if status == BrowserActionStatus.APPROVED:
            changes.update({"approved_by": request.operator_id, "approved_at": utc_now()})
        updated, _ = self.store.update_with_operation_event(
            BrowserAction, action.id, changes,
            expected_revision=request.expected_revision,
            operation_id=action.id, operation_kind="browser_action", engagement_id=action.engagement_id,
            event_type=f"browser_action.{status.value}", event_payload={"action_sha256": action.action_sha256}, actor_id=request.operator_id,
        )
        return updated

    def start_action(self, action_id: str, request: BrowserActionExecutionRequest) -> BrowserAction:
        action = self.store.get(BrowserAction, action_id)
        if action.status != BrowserActionStatus.APPROVED:
            raise BrowserWorkflowError("only approved browser actions can execute")
        if utc_now() >= action.expires_at:
            return self._transition_action(action, request.expected_revision, BrowserActionStatus.EXPIRED, "browser_action.expired", request.device_id)
        scope = self._scope(action.engagement_id)
        if scope.id != action.scope_policy_id or scope.revision != action.scope_policy_revision:
            raise BrowserWorkflowError("Project scope changed after approval; propose the action again")
        self._require_in_scope(scope, action.page_url, f"browser.{action.kind.value}", RiskClass.PASSIVE)
        if action.kind == BrowserActionKind.NAVIGATE:
            target = action.arguments.get("url")
            if not isinstance(target, str):
                raise BrowserWorkflowError("approved navigate action lost its URL")
            self._require_in_scope(scope, target, "browser.navigate", RiskClass.PASSIVE)
        if action.kind == BrowserActionKind.REPLAY:
            target = action.arguments.get("url")
            if not isinstance(target, str):
                raise BrowserWorkflowError("approved replay action lost its URL")
            self._require_in_scope(scope, target, "browser.replay", RiskClass.ACTIVE_SCAN)
        return self._transition_action(action, request.expected_revision, BrowserActionStatus.EXECUTING, "browser_action.executing", request.device_id)

    def finish_action(self, action_id: str, request: BrowserActionResultRequest) -> BrowserAction:
        action = self.store.get(BrowserAction, action_id)
        if action.status != BrowserActionStatus.EXECUTING:
            raise BrowserWorkflowError("only executing browser actions can finish")
        for evidence_id in request.evidence_ids:
            self._owned(Evidence, evidence_id, action.engagement_id)
        status = BrowserActionStatus.COMPLETE if request.state == "complete" else BrowserActionStatus.FAILED
        changes = {
            "status": status,
            "completed_at": utc_now(),
            "result": request.result,
            "evidence_ids": request.evidence_ids,
            "error": request.error,
        }
        updated, _ = self.store.update_with_operation_event(
            BrowserAction, action.id, changes,
            expected_revision=request.expected_revision,
            operation_id=action.id, operation_kind="browser_action", engagement_id=action.engagement_id,
            event_type=f"browser_action.{status.value}", event_payload={"evidence_ids": request.evidence_ids}, actor_id=request.device_id,
        )
        return updated

    def create_handoff(self, session_id: str, request: BrowserHandoffCreateRequest) -> BrowserHandoff:
        session = self.store.get(BrowserSession, session_id)
        if session.status != BrowserSessionStatus.ACTIVE:
            raise BrowserWorkflowError("handoffs require an active browser session")
        if request.command == "navigate" and request.url:
            self._require_in_scope(self._scope(session.engagement_id), request.url, "browser.navigate", RiskClass.PASSIVE)
        if request.command == "focus_tab" and request.tab_id not in {tab.id for tab in session.tabs}:
            raise BrowserWorkflowError("handoff tab does not belong to the browser session")
        handoff = BrowserHandoff(
            engagement_id=session.engagement_id,
            session_id=session.id,
            expires_at=utc_now() + HANDOFF_LIFETIME,
            **request.model_dump(),
        )
        return self._create(handoff, "browser_handoff.queued", request.requested_by_device_id)

    def claim_handoff(self, handoff_id: str, request: BrowserHandoffClaimRequest) -> BrowserHandoff:
        handoff = self.store.get(BrowserHandoff, handoff_id)
        if handoff.status != BrowserHandoffStatus.QUEUED:
            raise BrowserWorkflowError("only queued browser handoffs can be claimed")
        if utc_now() >= handoff.expires_at:
            return self._transition_handoff(handoff, request.expected_revision, BrowserHandoffStatus.EXPIRED, request.desktop_device_id)
        updated, _ = self.store.update_with_operation_event(
            BrowserHandoff, handoff.id,
            {"status": BrowserHandoffStatus.CLAIMED, "claimed_by_device_id": request.desktop_device_id, "claimed_at": utc_now()},
            expected_revision=request.expected_revision,
            operation_id=handoff.id, operation_kind="browser_handoff", engagement_id=handoff.engagement_id,
            event_type="browser_handoff.claimed", event_payload={}, actor_id=request.desktop_device_id,
        )
        return updated

    def finish_handoff(self, handoff_id: str, request: BrowserHandoffResultRequest) -> BrowserHandoff:
        handoff = self.store.get(BrowserHandoff, handoff_id)
        if handoff.status != BrowserHandoffStatus.CLAIMED or handoff.claimed_by_device_id != request.desktop_device_id:
            raise BrowserWorkflowError("handoff is not claimed by this desktop")
        status = BrowserHandoffStatus.COMPLETE if request.state == "complete" else BrowserHandoffStatus.FAILED
        updated, _ = self.store.update_with_operation_event(
            BrowserHandoff, handoff.id,
            {"status": status, "completed_at": utc_now(), "error": request.error},
            expected_revision=request.expected_revision,
            operation_id=handoff.id, operation_kind="browser_handoff", engagement_id=handoff.engagement_id,
            event_type=f"browser_handoff.{status.value}", event_payload={}, actor_id=request.desktop_device_id,
        )
        return updated

    def _scope(self, engagement_id: str) -> ScopePolicy:
        engagement = self.store.get(Engagement, engagement_id)
        if not engagement.scope_policy_id:
            raise BrowserWorkflowError("Project scope is not configured")
        scope = self.store.get(ScopePolicy, engagement.scope_policy_id)
        if scope.engagement_id != engagement_id:
            raise BrowserWorkflowError("Project scope ownership is invalid")
        return scope

    def _require_in_scope(self, scope: ScopePolicy, target: str, action: str, risk: RiskClass) -> None:
        decision = self.policy.evaluate(scope, PolicyRequest(tool_name="security_browser", risk_class=risk, target=target, action=action))
        if decision.effect == PolicyEffect.DENY:
            raise BrowserWorkflowError(decision.reason)

    def _owned(self, model: type[Any], entity_id: str, engagement_id: str) -> Any:
        entity = self.store.get(model, entity_id)
        if getattr(entity, "engagement_id", None) != engagement_id:
            raise BrowserWorkflowError("browser entity belongs to another Project")
        return entity

    def _create(self, entity: Any, event_type: str, actor_id: str) -> Any:
        created, _ = self.store.create_with_operation_event(
            entity,
            operation_id=entity.id,
            operation_kind=entity.entity_kind,
            engagement_id=entity.engagement_id,
            event_type=event_type,
            event_payload={"entity_id": entity.id},
            actor_id=actor_id,
            idempotency_key=f"create:{entity.id}",
        )
        return created

    def _transition_action(self, action: BrowserAction, revision: int, status: BrowserActionStatus, event_type: str, actor_id: str) -> BrowserAction:
        updated, _ = self.store.update_with_operation_event(
            BrowserAction, action.id, {"status": status}, expected_revision=revision,
            operation_id=action.id, operation_kind="browser_action", engagement_id=action.engagement_id,
            event_type=event_type, event_payload={"action_sha256": action.action_sha256}, actor_id=actor_id,
        )
        return updated

    def _transition_handoff(self, handoff: BrowserHandoff, revision: int, status: BrowserHandoffStatus, actor_id: str) -> BrowserHandoff:
        updated, _ = self.store.update_with_operation_event(
            BrowserHandoff, handoff.id, {"status": status}, expected_revision=revision,
            operation_id=handoff.id, operation_kind="browser_handoff", engagement_id=handoff.engagement_id,
            event_type=f"browser_handoff.{status.value}", event_payload={}, actor_id=actor_id,
        )
        return updated


__all__ = [name for name in globals() if name.startswith("Browser") or name == "redact_browser_headers"]
