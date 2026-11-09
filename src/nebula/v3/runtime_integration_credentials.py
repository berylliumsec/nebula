"""Purpose-scoped host session credentials for current execution owners."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal
from uuid import uuid5, NAMESPACE_URL

import httpx
from fastapi import HTTPException
from pydantic import Field, SecretStr, ValidationError

from .credentials import CredentialError, CredentialStore, CredentialUnavailableError
from .database import EntityRow
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .domain import (
    AutomationProjectPolicy,
    Engagement,
    NebulaModel,
    ScopePolicy,
    ToolSuggestionSettings,
    VpnProfile,
)
from .execution_scope import current_execution_lease
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from .runtime_ipc import RuntimeIpcError
from .shared_memory_rpc import MemoryAuthority

INTEGRATION_CREDENTIAL = "host.integration-credential/v1"


class IntegrationCredentialRequest(NebulaModel):
    lease: MemoryAuthority
    purpose: Literal["typesafe", "vpn"]
    operation: Literal["available", "resolve"]
    profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    reference: str = Field(pattern=r"^session:[0-9a-f]{32}$")


def register_integration_credentials(rpc, runtime_id, coordination, credentials):
    async def credential(payload):
        try:
            request = IntegrationCredentialRequest.model_validate(payload)
            lease = ProjectLease(**request.lease.model_dump())
            if (
                lease.runtime_id != runtime_id
                or lease.project_id == GLOBAL_EXECUTION_PROJECT
            ):
                raise LeaseLost(
                    "Integration credentials require this worker's exact project grant"
                )
            async with coordination.fenced(lease) as session:
                if request.purpose == "typesafe":
                    if request.profile_id is not None:
                        raise ValueError(
                            "TypeSafe credentials have one canonical settings record"
                        )
                    row = await session.get(EntityRow, "typesafe")
                    if row is None or row.kind != ToolSuggestionSettings.entity_kind:
                        raise ValueError("TypeSafe settings are unavailable")
                    settings = ToolSuggestionSettings.model_validate(row.payload)
                    if settings.secret_ref != request.reference:
                        raise ValueError("TypeSafe reference changed")
                    if request.operation == "resolve":
                        project_row = await session.get(EntityRow, lease.project_id)
                        if (
                            project_row is None
                            or project_row.kind != Engagement.entity_kind
                        ):
                            raise ValueError("Project is unavailable")
                        project = Engagement.model_validate(project_row.payload)
                        scope_row = (
                            await session.get(EntityRow, project.scope_policy_id)
                            if project.scope_policy_id
                            else None
                        )
                        if (
                            scope_row is None
                            or scope_row.kind != ScopePolicy.entity_kind
                        ):
                            raise ValueError("Project has no TypeSafe opt-in")
                        scope = ScopePolicy.model_validate(scope_row.payload)
                        if (
                            scope.engagement_id != lease.project_id
                            or not scope.tool_suggestions
                            or scope.local_only
                        ):
                            raise ValueError("Project does not permit TypeSafe ranking")
                else:
                    row = (
                        await session.get(EntityRow, request.profile_id)
                        if request.profile_id
                        else None
                    )
                    if row is None or row.kind != VpnProfile.entity_kind:
                        raise ValueError("VPN profile is unavailable")
                    profile = VpnProfile.model_validate(row.payload)
                    if profile.secret_ref != request.reference:
                        raise ValueError("VPN reference changed")
                    if request.operation == "resolve":
                        identity = str(
                            uuid5(
                                NAMESPACE_URL,
                                f"nebula:automation-policy:{lease.project_id}",
                            )
                        )
                        policy_row = await session.get(EntityRow, identity)
                        if (
                            policy_row is None
                            or policy_row.kind != AutomationProjectPolicy.entity_kind
                        ):
                            raise ValueError("Project has no selected VPN")
                        policy = AutomationProjectPolicy.model_validate(
                            policy_row.payload
                        )
                        if (
                            policy.engagement_id != lease.project_id
                            or policy.vpn_profile_id != profile.id
                            or not policy.network_enabled
                            or policy.execution_mode != "docker"
                        ):
                            raise ValueError("Project does not permit this VPN")
                # Both operations use session-only bounded memory reads under
                # the grant; vault, environment and systemd stay local.
                if request.operation == "available":
                    return {
                        "lease": asdict(lease),
                        "available": credentials.status(request.reference).available,
                    }
                secret = credentials.resolve(request.reference)
                return {"lease": asdict(lease), "secret": secret.get_secret_value()}
        except (ValidationError, ValueError, CoordinationConflict, CredentialError):
            raise HTTPException(
                403,
                "The host integration credential is unavailable for this project grant",
            ) from None

    rpc.register(INTEGRATION_CREDENTIAL, credential)


class RemoteExecutionCredentials(CredentialStore):
    def __init__(self, client, keyring_backend=None):
        super().__init__(keyring_backend)
        self.client = client

    async def _request(self, reference, *, purpose, profile_id, operation):
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost(
                "Integration credentials require the current project execution context"
            )
        try:
            result = await self.client.call(
                INTEGRATION_CREDENTIAL,
                {
                    "lease": asdict(lease),
                    "purpose": purpose,
                    "profile_id": profile_id,
                    "reference": reference,
                    "operation": operation,
                },
            )
        except (httpx.TransportError, RuntimeIpcError) as error:
            raise CredentialUnavailableError(
                "The host session credential is unavailable; save it again in Settings"
            ) from error
        if not isinstance(result, dict) or result.get("lease") != asdict(lease):
            raise RuntimeIpcError("Invalid host integration credential response")
        return result

    async def resolve_runtime(self, reference, *, purpose, profile_id=None):
        result = await self._request(
            reference, purpose=purpose, profile_id=profile_id, operation="resolve"
        )
        secret = result.get("secret")
        if not isinstance(secret, str) or not 1 <= len(secret) <= 16384:
            raise RuntimeIpcError("Invalid host integration credential response")
        return SecretStr(secret)

    async def runtime_available(self, reference, *, purpose, profile_id=None):
        try:
            result = await self._request(
                reference, purpose=purpose, profile_id=profile_id, operation="available"
            )
        except CredentialUnavailableError:
            # diagnostic-expected: credential availability is a boolean probe; resolution failures still propagate.
            return False
        if type(result.get("available")) is not bool:
            raise RuntimeIpcError("Invalid host integration credential response")
        return result["available"]
