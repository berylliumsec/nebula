"""Deterministic scope and approval policy for executable capabilities.

Policy is evaluated before a tool request is persisted as runnable.  It does not
ask a language model whether an action is safe and it treats DNS answers as part
of the authorization decision so rebinding cannot silently widen engagement
scope.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import MissionGrant, RiskClass, ScopePolicy, utc_now


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyRequest(BaseModel):
    """The security-relevant portion of one proposed tool invocation."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    risk_class: RiskClass
    target: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    ports: list[int] = Field(default_factory=list)
    action: str | None = None
    resolved_ips: list[str] = Field(default_factory=list)
    credential_class: str | None = None
    writes_outside_workspace: bool = False
    cloud_transfer: bool = False

    @field_validator("resolved_ips")
    @classmethod
    def valid_addresses(cls, values: list[str]) -> list[str]:
        return sorted({str(ipaddress.ip_address(value)) for value in values})

    @field_validator("ports")
    @classmethod
    def valid_ports(cls, values: list[int]) -> list[int]:
        if any(not 1 <= value <= 65535 for value in values):
            raise ValueError("ports must be between 1 and 65535")
        return sorted(set(values))


class PolicyDecision(BaseModel):
    effect: PolicyEffect
    reason: str
    rule: str
    normalized_target: str | None = None
    matched_grant_index: int | None = None

    @property
    def allowed(self) -> bool:
        return self.effect == PolicyEffect.ALLOW


@dataclass(frozen=True)
class _Target:
    original: str
    host: str
    port: int | None
    url: str | None
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None


def _normalize_target(value: str, explicit_port: int | None) -> _Target:
    candidate = value.strip()
    if not candidate:
        raise ValueError("target cannot be empty")

    url: str | None = None
    host = candidate.rstrip(".").lower()
    port = explicit_port
    if "://" in candidate:
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("only absolute HTTP(S) targets are supported")
        host = parsed.hostname.rstrip(".").lower()
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise ValueError("target contains an invalid port") from exc
        port = explicit_port or parsed_port or (443 if parsed.scheme == "https" else 80)
        normalized_path = parsed.path or "/"
        url = f"{parsed.scheme.lower()}://{host}:{port}{normalized_path}"
    else:
        # Accept bracketed IPv6 and host:port without interpreting arbitrary
        # colon-containing strings as shell syntax.
        if candidate.startswith("[") and "]" in candidate:
            closing = candidate.index("]")
            host = candidate[1:closing].lower()
            remainder = candidate[closing + 1 :]
            if remainder:
                if not remainder.startswith(":") or not remainder[1:].isdigit():
                    raise ValueError("target contains an invalid port")
                port = explicit_port or int(remainder[1:])
        elif candidate.count(":") == 1:
            possible_host, possible_port = candidate.rsplit(":", 1)
            if possible_port.isdigit():
                host = possible_host.rstrip(".").lower()
                port = explicit_port or int(possible_port)

    if port is not None and not 1 <= port <= 65535:
        raise ValueError("target port must be between 1 and 65535")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    return _Target(candidate, host, port, url, address)


