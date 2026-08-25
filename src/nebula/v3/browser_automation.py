"""Run-scoped browser and proxy automation contracts.

The model never talks directly to a native webview.  It creates durable commands
under a short-lived lease; a paired desktop worker claims those commands and
returns bounded, redacted receipts.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import secrets
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from .browser_security import BrowserWorkflowError
from .domain import (
    AgentRun,
    BrowserAutomationLease,
    BrowserAutomationLeaseStatus,
    BrowserCommand,
    BrowserCommandStatus,
    BrowserIdentity,
    BrowserProxyRule,
    BrowserSession,
    BrowserSessionStatus,
    Engagement,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from .policy import PolicyEffect, PolicyEngine, PolicyRequest
from .storage import ConflictError, NebulaStore


LEASE_MAX_DURATION_SECONDS = 3_600
COMMAND_CLAIM_SECONDS = 30
COMMAND_MAX_ARGUMENT_BYTES = 32_000
COMMAND_MAX_RESULT_BYTES = 64_000
RULE_MAX_DURATION_SECONDS = 3_600

DEFAULT_AUTONOMOUS_RISKS = (
    RiskClass.PASSIVE,
    RiskClass.ACTIVE_SCAN,
    RiskClass.CREDENTIAL_USE,
)
HIGH_RISK_CLASSES = frozenset(
    {
        RiskClass.EXPLOITATION,
        RiskClass.PERSISTENCE,
        RiskClass.DESTRUCTIVE,
        RiskClass.SCOPE_CHANGE,
    }
)

COMMAND_RISK: dict[str, RiskClass] = {
    "browser.observe": RiskClass.PASSIVE,
    "browser.navigate": RiskClass.ACTIVE_SCAN,
    "browser.control": RiskClass.ACTIVE_SCAN,
    "browser.interact": RiskClass.ACTIVE_SCAN,
    "browser.extract": RiskClass.PASSIVE,
    "browser.replay": RiskClass.ACTIVE_SCAN,
    "browser.capture_evidence": RiskClass.PASSIVE,
    "proxy.observe": RiskClass.PASSIVE,
    "proxy.configure": RiskClass.ACTIVE_SCAN,
    "proxy.add_rule": RiskClass.ACTIVE_SCAN,
    "proxy.remove_rule": RiskClass.ACTIVE_SCAN,
    "proxy.replay": RiskClass.ACTIVE_SCAN,
    "proxy.emergency_stop": RiskClass.ACTIVE_SCAN,
}
CREDENTIAL_CAPABLE_COMMANDS = frozenset(
    {"browser.interact", "browser.replay", "proxy.replay"}
)
REQUEST_METERED_COMMANDS = frozenset(
    {"browser.navigate", "browser.interact", "browser.replay", "proxy.replay"}
)

SECRET_ARGUMENT_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
)

SECRET_ARGUMENT_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "cookie",
        "authorization",
        "proxy_authorization",
        "api_key",
        "apikey",
        "set_cookie",
    }
)


class BrowserAutomationRequestError(BrowserWorkflowError):
    """A browser automation request is invalid or outside its lease."""


class BrowserAutonomyRequestModel(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    targets: list[str] = Field(min_length=1, max_length=256)
    allowed_risk_classes: list[RiskClass] = Field(
        default_factory=lambda: list(DEFAULT_AUTONOMOUS_RISKS), max_length=16
    )
    credential_refs: list[str] = Field(default_factory=list, max_length=64)
    duration_seconds: int = Field(default=1_800, ge=1, le=LEASE_MAX_DURATION_SECONDS)
    max_commands: int = Field(default=100, ge=1, le=100_000)
    max_requests: int = Field(default=1_000, ge=1, le=1_000_000)
    max_body_bytes: int = Field(default=1_048_576, ge=0, le=8_388_608)

    @field_validator("credential_refs")
    @classmethod
    def credential_refs_are_opaque(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 200 or any(char.isspace() for char in value):
                raise ValueError(
                    "credential references must be opaque non-space identifiers"
                )
        return list(dict.fromkeys(values))


class BrowserCommandCreateRequest(BaseModel):
    tab_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,80}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_page_url: str | None = Field(default=None, max_length=16_384)
    expected_tab_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def kind_is_known(self) -> "BrowserCommandCreateRequest":
        if self.kind not in COMMAND_RISK:
            raise ValueError(f"unsupported browser automation command: {self.kind}")
        if (
            len(json.dumps(self.arguments, ensure_ascii=False))
            > COMMAND_MAX_ARGUMENT_BYTES
        ):
            raise ValueError("browser automation arguments are too large")
        return self


class BrowserCommandClaimRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)


class BrowserCommandResultRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    claim_token: str = Field(min_length=1, max_length=200)
    state: Literal["complete", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    error: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def result_is_bounded(self) -> "BrowserCommandResultRequest":
        if len(json.dumps(self.result, ensure_ascii=False)) > COMMAND_MAX_RESULT_BYTES:
            raise ValueError("browser automation result is too large")
        return self


class BrowserProxyRuleRequest(BaseModel):
    match: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=10_000)
    duration_seconds: int = Field(default=1_800, ge=1, le=RULE_MAX_DURATION_SECONDS)

    @model_validator(mode="after")
    def rule_is_declarative(self) -> "BrowserProxyRuleRequest":
        allowed_actions = {
            "pass",
            "block",
            "redirect",
            "delay",
            "set_header",
            "remove_header",
            "replace_body",
            "ws_drop",
            "ws_replace",
        }
        action_name = self.action.get("type")
        if action_name not in allowed_actions:
            raise ValueError("proxy rules must use a supported declarative action")
        encoded = json.dumps({"match": self.match, "action": self.action})
        if len(encoded) > 32_000:
            raise ValueError("proxy rule payload is too large")
        if "script" in encoded.lower() or "javascript" in encoded.lower():
            raise ValueError("proxy rules cannot contain executable scripts")
        return self


class BrowserAutomationStatus(BaseModel):
    leases: list[BrowserAutomationLease]
    commands: list[BrowserCommand]
    rules: list[BrowserProxyRule]


def _safe_json(value: Any, *, limit: int) -> dict[str, Any]:
    """Return a bounded result without reusable secret fields."""

    def clean(item: Any, key: str = "") -> Any:
        normalized = key.lower().replace("-", "_")
        if normalized != "non_secret_text" and (
            normalized in SECRET_ARGUMENT_KEYS
            or any(fragment in normalized for fragment in SECRET_ARGUMENT_FRAGMENTS)
        ):
            return "<redacted>"
        if isinstance(item, dict):
            return {str(k)[:200]: clean(v, str(k)) for k, v in list(item.items())[:200]}
        if isinstance(item, list):
            return [clean(v) for v in item[:200]]
        if isinstance(item, str):
            return item[:16_000]
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        return str(item)[:2_000]

    cleaned = clean(value)
    if not isinstance(cleaned, dict):
        cleaned = {"value": cleaned}
    if len(json.dumps(cleaned, ensure_ascii=False)) > limit:
        return {
            "truncated": True,
            "sha256": hashlib.sha256(json.dumps(cleaned).encode()).hexdigest(),
        }
    return cleaned


class BrowserAutomationService:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.policy = PolicyEngine()

    def validate_autonomy(
        self, engagement_id: str, request: BrowserAutonomyRequestModel
    ) -> BrowserSession:
        session = self.store.get(BrowserSession, request.session_id)
        if session.engagement_id != engagement_id:
            raise BrowserAutomationRequestError(
                "browser session belongs to another Project"
            )
        if session.status != BrowserSessionStatus.ACTIVE:
            raise BrowserAutomationRequestError(
                "autonomous browser tests require an active session"
            )
        if session.identity_id not in {
            identity.id
            for identity in self.store.list_entities(
                BrowserIdentity, engagement_id=engagement_id, limit=1_000
            )
        }:
            raise BrowserAutomationRequestError(
                "browser session identity is unavailable"
            )
        scope = self._scope(engagement_id)
        for target in request.targets:
            self._require_target(
                scope, target, RiskClass.PASSIVE, "browser.autonomy.target"
            )
        requested = set(request.allowed_risk_classes)
        if not requested:
            raise BrowserAutomationRequestError(
                "autonomous runs require at least one risk class"
            )
        default_allowed = set(DEFAULT_AUTONOMOUS_RISKS)
        for risk in requested - default_allowed:
            if risk not in HIGH_RISK_CLASSES or not self._grant_covers(
                scope, risk, request.targets
            ):
                raise BrowserAutomationRequestError(
                    f"risk class {risk.value} requires an explicit active scope grant"
                )
        return session

    def create_lease(
        self,
        run_id: str,
        engagement_id: str,
        request: BrowserAutonomyRequestModel,
        actor_id: str,
    ) -> BrowserAutomationLease:
        run = self.store.get(AgentRun, run_id)
        if run.engagement_id != engagement_id:
            raise BrowserAutomationRequestError("mission belongs to another Project")
        session = self.validate_autonomy(engagement_id, request)
        scope = self._scope(engagement_id)
        for existing in self.store.list_entities(
            BrowserAutomationLease,
            engagement_id=engagement_id,
            automation_session_id=session.id,
            automation_status=BrowserAutomationLeaseStatus.ACTIVE.value,
            limit=1_000,
        ):
            if existing.run_id != run_id and existing.expires_at > utc_now():
                raise BrowserAutomationRequestError(
                    "the browser session already has an active autonomous lease"
                )
        now = utc_now()
        lease = BrowserAutomationLease(
            engagement_id=engagement_id,
            run_id=run_id,
            session_id=session.id,
            identity_id=session.identity_id,
            scope_policy_id=scope.id,
            scope_policy_revision=scope.revision,
            target_urls=request.targets,
            allowed_risk_classes=list(dict.fromkeys(request.allowed_risk_classes)),
            credential_refs=request.credential_refs,
            max_commands=request.max_commands,
            max_requests=request.max_requests,
            max_body_bytes=request.max_body_bytes,
            expires_at=now + timedelta(seconds=request.duration_seconds),
            metadata={"created_by": actor_id, "autonomy_mode": "run_scoped"},
        )
        return self._create(lease, "browser_automation_lease.created", actor_id)

    def status(
        self, engagement_id: str, run_id: str | None = None
    ) -> BrowserAutomationStatus:
        leases = self.store.list_entities(
            BrowserAutomationLease,
            engagement_id=engagement_id,
            automation_run_id=run_id,
            limit=1_000,
        )
        leases = [self._reconcile_lease(item) for item in leases]
        lease_ids = {item.id for item in leases}
        commands = [
            item
            for item in self.store.list_entities(
                BrowserCommand,
                engagement_id=engagement_id,
                automation_run_id=run_id,
                limit=1_000,
            )
            if item.lease_id in lease_ids
        ]
        rules = [
            self._reconcile_rule(item)
            for item in self.store.list_entities(
                BrowserProxyRule,
                engagement_id=engagement_id,
                automation_run_id=run_id,
                limit=1_000,
            )
            if item.lease_id in lease_ids
        ]
        return BrowserAutomationStatus(leases=leases, commands=commands, rules=rules)

    def active_lease_for_run(self, run_id: str) -> BrowserAutomationLease:
        run = self.store.get(AgentRun, run_id)
        leases = [
            item
            for item in self.store.list_entities(
                BrowserAutomationLease,
                engagement_id=run.engagement_id,
                automation_run_id=run_id,
                limit=1_000,
            )
        ]
        if not leases:
            raise BrowserAutomationRequestError(
                "the mission has no browser automation lease"
            )
        return self._active_lease(leases[-1].id)

    def enqueue_command(
        self,
        lease_id: str,
        request: BrowserCommandCreateRequest,
        actor_id: str,
    ) -> BrowserCommand:
        lease = self._active_lease(lease_id)
        if request.idempotency_key:
            existing = self._commands_for_lease(lease)
            for item in existing:
                if item.idempotency_key == request.idempotency_key:
                    return item
        risk = (
            RiskClass.CREDENTIAL_USE
            if request.kind in CREDENTIAL_CAPABLE_COMMANDS
            and request.arguments.get("credential_ref") is not None
            else COMMAND_RISK[request.kind]
        )
        if risk not in lease.allowed_risk_classes:
            raise BrowserAutomationRequestError(
                f"{request.kind} is not authorized by the active browser lease"
            )
        self._reject_secret_values(request.arguments)
        target = request.expected_page_url or self._argument_target(request.arguments)
        if target:
            self._require_target(
                self._scope(lease.engagement_id), target, risk, request.kind
            )
            if not self._target_in_lease(target, lease.target_urls):
                raise BrowserAutomationRequestError(
                    "browser command target is outside the lease target subset"
                )
        if request.arguments.get("credential_ref") is not None:
            credential_ref = request.arguments["credential_ref"]
            if credential_ref not in lease.credential_refs:
                raise BrowserAutomationRequestError(
                    "credential reference is not authorized by this lease"
                )
            if (
                request.kind != "browser.interact"
                or request.arguments.get("operation") != "fill"
            ):
                raise BrowserAutomationRequestError(
                    "credential_ref is supported only for bounded browser.interact fill actions"
                )
        if lease.commands_used >= lease.max_commands:
            raise BrowserAutomationRequestError(
                "browser automation command budget is exhausted"
            )
        request_cost = 1 if request.kind in REQUEST_METERED_COMMANDS else 0
        if lease.requests_used + request_cost > lease.max_requests:
            raise BrowserAutomationRequestError(
                "browser automation request budget is exhausted"
            )
        body_values = [
            value
            for value in (
                request.arguments.get("body"),
                request.arguments.get("non_secret_text"),
                request.arguments.get("replacement"),
            )
            if isinstance(value, str)
        ]
        if any(
            len(value.encode("utf-8")) > lease.max_body_bytes for value in body_values
        ):
            raise BrowserAutomationRequestError(
                "browser automation body budget is exhausted"
            )
        updated = self.store.update(
            BrowserAutomationLease,
            lease.id,
            {
                "commands_used": lease.commands_used + 1,
                "requests_used": lease.requests_used + request_cost,
                "last_heartbeat_at": utc_now(),
            },
            expected_revision=lease.revision,
        )
        now = utc_now()
        command = BrowserCommand(
            engagement_id=updated.engagement_id,
            run_id=updated.run_id,
            lease_id=updated.id,
            session_id=updated.session_id,
            tab_id=request.tab_id,
            kind=request.kind,
            arguments=_safe_json(request.arguments, limit=COMMAND_MAX_ARGUMENT_BYTES),
            expected_page_url=request.expected_page_url,
            expected_tab_revision=request.expected_tab_revision,
            expires_at=min(updated.expires_at, now + timedelta(minutes=5)),
            idempotency_key=request.idempotency_key,
        )
        return self._create(command, "browser_command.queued", actor_id)

    def claim_command(
        self, command_id: str, request: BrowserCommandClaimRequest
    ) -> BrowserCommand:
        command = self.store.get(BrowserCommand, command_id)
        lease = None
        if command.status in {
            BrowserCommandStatus.QUEUED,
            BrowserCommandStatus.CLAIMED,
        }:
            lease = self._active_lease(command.lease_id)
            self._require_paired_desktop(lease, request.device_id)
        if (
            command.status == BrowserCommandStatus.CLAIMED
            and command.claim_expires_at is not None
            and command.claim_expires_at <= utc_now()
        ):
            command = self._update_command(
                command,
                {
                    "status": BrowserCommandStatus.QUEUED,
                    "claimed_by_device_id": None,
                    "claim_token": None,
                    "claimed_at": None,
                    "claim_expires_at": None,
                    "error": "previous desktop claim expired; command requeued",
                },
                "browser_command.requeued",
                request.device_id,
            )
        if command.status != BrowserCommandStatus.QUEUED:
            return command
        assert lease is not None
        if command.expires_at <= utc_now():
            return self._update_command(
                command,
                {
                    "status": BrowserCommandStatus.EXPIRED,
                    "error": "command expired before claim",
                },
                "browser_command.expired",
                request.device_id,
            )
        token = secrets.token_urlsafe(24)
        try:
            return self._update_command(
                command,
                {
                    "status": BrowserCommandStatus.CLAIMED,
                    "claimed_by_device_id": request.device_id,
                    "claim_token": token,
                    "claimed_at": utc_now(),
                    "claim_expires_at": utc_now()
                    + timedelta(seconds=COMMAND_CLAIM_SECONDS),
                },
                "browser_command.claimed",
                request.device_id,
            )
        except ConflictError:  # diagnostic-expected: reload the winning durable claim
            # Another paired worker won the optimistic claim race. Returning
            # its durable state makes retries idempotent without issuing a
            # second native action.
            return self.store.get(BrowserCommand, command.id)

    def finish_command(
        self, command_id: str, request: BrowserCommandResultRequest
    ) -> BrowserCommand:
        command = self.store.get(BrowserCommand, command_id)
        if command.status == BrowserCommandStatus.COMPLETE:
            return command
        if command.status != BrowserCommandStatus.CLAIMED:
            raise BrowserAutomationRequestError(
                "only claimed browser commands can finish"
            )
        lease = self._active_lease(command.lease_id)
        self._require_paired_desktop(lease, request.device_id)
        if (
            command.claimed_by_device_id != request.device_id
            or command.claim_token != request.claim_token
        ):
            raise BrowserAutomationRequestError(
                "browser command claim does not match the paired device"
            )
        if command.claim_expires_at and command.claim_expires_at <= utc_now():
            raise BrowserAutomationRequestError(
                "browser command claim expired; retry the command"
            )
        status = (
            BrowserCommandStatus.COMPLETE
            if request.state == "complete"
            else BrowserCommandStatus.FAILED
        )
        return self._update_command(
            command,
            {
                "status": status,
                "result": _safe_json(request.result, limit=COMMAND_MAX_RESULT_BYTES),
                "evidence_ids": request.evidence_ids,
                "error": request.error,
            },
            f"browser_command.{status.value}",
            request.device_id,
        )

    def revoke_run(self, run_id: str, reason: str, actor_id: str) -> int:
        run = self.store.get(AgentRun, run_id)
        count = 0
        lease_ids: set[str] = set()
        session_ids: set[str] = set()
        for lease in self.store.list_entities(
            BrowserAutomationLease,
            engagement_id=run.engagement_id,
            automation_run_id=run_id,
            limit=1_000,
        ):
            lease_ids.add(lease.id)
            session_ids.add(lease.session_id)
            if lease.status not in {
                BrowserAutomationLeaseStatus.REVOKED,
                BrowserAutomationLeaseStatus.EXPIRED,
            }:
                self._update_lease(
                    lease,
                    {
                        "status": BrowserAutomationLeaseStatus.REVOKED,
                        "revoked_at": utc_now(),
                        "stop_reason": reason,
                    },
                    "browser_automation_lease.revoked",
                    actor_id,
                )
                count += 1
            for command in self._commands_for_lease(lease):
                if command.status in {
                    BrowserCommandStatus.QUEUED,
                    BrowserCommandStatus.CLAIMED,
                }:
                    self._update_command(
                        command,
                        {"status": BrowserCommandStatus.CANCELLED, "error": reason},
                        "browser_command.cancelled",
                        actor_id,
                    )
        for rule in self.store.list_entities(
            BrowserProxyRule,
            engagement_id=run.engagement_id,
            automation_run_id=run_id,
            limit=1_000,
        ):
            if rule.lease_id in lease_ids and rule.enabled:
                self._disable_rule(rule, reason, actor_id)
        for session_id in session_ids:
            self._disable_session_proxy(session_id, reason, actor_id)
        return count

    def _disable_session_proxy(
        self, session_id: str, reason: str, actor_id: str
    ) -> None:
        """Make an emergency stop durable, not just a native worker signal."""

        session = self.store.get(BrowserSession, session_id)
        if not session.proxy_enabled and not session.proxy_trust_acknowledged:
            return
        try:
            self.store.update_with_operation_event(
                BrowserSession,
                session.id,
                {
                    "proxy_enabled": False,
                    "proxy_trust_acknowledged": False,
                },
                expected_revision=session.revision,
                operation_id=session.id,
                operation_kind="browser_session",
                engagement_id=session.engagement_id,
                event_type="browser_session.proxy_disabled_by_emergency_stop",
                event_payload={"reason": reason},
                actor_id=actor_id,
            )
        except (
            ConflictError
        ):  # diagnostic-expected: retry once against authoritative state
            # A concurrent tab sync or operator change wins the revision race;
            # reload and fail closed if the proxy is still durable-enabled.
            current = self.store.get(BrowserSession, session.id)
            if current.proxy_enabled or current.proxy_trust_acknowledged:
                self.store.update_with_operation_event(
                    BrowserSession,
                    current.id,
                    {
                        "proxy_enabled": False,
                        "proxy_trust_acknowledged": False,
                    },
                    expected_revision=current.revision,
                    operation_id=current.id,
                    operation_kind="browser_session",
                    engagement_id=current.engagement_id,
                    event_type="browser_session.proxy_disabled_by_emergency_stop",
                    event_payload={"reason": reason},
                    actor_id=actor_id,
                )

    def invalidate_scope_revision(
        self, engagement_id: str, revision: int, actor_id: str
    ) -> int:
        """Revoke every active browser lease pinned to an older scope revision."""

        count = 0
        for lease in self.store.list_entities(
            BrowserAutomationLease, engagement_id=engagement_id, limit=1_000
        ):
            if (
                lease.status != BrowserAutomationLeaseStatus.ACTIVE
                or lease.scope_policy_revision == revision
            ):
                continue
            try:
                self._update_lease(
                    lease,
                    {
                        "status": BrowserAutomationLeaseStatus.REVOKED,
                        "revoked_at": utc_now(),
                        "stop_reason": "Project scope policy revision changed",
                    },
                    "browser_automation_lease.scope_invalidated",
                    actor_id,
                )
                self._disable_lease_dependents(
                    lease, "Project scope policy revision changed"
                )
                count += 1
            except (
                ConflictError
            ):  # diagnostic-expected: a concurrent revocation already won
                # A concurrent status poll already revoked this lease. The
                # durable state remains fail-closed, so continue the sweep.
                continue
        return count

    def add_rule(
        self, lease_id: str, request: BrowserProxyRuleRequest, actor_id: str
    ) -> BrowserProxyRule:
        lease = self._active_lease(lease_id)
        if RiskClass.ACTIVE_SCAN not in lease.allowed_risk_classes:
            raise BrowserAutomationRequestError(
                "proxy mutation requires active-scan authorization"
            )
        self._reject_secret_values({"match": request.match, "action": request.action})
        replacement = request.action.get("body") or request.action.get("value")
        if (
            isinstance(replacement, str)
            and len(replacement.encode("utf-8")) > lease.max_body_bytes
        ):
            raise BrowserAutomationRequestError(
                "browser automation body budget is exhausted"
            )
        rule = BrowserProxyRule(
            engagement_id=lease.engagement_id,
            run_id=lease.run_id,
            lease_id=lease.id,
            session_id=lease.session_id,
            match=request.match,
            action=request.action,
            priority=request.priority,
            expires_at=min(
                lease.expires_at,
                utc_now() + timedelta(seconds=request.duration_seconds),
            ),
        )
        return self._create(rule, "browser_proxy_rule.created", actor_id)

    def remove_rule(self, rule_id: str, actor_id: str) -> BrowserProxyRule:
        rule = self.store.get(BrowserProxyRule, rule_id)
        self._active_lease(rule.lease_id)
        updated, _ = self.store.update_with_operation_event(
            BrowserProxyRule,
            rule.id,
            {
                "enabled": False,
                "disabled_at": utc_now(),
                "disabled_reason": "removed by automation",
            },
            expected_revision=rule.revision,
            operation_id=rule.id,
            operation_kind="browser_proxy_rule",
            engagement_id=rule.engagement_id,
            event_type="browser_proxy_rule.removed",
            event_payload={
                "rule_id": rule.id,
                "run_id": rule.run_id,
                "lease_id": rule.lease_id,
            },
            actor_id=actor_id,
        )
        return updated

    async def wait_for_command(
        self, command_id: str, timeout_seconds: float
    ) -> BrowserCommand:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            command = self.store.get(BrowserCommand, command_id)
            if command.status in {
                BrowserCommandStatus.COMPLETE,
                BrowserCommandStatus.FAILED,
                BrowserCommandStatus.EXPIRED,
                BrowserCommandStatus.CANCELLED,
            }:
                return command
            if asyncio.get_running_loop().time() >= deadline:
                return command
            await asyncio.sleep(0.15)

    def _scope(self, engagement_id: str) -> ScopePolicy:
        engagement = self.store.get(Engagement, engagement_id)
        if not engagement.scope_policy_id:
            raise BrowserAutomationRequestError("Project scope is not configured")
        scope = self.store.get(ScopePolicy, engagement.scope_policy_id)
        if scope.engagement_id != engagement_id:
            raise BrowserAutomationRequestError("Project scope ownership is invalid")
        return scope

    def _require_target(
        self, scope: ScopePolicy, target: str, risk: RiskClass, action: str
    ) -> None:
        parsed = urlsplit(target)
        try:
            parsed.port
        except ValueError as exc:
            raise BrowserAutomationRequestError(
                "browser target contains an invalid port"
            ) from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise BrowserAutomationRequestError(
                "browser targets must be credential-free HTTP(S) URLs"
            )
        # The native proxy is the authority for the resolved destination of a
        # live browser request. Core still evaluates the original risk class
        # against the frozen scope, while this explicit flag prevents Core from
        # inventing an unpinned DNS answer before the desktop worker observes it.
        decision = self.policy.evaluate(
            scope,
            PolicyRequest(
                tool_name="browser_automation",
                risk_class=risk,
                target=target,
                action=action,
                native_scope_authority=True,
            ),
        )
        if decision.effect == PolicyEffect.DENY:
            raise BrowserAutomationRequestError(decision.reason)

    @staticmethod
    def _grant_covers(scope: ScopePolicy, risk: RiskClass, targets: list[str]) -> bool:
        now = utc_now()
        for grant in scope.grants:
            if risk not in grant.risk_classes or grant.expires_at <= now:
                continue
            if not grant.targets or all(
                any(
                    BrowserAutomationService._grant_target_covers(target, allowed)
                    for allowed in grant.targets
                )
                for target in targets
            ):
                return True
        return False

    @staticmethod
    def _grant_target_covers(target: str, allowed: str) -> bool:
        """Match MissionGrant URL, domain, IP, and CIDR target forms."""

        target_parts = urlsplit(target)
        if "://" in allowed:
            return BrowserAutomationService._target_in_lease(target, [allowed])
        host = (target_parts.hostname or "").rstrip(".").lower()
        if not host:
            return False
        try:
            address = ipaddress.ip_address(host)
            return address in ipaddress.ip_network(allowed, strict=False)
        except (
            ValueError
        ):  # diagnostic-expected: non-IP grant targets continue as hostnames
            pass
        candidate = allowed.strip().rstrip(".").lower()
        if candidate.startswith("*."):
            suffix = candidate[2:]
            return host.endswith(f".{suffix}") and host != suffix
        return host == candidate

    @staticmethod
    def _target_in_lease(target: str, targets: list[str]) -> bool:
        parsed = urlsplit(target)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            return False
        try:
            target_port = parsed.port
        except ValueError:  # diagnostic-expected: malformed target ports fail closed
            return False
        origin = (
            parsed.scheme.lower(),
            parsed.hostname.lower(),
            target_port or (443 if parsed.scheme.lower() == "https" else 80),
        )
        target_path = parsed.path or "/"
        for allowed in targets:
            candidate = urlsplit(allowed)
            if (
                candidate.scheme.lower() not in {"http", "https"}
                or not candidate.hostname
                or candidate.username is not None
                or candidate.password is not None
                or candidate.fragment
            ):
                continue
            try:
                candidate_port = candidate.port
            except (
                ValueError
            ):  # diagnostic-expected: malformed allowed ports are skipped
                continue
            candidate_origin = (
                candidate.scheme.lower(),
                candidate.hostname.lower(),
                candidate_port or (443 if candidate.scheme.lower() == "https" else 80),
            )
            candidate_path = candidate.path or "/"
            candidate_path = candidate_path.rstrip("/") or "/"
            if origin == candidate_origin and (
                candidate_path == "/"
                or target_path == candidate_path
                or target_path.startswith(f"{candidate_path}/")
            ):
                return True
        return False

    @staticmethod
    def _argument_target(arguments: dict[str, Any]) -> str | None:
        for key in ("url", "target", "request_url"):
            value = arguments.get(key)
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _reject_secret_values(arguments: dict[str, Any]) -> None:
        def walk(value: Any) -> bool:
            if isinstance(value, dict):
                for raw_key, item in value.items():
                    key = str(raw_key).lower().replace("-", "_")
                    if key != "non_secret_text" and (
                        key in SECRET_ARGUMENT_KEYS
                        or any(
                            fragment in key for fragment in SECRET_ARGUMENT_FRAGMENTS
                        )
                    ):
                        return True
                    if walk(item):
                        return True
            elif isinstance(value, list):
                return any(walk(item) for item in value)
            return False

        if walk(arguments):
            raise BrowserAutomationRequestError(
                "browser automation accepts credential_ref, not reusable secret values"
            )

    def _active_lease(self, lease_id: str) -> BrowserAutomationLease:
        lease = self._reconcile_lease(self.store.get(BrowserAutomationLease, lease_id))
        if lease.status != BrowserAutomationLeaseStatus.ACTIVE:
            raise BrowserAutomationRequestError(
                f"browser automation lease is {lease.status.value}"
            )
        if lease.expires_at <= utc_now():
            raise BrowserAutomationRequestError("browser automation lease has expired")
        return lease

    def _reconcile_lease(self, lease: BrowserAutomationLease) -> BrowserAutomationLease:
        if lease.status == BrowserAutomationLeaseStatus.ACTIVE:
            if lease.expires_at <= utc_now():
                try:
                    updated = self._update_lease(
                        lease,
                        {
                            "status": BrowserAutomationLeaseStatus.EXPIRED,
                            "stop_reason": "lease expired",
                        },
                        "browser_automation_lease.expired",
                        "system",
                    )
                    self._disable_lease_dependents(lease, "lease expired")
                    return updated
                except (
                    ConflictError
                ):  # diagnostic-expected: reload the winning expiry update
                    return self.store.get(BrowserAutomationLease, lease.id)
            try:
                current_scope = self._scope(lease.engagement_id)
            except (
                BrowserAutomationRequestError
            ):  # diagnostic-expected: absent scope revokes the lease below
                current_scope = None
            if (
                current_scope is None
                or current_scope.revision != lease.scope_policy_revision
            ):
                try:
                    updated = self._update_lease(
                        lease,
                        {
                            "status": BrowserAutomationLeaseStatus.REVOKED,
                            "revoked_at": utc_now(),
                            "stop_reason": "Project scope policy revision changed",
                        },
                        "browser_automation_lease.scope_invalidated",
                        "system",
                    )
                    self._disable_lease_dependents(
                        lease, "Project scope policy revision changed"
                    )
                    return updated
                except (
                    ConflictError
                ):  # diagnostic-expected: reload the winning revocation update
                    return self.store.get(BrowserAutomationLease, lease.id)
        return lease

    def _commands_for_lease(
        self, lease: BrowserAutomationLease
    ) -> list[BrowserCommand]:
        return [
            item
            for item in self.store.list_entities(
                BrowserCommand,
                engagement_id=lease.engagement_id,
                automation_run_id=lease.run_id,
                automation_session_id=lease.session_id,
                limit=1_000,
            )
            if item.lease_id == lease.id
        ]

    def _require_paired_desktop(
        self, lease: BrowserAutomationLease, device_id: str
    ) -> None:
        session = self.store.get(BrowserSession, lease.session_id)
        if session.device_owner != device_id:
            raise BrowserAutomationRequestError(
                "browser commands can be claimed only by the paired desktop device"
            )

    def _disable_lease_dependents(
        self, lease: BrowserAutomationLease, reason: str
    ) -> None:
        for command in self._commands_for_lease(lease):
            if command.status in {
                BrowserCommandStatus.QUEUED,
                BrowserCommandStatus.CLAIMED,
            }:
                self._update_command(
                    command,
                    {"status": BrowserCommandStatus.CANCELLED, "error": reason},
                    "browser_command.cancelled",
                    "system",
                )
        for rule in self.store.list_entities(
            BrowserProxyRule,
            engagement_id=lease.engagement_id,
            automation_run_id=lease.run_id,
            automation_session_id=lease.session_id,
            limit=1_000,
        ):
            if rule.lease_id == lease.id and rule.enabled:
                self._disable_rule(rule, reason, "system")

    def _reconcile_rule(self, rule: BrowserProxyRule) -> BrowserProxyRule:
        if not rule.enabled or rule.expires_at > utc_now():
            return rule
        try:
            return self._disable_rule(rule, "proxy rule expired", "system")
        except (
            ConflictError
        ):  # diagnostic-expected: reload the winning rule-expiry update
            return self.store.get(BrowserProxyRule, rule.id)

    def _disable_rule(
        self, rule: BrowserProxyRule, reason: str, actor_id: str
    ) -> BrowserProxyRule:
        updated, _ = self.store.update_with_operation_event(
            BrowserProxyRule,
            rule.id,
            {"enabled": False, "disabled_at": utc_now(), "disabled_reason": reason},
            expected_revision=rule.revision,
            operation_id=rule.id,
            operation_kind="browser_proxy_rule",
            engagement_id=rule.engagement_id,
            event_type="browser_proxy_rule.disabled",
            event_payload={"rule_id": rule.id, "run_id": rule.run_id, "reason": reason},
            actor_id=actor_id,
        )
        return updated

    def _update_lease(
        self,
        lease: BrowserAutomationLease,
        changes: dict[str, Any],
        event_type: str,
        actor_id: str,
    ) -> BrowserAutomationLease:
        updated, _ = self.store.update_with_operation_event(
            BrowserAutomationLease,
            lease.id,
            changes,
            expected_revision=lease.revision,
            operation_id=lease.id,
            operation_kind="browser_automation_lease",
            engagement_id=lease.engagement_id,
            event_type=event_type,
            event_payload={"run_id": lease.run_id, "lease_id": lease.id},
            actor_id=actor_id,
        )
        return updated

    def _update_command(
        self,
        command: BrowserCommand,
        changes: dict[str, Any],
        event_type: str,
        actor_id: str,
    ) -> BrowserCommand:
        updated, _ = self.store.update_with_operation_event(
            BrowserCommand,
            command.id,
            changes,
            expected_revision=command.revision,
            operation_id=command.id,
            operation_kind="browser_command",
            engagement_id=command.engagement_id,
            event_type=event_type,
            event_payload={
                "run_id": command.run_id,
                "lease_id": command.lease_id,
                "command_id": command.id,
            },
            actor_id=actor_id,
        )
        return updated

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


__all__ = [
    "BrowserAutomationRequestError",
    "BrowserAutomationService",
    "BrowserAutomationStatus",
    "BrowserAutonomyRequestModel",
    "BrowserCommandClaimRequest",
    "BrowserCommandCreateRequest",
    "BrowserCommandResultRequest",
    "BrowserProxyRuleRequest",
    "COMMAND_RISK",
    "CREDENTIAL_CAPABLE_COMMANDS",
    "DEFAULT_AUTONOMOUS_RISKS",
]
