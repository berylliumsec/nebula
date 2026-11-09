"""Canonical integration Settings writes executed by durable host commands."""

from __future__ import annotations

import asyncio
import re

from fastapi import Depends, HTTPException, Response
from pydantic import SecretStr
from sqlalchemy import select

from .credentials import CredentialCreateRequest, CredentialError
from .database import RuntimeResourceRow
from .diagnostics import record_caught_exception
from .domain import (
    AutomationProjectPolicy,
    AutomationSession,
    AutomationSessionStatus,
    ToolSuggestionSettings,
    VpnProfile,
    WebSearchRuntimeState,
)
from .integration_settings import (
    TypeSafeKeyRequest,
    VpnProfileCreateRequest,
    VpnProfileDeleteRequest,
    WebSearchEngineRequest,
    typesafe_integration_status,
    web_search_integration_status,
)
from .tool_suggestions import (
    SETTINGS_ID as TYPESAFE_SETTINGS_ID,
    check_connection,
    load_settings as load_typesafe_settings,
    resolve_jev_client,
)
from .vpn import VpnProfileError, parse_openvpn_profile
from .web_search import WebSearchError

HOST_INTEGRATION_WRITES = {
    ("POST", "/api/v1/vpn-profiles"),
    ("DELETE", "/api/v1/vpn-profiles/{profile_id}"),
    ("PUT", "/api/v1/integrations/typesafe"),
    ("POST", "/api/v1/integrations/typesafe/test"),
    ("DELETE", "/api/v1/integrations/typesafe"),
    ("POST", "/api/v1/integrations/web-search/install"),
    ("POST", "/api/v1/integrations/web-search/start"),
    ("POST", "/api/v1/integrations/web-search/stop"),
    ("POST", "/api/v1/integrations/web-search/test"),
    ("PUT", "/api/v1/integrations/web-search/engines"),
    ("DELETE", "/api/v1/integrations/web-search"),
}


def is_host_integration_write(method, path):
    return (method, path) in HOST_INTEGRATION_WRITES or (
        method == "DELETE"
        and re.fullmatch(r"/api/v1/vpn-profiles/[^/]+", path) is not None
    )