def _domain_allowed(host: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.rstrip(".").lower()
        if normalized.startswith("*."):
            suffix = normalized[1:]
            # A wildcard authorizes subdomains, never the bare apex.
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == normalized:
            return True
    return False


def _ip_allowed(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    cidrs: list[str],
) -> bool:
    return any(address in ipaddress.ip_network(cidr) for cidr in cidrs)


def _url_allowed(candidate: str | None, allowed_urls: list[str]) -> bool:
    if candidate is None:
        return False
    parsed_candidate = urlsplit(candidate)
    for allowed in allowed_urls:
        parsed_allowed = urlsplit(allowed)
        if not parsed_allowed.scheme or not parsed_allowed.hostname:
            continue
        allowed_port = parsed_allowed.port or (
            443 if parsed_allowed.scheme.lower() == "https" else 80
        )
        candidate_port = parsed_candidate.port or (
            443 if parsed_candidate.scheme.lower() == "https" else 80
        )
        if (
            parsed_candidate.scheme.lower() != parsed_allowed.scheme.lower()
            or parsed_candidate.hostname != parsed_allowed.hostname
            or candidate_port != allowed_port
        ):
            continue
        base_path = (parsed_allowed.path or "/").rstrip("/")
        candidate_path = (parsed_candidate.path or "/").rstrip("/")
        if candidate_path == base_path or candidate_path.startswith(base_path + "/"):
            return True
    return False


def _grant_matches(
    grant: MissionGrant,
    request: PolicyRequest,
    target: _Target | None,
    now: datetime,
) -> bool:
    if not grant.granted_at <= now < grant.expires_at:
        return False
    if request.risk_class not in grant.risk_classes:
        return False
    if grant.tool_names and request.tool_name not in grant.tool_names:
        return False
    if grant.targets:
        if target is None:
            return False
        normalized_grants = {value.rstrip(".").lower() for value in grant.targets}
        if (
            target.original.rstrip(".").lower() not in normalized_grants
            and target.host not in normalized_grants
        ):
            return False
    return True


class PolicyEngine:
    """Evaluate a request against a frozen engagement scope policy."""

    _network_risks = {
        RiskClass.PASSIVE,
        RiskClass.ACTIVE_SCAN,
        RiskClass.CREDENTIAL_USE,
        RiskClass.EXPLOITATION,
        RiskClass.PERSISTENCE,
        RiskClass.DESTRUCTIVE,
    }
    _always_approve = {
        RiskClass.CREDENTIAL_USE,
        RiskClass.EXPLOITATION,
        RiskClass.PERSISTENCE,
        RiskClass.DESTRUCTIVE,
        RiskClass.SCOPE_CHANGE,
    }

    def evaluate(
        self,
        policy: ScopePolicy,
        request: PolicyRequest,
        *,
        now: datetime | None = None,
    ) -> PolicyDecision:
        current = (now or utc_now()).astimezone(timezone.utc)
        if policy.not_before and current < policy.not_before:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason="engagement execution window has not started",
                rule="time_window",
            )
        if policy.not_after and current >= policy.not_after:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason="engagement execution window has ended",
                rule="time_window",
            )
        if policy.local_only and request.cloud_transfer:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason="engagement is local-only and forbids cloud transfer",
                rule="local_only",
            )

        action = (request.action or request.tool_name).casefold()
        prohibited = {value.casefold() for value in policy.prohibited_actions}
        if action in prohibited or request.tool_name.casefold() in prohibited:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason="action is explicitly prohibited by the rules of engagement",
                rule="prohibited_action",
            )
        target: _Target | None = None
        if request.target:
            try:
                target = _normalize_target(request.target, request.port)
            except ValueError as exc:
                return PolicyDecision(
                    effect=PolicyEffect.DENY,
                    reason=str(exc),
                    rule="invalid_target",
                )
        if request.risk_class in self._network_risks:
            if target is None:
                return PolicyDecision(
                    effect=PolicyEffect.DENY,
                    reason="network-capable tools require an explicit target",
                    rule="target_required",
                )
            scope_decision = self._check_target(policy, request, target)
            if scope_decision is not None:
                return scope_decision

        # Approval can authorize a high-risk in-scope effect, but it can never
        # expand hard target, port, DNS, prohibition, or time boundaries.
        if request.writes_outside_workspace:
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                reason="writes outside the engagement workspace require operator approval",
                rule="external_write",
                normalized_target=target.host if target else None,
            )

        if request.credential_class and request.risk_class != RiskClass.CREDENTIAL_USE:
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                reason="credential use always requires operator approval",
                rule="credential_use",
                normalized_target=target.host if target else None,
            )
        if request.risk_class in self._always_approve:
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                reason=f"{request.risk_class.value} actions always require operator approval",
                rule="high_risk",
                normalized_target=target.host if target else None,
            )

        if request.risk_class == RiskClass.ACTIVE_SCAN:
            for index, grant in enumerate(policy.grants):
                if _grant_matches(grant, request, target, current):
                    return PolicyDecision(
                        effect=PolicyEffect.ALLOW,
                        reason="request is covered by an active mission grant",
                        rule="mission_grant",
                        normalized_target=target.host if target else None,
                        matched_grant_index=index,
                    )
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                reason="active scanning requires a current mission grant or approval",
                rule="active_scan",
                normalized_target=target.host if target else None,
            )

        return PolicyDecision(
            effect=PolicyEffect.ALLOW,
            reason="request is read-only, passive, or confined to the workspace",
            rule="low_risk",
            normalized_target=target.host if target else None,
        )

    def _check_target(
        self,
        policy: ScopePolicy,
        request: PolicyRequest,
        target: _Target,
    ) -> PolicyDecision | None:
        address_allowed = target.address is not None and _ip_allowed(
            target.address, policy.allowed_cidrs
        )
        hostname_allowed = _domain_allowed(target.host, policy.allowed_domains)
        url_allowed = _url_allowed(target.url, policy.allowed_urls)
        if not (address_allowed or hostname_allowed or url_allowed):
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason="target is outside the authorized engagement scope",
                rule="target_scope",
                normalized_target=target.host,
            )

        requested_ports = set(request.ports)
        if target.port is not None:
            requested_ports.add(target.port)
        if policy.allowed_ports and (
            not requested_ports
            or any(port not in policy.allowed_ports for port in requested_ports)
        ):
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason="target port is outside the authorized engagement scope",
                rule="port_scope",
                normalized_target=target.host,
            )

        # For hostnames, all observed addresses must be authorized.  A second
        # untrusted DNS answer therefore fails closed instead of broadening scope.
        for value in request.resolved_ips:
            address = ipaddress.ip_address(value)
            if not _ip_allowed(address, policy.allowed_cidrs):
                return PolicyDecision(
                    effect=PolicyEffect.DENY,
                    reason=f"resolved address {address} is outside authorized CIDRs",
                    rule="dns_rebinding",
                    normalized_target=target.host,
                )
        if target.address is None and request.risk_class != RiskClass.PASSIVE:
            if not request.resolved_ips:
                return PolicyDecision(
                    effect=PolicyEffect.DENY,
                    reason="active hostname targets require pinned, in-scope DNS answers",
                    rule="dns_resolution_required",
                    normalized_target=target.host,
                )
        return None


def path_is_within_workspace(path: str | Path, workspace: str | Path) -> bool:
    """Return whether a normalized path remains inside the engagement workspace."""

    candidate = Path(path).expanduser().resolve(strict=False)
    root = Path(workspace).expanduser().resolve(strict=False)
    return candidate == root or root in candidate.parents


__all__ = [
    "PolicyDecision",
    "PolicyEffect",
    "PolicyEngine",
    "PolicyRequest",
    "path_is_within_workspace",
]
