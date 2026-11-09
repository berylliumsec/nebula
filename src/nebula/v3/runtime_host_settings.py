"""Host-owned external Settings operations without a project execution Kernel."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import os
import re
import time
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from starlette.responses import JSONResponse

from .credentials import (
    CredentialCreateRequest,
    CredentialError,
    CredentialStore,
    CredentialUnavailableError,
)
from .domain import (
    HarnessProfile,
    PairedDeviceSession,
    ProviderProfile,
    RunnerProfile,
    utc_now,
)
from .model_catalog import (
    OPENROUTER_PROVIDER_DIRECTORY_URL,
    openrouter_upstream_providers,
)
from .provider_settings import (
    ProviderCapabilityVerifyRequest,
    _discover_local_provider_services,
    _provider_health,
    _verify_provider_capability,
    persist_provider_model_catalog,
)
from .providers import provider_from_profile
from .runtime_delivery import HttpIntent, RuntimeDelivery
from .runtime_host_delivery import HOST_EFFECT, HostEffectDelivery
from .runtime_host_observations import HOST_OBSERVATION_ROUTES, HostObservations
from .runtime_integration_effects import is_host_integration_write
from .runtime_global_settings import is_global_settings_host_route
from .runtime_vault import RuntimeVault
from .storage import ConflictError, NebulaStore, NotFoundError


class _Forward(Response):
    def __init__(self, application):
        super().__init__()
        self.application = application

    async def __call__(self, scope, receive, send):
        await self.application(scope, receive, send)


@dataclass(frozen=True)
class _ReadIntent:
    """A transient read with the same bounded path/query/header validation."""

    path: str
    query: str
    headers: list[tuple[str, str]]
    method: Literal["GET"] = "GET"

    @classmethod
    def capture(cls, path, query, headers):
        # Reuse envelope validation without widening the durable mutation model.
        checked = HttpIntent.capture("POST", path, query, headers, b"")
        return cls(checked.path, checked.query, checked.headers)

    def body(self):
        return b""


def is_host_settings_route(method, path):
    return (
        (method, path) in HOST_OBSERVATION_ROUTES
        or is_host_integration_write(method, path)
        or is_global_settings_host_route(method, path)
        or (
            (
                method == "GET"
                and path in {"/api/v1/desktop-browser-mode", "/api/v1/ssh-environments"}
            )
            or (method == "GET" and path == "/api/v1/credentials/vault")
            or (method == "POST" and path == "/api/v1/credentials")
            or (
                method == "GET"
                and re.fullmatch(r"/api/v1/credentials/[^/]+/status", path)
            )
            or (method == "DELETE" and re.fullmatch(r"/api/v1/credentials/[^/]+", path))
        )
    )


class HostSettings:
    def __init__(
        self,
        host,
        *,
        credentials=None,
        provider_factory=None,
        runner_factory=None,
        harness_factory=None,
    ):
        self.host = host
        self.store = NebulaStore(host.coordinator.database)
        self.credentials = credentials or CredentialStore()
        self.provider_factory = provider_factory or (
            lambda profile: provider_from_profile(profile, self.credentials.resolve)
        )
        self.runner_factory, self.harness_factory = runner_factory, harness_factory
        self.directory_transport = self.credit_transport = None
        self.ssh_discover = None
        self._directory = None
        self._directory_at = 0.0
        self.actor = ContextVar("host_settings_actor", default="")
        self.effect = None
        self.read_slots = asyncio.Semaphore(8)
        self.observations = HostObservations(self)
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self._routes()
        self.observations.mount(self.app, self._authorized)

    async def start(self):
        await self.observations.start()
        if self.effect is None:
            delivery = RuntimeDelivery(
                self.host.coordinator.coordination,
                RuntimeVault(self.host.data_root / "runtime" / "payloads"),
            )
            self.effect = HostEffectDelivery(delivery, self)
            await self.effect.start()

    async def close(self):
        if self.effect is not None:
            await self.effect.close()
        await self.observations.close()
        await self.store.event_history.close()

    async def __call__(self, scope, receive, send):
        token = self.actor.set(scope.get("state", {}).get("global_actor", ""))
        try:
            await self.app(scope, receive, send)
        finally:
            self.actor.reset(token)

    async def _authorized(self):
        actor = self.actor.get()
        if actor == "operator":
            return actor
        if actor.startswith("device:"):
            try:
                device = await self.store.get(PairedDeviceSession, actor[7:])
            except NotFoundError:
                # diagnostic-expected: a removed accepting device is denied by the authorization check below.
                device = None
            now = utc_now()
            if (
                device
                and device.revoked_at is None
                and device.idle_expires_at > now
                and device.absolute_expires_at > now
            ):
                return actor
        raise HTTPException(401, "The accepting device session is no longer authorized")

    def _routes(self):
        app, auth = self.app, [Depends(self._authorized)]

        @app.get("/api/v1/desktop-browser-mode", dependencies=auth)
        async def desktop_browser_mode():
            services = self.host.shared_services
            cutover = (
                bool(services and services.browser_bridge.enabled)
                or os.environ.get("NEBULA_ONE_BROWSER_CUTOVER") == "1"
            )
            return {
                "one_browser_cutover": cutover,
                "agent_security_enabled": cutover
                or os.environ.get("NEBULA_AGENT_SECURITY_ENABLED") == "1",
            }

        @app.get("/api/v1/ssh-environments", dependencies=auth)
        async def ssh_environments(resolve: bool = Query(default=True)):
            if self.ssh_discover is not None:
                return await self.ssh_discover(resolve=resolve)
            from .environments import SshEnvironmentService

            return await SshEnvironmentService(self.store).discover(resolve=resolve)

        @app.exception_handler(NotFoundError)
        async def missing(_request, error):
            return JSONResponse({"detail": str(error)}, status_code=404)

        @app.exception_handler(ConflictError)
        async def conflict(_request, error):
            return JSONResponse({"detail": str(error)}, status_code=409)

        @app.exception_handler(ValueError)
        async def invalid(_request, error):
            return JSONResponse({"detail": str(error)}, status_code=422)

        @app.exception_handler(CredentialError)
        async def credential_error(_request, error):
            return JSONResponse(
                {"detail": str(error)},
                status_code=503
                if isinstance(error, CredentialUnavailableError)
                else 409,
            )

        @app.get("/api/v1/credentials/vault", dependencies=auth)
        async def vault():
            state = await asyncio.to_thread(lambda: self.credentials.vault_state)
            return {"state": state, "available": state == "available"}

        @app.get("/api/v1/credentials/{reference}/status", dependencies=auth)
        async def credential_status(reference: str):
            return await asyncio.to_thread(self.credentials.status, reference)

        @app.post("/api/v1/credentials", dependencies=auth, status_code=201)
        async def credential_create(body: CredentialCreateRequest):
            return await asyncio.to_thread(self.credentials.create, body)

        @app.delete(
            "/api/v1/credentials/{reference}", dependencies=auth, status_code=204
        )
        async def credential_delete(reference: str):
            await asyncio.to_thread(self.credentials.delete, reference)
            return Response(status_code=204)

        @app.get("/api/v1/providers/discover-local", dependencies=auth)
        async def discover():
            return await _discover_local_provider_services(self.provider_factory)

        @app.get("/api/v1/providers/openrouter/upstream-providers", dependencies=auth)
        async def upstreams():
            if (
                self._directory is not None
                and time.monotonic() - self._directory_at < 3600
            ):
                return self._directory
            try:
                async with httpx.AsyncClient(
                    timeout=10,
                    transport=self.directory_transport,
                    follow_redirects=False,
                ) as client:
                    response = await client.get(OPENROUTER_PROVIDER_DIRECTORY_URL)
                response.raise_for_status()
                value = openrouter_upstream_providers(response.json())
            except (httpx.HTTPError, ValueError):
                raise HTTPException(
                    502,
                    "OpenRouter's provider directory is unavailable; retry shortly.",
                ) from None
            self._directory, self._directory_at = value, time.monotonic()
            return value

        @app.post("/api/v1/providers/{provider_id}/health", dependencies=auth)
        async def health(provider_id: str):
            return await self.provider_health(provider_id)

        @app.post("/api/v1/provider-health/refresh", dependencies=auth)
        async def refresh_all():
            profiles = await self.store.list_entities(ProviderProfile, limit=1000)
            if await self.store.list_entities(ProviderProfile, offset=1000, limit=1):
                raise HTTPException(
                    413,
                    "Refresh at most 1000 providers together; check additional profiles individually",
                )
            semaphore = asyncio.Semaphore(8)

            async def check(profile):
                async with semaphore:
                    return await self.provider_health(profile.id)

            return await asyncio.gather(*(check(profile) for profile in profiles))

        @app.post(
            "/api/v1/providers/{provider_id}/capabilities/verify", dependencies=auth
        )
        async def verify(provider_id: str, body: ProviderCapabilityVerifyRequest):
            profile = await self.store.get(ProviderProfile, provider_id)
            if profile.revision != body.expected_revision:
                raise ConflictError(
                    "Provider revision changed; refresh before verifying"
                )
            return await _verify_provider_capability(
                self.store, profile, body.model, self.provider_factory
            )

        @app.post(
            "/api/v1/providers/{provider_id}/openrouter/credits/refresh",
            dependencies=auth,
        )
        async def credits(provider_id: str):
            from .butler_resources import record_openrouter_credit
            from .provider_credits import read_openrouter_credits

            profile = await self.store.get(ProviderProfile, provider_id)
            snapshot = await read_openrouter_credits(
                profile, self.credentials, transport=self.credit_transport
            )
            if (
                snapshot.status == "observed"
                and profile.openrouter_management_secret_ref
            ):
                await record_openrouter_credit(
                    self.store,
                    snapshot=snapshot,
                    management_ref=profile.openrouter_management_secret_ref,
                )
            return snapshot

        @app.post("/api/v1/runner-profiles/{profile_id}/probe", dependencies=auth)
        async def runner_probe(profile_id: str):
            from .runner_profiles import container_runner_profile
            from .sandbox import ContainerSandboxRunner

            profile = await self.store.get(RunnerProfile, profile_id)
            runner = (
                self.runner_factory(profile)
                if self.runner_factory
                else ContainerSandboxRunner(profile=container_runner_profile(profile))
            )
            healthy, detail = await runner.available()
            return await self.store.update(
                RunnerProfile,
                profile.id,
                {
                    "healthy": healthy,
                    "last_health_at": utc_now(),
                    "last_health_detail": detail,
                },
                expected_revision=profile.revision,
            )

        @app.post("/api/v1/harnesses/{profile_id}/health", dependencies=auth)
        async def harness_health(profile_id: str):
            from .diagnostic_guidance import reason_code_for
            from .harnesses import HarnessConfigurationError, HarnessRuntimeService

            profile = await self.store.get(HarnessProfile, profile_id)
            adapter = (
                self.harness_factory(profile.kind)
                if self.harness_factory
                else HarnessRuntimeService._default_adapter(profile.kind)
            )
            result = await adapter.probe(profile, self.credentials)
            capabilities = result.capabilities.model_copy(
                update={
                    "checked_at": result.checked_at,
                    "detail": result.detail,
                    "harness_version": result.harness_version,
                    "authentication_state": result.authentication_state
                    if result.healthy
                    else "failed"
                    if reason_code_for(
                        HarnessConfigurationError(result.detail or ""),
                        feature="harnesses",
                    )
                    == "authentication_failed"
                    else "unverified",
                    "session_state": profile.capabilities.session_state,
                    "turn_state": profile.capabilities.turn_state,
                    "last_successful_turn_at": profile.capabilities.last_successful_turn_at,
                    "last_turn_failure_reason": profile.capabilities.last_turn_failure_reason,
                    "exercised_capabilities": profile.capabilities.exercised_capabilities,
                }
            )
            changes = {"capabilities": capabilities}
            if capabilities.models and profile.default_model not in capabilities.models:
                changes["default_model"] = capabilities.models[0]
            await self.store.update(
                HarnessProfile, profile.id, changes, expected_revision=profile.revision
            )
            return result.model_copy(
                update={
                    "authentication_state": capabilities.authentication_state,
                    "session_state": capabilities.session_state,
                    "turn_state": capabilities.turn_state,
                    "last_successful_turn_at": capabilities.last_successful_turn_at,
                }
            )

    async def provider_health(self, provider_id):
        profile = await self.store.get(ProviderProfile, provider_id)
        health = await _provider_health(profile, self.provider_factory)
        stored = await persist_provider_model_catalog(self.store, profile, health)
        return health.model_copy(update={"provider_revision": stored.revision})

    def mount(self, app, authentication, proxy):
        async def observe(intent, actor):
            if self.read_slots.locked():
                raise HTTPException(429, "Host Settings reads are busy; retry shortly")
            try:
                async with self.read_slots, asyncio.timeout(30):
                    status, headers, body = await self.effect._response(intent, actor)
            except TimeoutError:
                raise HTTPException(
                    503, "Host Settings read timed out; retry shortly"
                ) from None
            response = Response(body, status_code=status)
            response.raw_headers = [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers
                if name.lower()
                not in {
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "cache-control",
                }
            ] + [(b"cache-control", b"private, no-store")]
            return response

        def intent_for(request, body=b""):
            try:
                if request.method == "GET":
                    return _ReadIntent.capture(
                        request.scope["path"],
                        request.scope.get("query_string", b""),
                        request.headers.raw,
                    )
                return HttpIntent.capture(
                    request.method,
                    request.scope["path"],
                    request.scope.get("query_string", b""),
                    request.headers.raw,
                    body,
                )
            except (ValueError, UnicodeError):
                raise HTTPException(422, "Host Settings request is invalid") from None

        async def principal(request):
            await authentication.require_auth(
                request, await authentication.bearer(request)
            )
            device = getattr(request.state, "auth_device_id", None)
            return "device:" + device if device else "operator"

        async def request(request: Request):
            actor = await principal(request)
            if not is_host_settings_route(request.method, request.url.path):
                return _Forward(proxy)
            if request.method == "GET":
                return await observe(intent_for(request), actor)
            telemetry = request.url.path == "/api/v1/diagnostics/events"
            key = request.headers.get("Idempotency-Key", "")
            if not telemetry and re.fullmatch(r"[!-~]{1,200}", key) is None:
                raise HTTPException(
                    428,
                    "A client-generated Idempotency-Key is required for this mutation",
                )
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 128 * 1024:
                    raise HTTPException(413, "Host Settings request exceeds 128 KiB")
                body.extend(chunk)
            intent = intent_for(request, bytes(body))
            if telemetry:
                return await observe(intent, actor)
            receipt = await self.effect.execute(
                intent, actor_id=actor, idempotency_key=key
            )
            return await self.effect.response(
                actor, receipt.command_id, receipt=receipt
            )

        for path, methods in (
            ("/api/v1/desktop-browser-mode", ["GET"]),
            ("/api/v1/ssh-environments", ["GET"]),
            ("/api/v1/providers/{path:path}", ["GET", "POST"]),
            ("/api/v1/provider-health/refresh", ["POST"]),
            ("/api/v1/runner-profiles/{profile_id}/probe", ["POST"]),
            ("/api/v1/harnesses/{profile_id}/health", ["POST"]),
            ("/api/v1/credentials", ["POST"]),
            ("/api/v1/credentials/{path:path}", ["GET", "DELETE"]),
        ):
            app.add_api_route(path, request, methods=methods)
        for method, path in sorted(HOST_OBSERVATION_ROUTES):
            app.add_api_route(path, request, methods=[method])

        async def owned(command_id, actor):
            from .database import RuntimeCommandRow

            async with self.store.database.session() as session:
                row = await session.get(RuntimeCommandRow, command_id)
                if row is None or row.actor_id != actor:
                    raise HTTPException(404, "Accepted request is unavailable")
                return row.name == HOST_EFFECT

        @app.get("/api/v1/runtime/requests/lookup")
        async def lookup(
            request: Request,
            idempotency_key: str = Query(
                min_length=1, max_length=200, pattern=r"^[!-~]+$"
            ),
        ):
            actor = await principal(request)
            try:
                identity = await self.effect.delivery.lookup(actor, idempotency_key)
            except NotFoundError:
                raise HTTPException(404, "Accepted request is unavailable") from None
            if not await owned(identity, actor):
                return _Forward(proxy)
            page = await self.effect.delivery.page(actor, identity, limit=1)
            return {
                "command_id": identity,
                "project_id": page.project_id,
                "state": page.state,
            }

        @app.get("/api/v1/runtime/requests/{command_id}")
        async def status(command_id: str, request: Request):
            actor = await principal(request)
            if not await owned(command_id, actor):
                return _Forward(proxy)
            page = await self.effect.delivery.page(actor, command_id, limit=1)
            return {
                "command_id": command_id,
                "project_id": page.project_id,
                "state": page.state,
            }

        @app.get("/api/v1/runtime/requests/{command_id}/response")
        async def response(
            command_id: str, request: Request, after: int = Query(default=0, ge=0)
        ):
            actor = await principal(request)
            if not await owned(command_id, actor):
                return _Forward(proxy)
            return await self.effect.response(actor, command_id, after=after)