class HostIntegrationEffects:
    def __init__(self, observations):
        self.observations = observations
        self.settings, self.store = observations.settings, observations.settings.store
        self.vpn_lock, self.typesafe_lock, self.search_lock = (
            asyncio.Lock(),
            asyncio.Lock(),
            asyncio.Lock(),
        )
        self.typesafe_check = check_connection
        self.typesafe_client = resolve_jev_client
        # Direct legacy OCI startup is not an acceptable memory-managed adapter.
        self.search_initialization_supported = None

    @property
    def credentials(self):
        return self.settings.credentials

    async def search_capability(self, action):
        if self.search_initialization_supported is not None:
            return {
                "supported": self.search_initialization_supported,
                "detail": "Synthetic runtime adapter",
            }
        observe = getattr(self.observations.search, "initialization_capability", None)
        if callable(observe):
            value = await observe(action=action)
            if (
                isinstance(value, dict)
                and type(value.get("supported")) is bool
                and isinstance(value.get("detail"), str)
            ):
                return value
        return {
            "supported": False,
            "detail": "This Core has no supported memory-managed search runtime adapter.",
        }

    async def require_search_initialization(self, action):
        capability = await self.search_capability(action)
        if not capability["supported"]:
            raise HTTPException(
                501, "Web search initialization is unavailable: " + capability["detail"]
            )

    async def search_status(self):
        status = await web_search_integration_status(
            self.store, self.observations.search
        )
        start, install = await asyncio.gather(
            self.search_capability("start"), self.search_capability("install")
        )
        return {
            **status.model_dump(mode="json"),
            "initialization_supported": start["supported"],
            "initialization_detail": start["detail"],
            "installation_supported": install["supported"],
            "installation_detail": install["detail"],
        }

    async def save_typesafe(self, **changes):
        current = await load_typesafe_settings(self.store)
        if current is None:
            return await self.store.create(
                ToolSuggestionSettings(id=TYPESAFE_SETTINGS_ID, **changes)
            )
        return await self.store.update(
            ToolSuggestionSettings,
            current.id,
            changes,
            expected_revision=current.revision,
        )

    async def public_vpn(self, profile):
        status = await asyncio.to_thread(self.credentials.status, profile.secret_ref)
        return {
            **profile.model_dump(mode="json", exclude={"secret_ref"}),
            "available": status.available,
        }

    async def refuse_vpn_use(self, profile_id):
        if await self.store.find_entities(
            AutomationProjectPolicy, {"vpn_profile_id": profile_id}, limit=1
        ):
            raise HTTPException(
                409, "remove this VPN profile from project command policies first"
            )
        active = [
            item.value
            for item in AutomationSessionStatus
            if item
            not in {AutomationSessionStatus.CLOSED, AutomationSessionStatus.FAILED}
        ]
        if await self.store.find_entities(
            AutomationSession, {"vpn_profile_id": profile_id, "status": active}, limit=1
        ):
            raise HTTPException(
                409, "close active command sessions using this VPN profile first"
            )
        async with self.store.database.session() as session:
            resources = await session.scalars(
                select(RuntimeResourceRow).where(
                    RuntimeResourceRow.state != "closed",
                    RuntimeResourceRow.kind == "resident_handle",
                )
            )
            if any(
                row.state == "uncertain" or "terminal" in row.detail.lower()
                for row in resources
            ):
                raise HTTPException(
                    409,
                    "VPN terminal use cannot be ruled out; close or reconcile active terminals before removing this profile",
                )

    def mount(self, app, auth):
        dependencies = [Depends(auth)]

        @app.post("/api/v1/vpn-profiles", status_code=201, dependencies=dependencies)
        async def create_vpn(body: VpnProfileCreateRequest):
            async with self.vpn_lock:
                try:
                    parsed = parse_openvpn_profile(
                        body.config, username=body.username, password=body.password
                    )
                    if await self.store.find_entities(
                        VpnProfile, {"fingerprint": parsed.fingerprint}, limit=1
                    ):
                        raise HTTPException(409, "this VPN profile is already saved")
                    secret = await asyncio.to_thread(
                        self.credentials.create,
                        CredentialCreateRequest(
                            secret=SecretStr(parsed.config),
                            persistence=body.persistence,
                        ),
                    )
                    try:
                        profile = await self.store.create(
                            VpnProfile(
                                name=body.name.strip(),
                                filename=body.filename,
                                remote_host=parsed.remote_host,
                                remote_port=parsed.remote_port,
                                protocol=parsed.protocol,
                                fingerprint=parsed.fingerprint,
                                requires_credentials=parsed.requires_credentials,
                                secret_ref=secret.reference,
                            )
                        )
                    except BaseException:
                        from .async_utils import finish_cleanup

                        await finish_cleanup(
                            asyncio.to_thread(self.credentials.delete, secret.reference)
                        )
                        raise
                    return await self.public_vpn(profile)
                except VpnProfileError as error:
                    raise HTTPException(422, str(error)) from error

        @app.delete(
            "/api/v1/vpn-profiles/{profile_id}",
            status_code=204,
            dependencies=dependencies,
        )
        async def delete_vpn(profile_id: str, body: VpnProfileDeleteRequest):
            async with self.vpn_lock:
                profile = await self.store.get(VpnProfile, profile_id)
                await self.refuse_vpn_use(profile_id)
                if profile.revision != body.expected_revision:
                    raise HTTPException(409, "VPN profile changed. Refresh and retry.")
                try:
                    await asyncio.to_thread(self.credentials.delete, profile.secret_ref)
                except CredentialError as error:
                    raise HTTPException(
                        409, "VPN profile was not removed. " + str(error)
                    ) from error
                await self.store.delete(
                    VpnProfile, profile_id, expected_revision=body.expected_revision
                )
                return Response(status_code=204)

        @app.put("/api/v1/integrations/typesafe", dependencies=dependencies)
        async def save_typesafe(body: TypeSafeKeyRequest):
            async with self.typesafe_lock:
                secret = await asyncio.to_thread(
                    self.credentials.create,
                    CredentialCreateRequest(
                        secret=body.secret, persistence=body.persistence
                    ),
                )
                previous = await load_typesafe_settings(self.store)
                try:
                    await self.save_typesafe(
                        secret_ref=secret.reference, last_test=None
                    )
                except BaseException:
                    from .async_utils import finish_cleanup

                    await finish_cleanup(
                        asyncio.to_thread(self.credentials.delete, secret.reference)
                    )
                    raise
                if previous is not None and previous.secret_ref:
                    try:
                        await asyncio.to_thread(
                            self.credentials.delete, previous.secret_ref
                        )
                    except CredentialError as error:
                        record_caught_exception(
                            "integrations",
                            "integrations.typesafe.previous_key_delete_failed",
                            "The replaced TypeSafe key could not be removed from the vault.",
                            error,
                            stage="replace",
                        )
                result = await self.typesafe_check(
                    await self.typesafe_client(self.store, self.credentials)
                )
                await self.save_typesafe(last_test=result)
                return await typesafe_integration_status(self.store, self.credentials)

        @app.post("/api/v1/integrations/typesafe/test", dependencies=dependencies)
        async def test_typesafe():
            async with self.typesafe_lock:
                result = await self.typesafe_check(
                    await self.typesafe_client(self.store, self.credentials)
                )
                await self.save_typesafe(last_test=result)
                return await typesafe_integration_status(self.store, self.credentials)

        @app.delete("/api/v1/integrations/typesafe", dependencies=dependencies)
        async def delete_typesafe():
            async with self.typesafe_lock:
                current = await load_typesafe_settings(self.store)
                if current is not None:
                    if current.secret_ref:
                        await asyncio.to_thread(
                            self.credentials.delete, current.secret_ref
                        )
                    await self.save_typesafe(secret_ref=None, last_test=None)
                return await typesafe_integration_status(self.store, self.credentials)

        async def search_action(action):
            async with self.search_lock:
                runtime = self.observations.search
                try:
                    if action in {"install", "start", "test"}:
                        await self.require_search_initialization(action)
                    if action == "install":
                        await runtime.record(
                            image_digest=await runtime.install(),
                            last_detail="image pinned",
                        )
                    elif action == "start":
                        await runtime.ensure_ready()
                    elif action == "stop":
                        await runtime.stop()
                    elif action == "test":
                        await runtime.record(last_test=await runtime.check())
                    elif action == "remove":
                        await runtime.remove()
                except WebSearchError as error:
                    raise HTTPException(503, str(error)) from error
                return await self.search_status()

        @app.post("/api/v1/integrations/web-search/install", dependencies=dependencies)
        async def install_search():
            return await search_action("install")

        @app.post("/api/v1/integrations/web-search/start", dependencies=dependencies)
        async def start_search():
            return await search_action("start")

        @app.post("/api/v1/integrations/web-search/stop", dependencies=dependencies)
        async def stop_search():
            return await search_action("stop")

        @app.post("/api/v1/integrations/web-search/test", dependencies=dependencies)
        async def test_search():
            return await search_action("test")

        @app.delete("/api/v1/integrations/web-search", dependencies=dependencies)
        async def remove_search():
            return await search_action("remove")

        @app.put("/api/v1/integrations/web-search/engines", dependencies=dependencies)
        async def engines(body: WebSearchEngineRequest):
            if len(set(body.engines)) != len(body.engines):
                raise HTTPException(422, "search engines must be unique")
            async with self.search_lock:
                runtime = self.observations.search
                state, _ = await runtime.state()
                if state == WebSearchRuntimeState.READY:
                    await self.require_search_initialization("reconfigure")
                await runtime.record(engines=body.engines)
                if state == WebSearchRuntimeState.READY:
                    try:
                        await runtime.remove()
                        await runtime.ensure_ready()
                    except WebSearchError as error:
                        raise HTTPException(503, str(error)) from error
                return await self.search_status()
