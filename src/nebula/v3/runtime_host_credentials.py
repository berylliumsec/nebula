"""Resolve host session secrets only through a signed, exact project grant."""

from __future__ import annotations

from dataclasses import asdict

import httpx
from fastapi import HTTPException
from pydantic import Field, SecretStr, ValidationError

from .domain import NebulaModel, ProviderProfile
from .credentials import CredentialError
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .execution_scope import current_execution_lease
from .providers import ModelProvider, ProviderError, provider_from_profile
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from .runtime_ipc import RuntimeIpcError
from .shared_memory_rpc import MemoryAuthority

SESSION_CREDENTIAL = "host.provider-credential/v1"


class SessionCredentialRequest(NebulaModel):
    lease: MemoryAuthority
    provider_id: str = Field(min_length=1, max_length=200)
    reference: str = Field(pattern=r"^session:[0-9a-f]{32}$")


def register_session_credentials(rpc, runtime_id, coordination, credentials):
    async def resolve(payload):
        try:
            request = SessionCredentialRequest.model_validate(payload)
            lease = ProjectLease(**request.lease.model_dump())
            if (
                lease.runtime_id != runtime_id
                or lease.project_id == GLOBAL_EXECUTION_PROJECT
            ):
                raise LeaseLost("Session credential request belongs to another worker")
            async with coordination.fenced(lease) as session:
                from .database import EntityRow

                row = await session.get(EntityRow, request.provider_id)
                if row is None or row.kind != ProviderProfile.entity_kind:
                    raise ValueError("Provider is unavailable")
                profile = ProviderProfile.model_validate(row.payload)
                if not profile.enabled or profile.secret_ref != request.reference:
                    raise ValueError("Provider credential reference changed")
                # Session lookup is a bounded in-memory read; never call the OS
                # vault or perform external I/O while holding the project fence.
                secret = credentials.resolve(request.reference)
                return {"lease": asdict(lease), "secret": secret.get_secret_value()}
        except (ValidationError, ValueError, CoordinationConflict, CredentialError):
            raise HTTPException(
                403, "Host session credential is unavailable for this project grant"
            ) from None

    rpc.register(SESSION_CREDENTIAL, resolve)


class RemoteSessionCredentials:
    def __init__(self, client):
        self.client = client

    async def resolve(self, provider_id, reference) -> SecretStr:
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost(
                "Session credentials require the current project execution context"
            )
        try:
            result = await self.client.call(
                SESSION_CREDENTIAL,
                {
                    "lease": asdict(lease),
                    "provider_id": provider_id,
                    "reference": reference,
                },
            )
        except (httpx.TransportError, RuntimeIpcError) as error:
            raise ProviderError(
                "The host session credential is unavailable; save it again in Settings"
            ) from error
        if not isinstance(result, dict) or result.get("lease") != asdict(lease):
            raise RuntimeIpcError("Invalid host session credential response")
        secret = result.get("secret")
        if not isinstance(secret, str) or not 1 <= len(secret) <= 16384:
            raise RuntimeIpcError("Invalid host session credential response")
        return SecretStr(secret)


class SessionCredentialProvider(ModelProvider):
    def __init__(self, profile, resolver, *, factory=provider_from_profile):
        self.profile, self.resolver, self.factory = profile, resolver, factory
        super().__init__(factory(profile).config)

    async def _provider(self):
        secret = await self.resolver(self.profile.id, self.profile.secret_ref)
        return self.factory(self.profile, lambda reference: secret)

    async def complete(self, request):
        return await (await self._provider()).complete(request)

    async def health(self):
        return await (await self._provider()).health()

    async def stream(self, request):
        provider = await self._provider()
        async for event in provider.stream(request):
            yield event

    async def openrouter_route_limits(self, model):
        return await (await self._provider()).openrouter_route_limits(model)
