"""Desktop-viewer registration shared by fenced execution workers.

The registration is short-lived (it expires unless the desktop refreshes it),
but it outlives one supervisor generation: a private file in the runtime
directory carries it to the next generation, so a supervisor swap neither
detaches the desktop browser nor turns its next refresh into a conflict.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from pydantic import Field, SecretStr, ValidationError, field_validator
from starlette.responses import JSONResponse

from .authentication import secret_matches
from .browser_engine import VisibleElectronAdapter
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .diagnostics import record_caught_exception
from .domain import NebulaModel
from .execution_scope import current_execution_lease
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from .runtime_ipc import RuntimeIpcError
from .shared_memory_rpc import MemoryAuthority

BRIDGE_OPERATION = "host.desktop-browser/v1"
BRIDGE_PROTOCOL = "nebula.desktop-browser/v1"
BRIDGE_TTL_SECONDS = 180
# The registration the next supervisor generation adopts, in the runtime root.
BRIDGE_STATE_NAME = "browser-bridge.json"
BRIDGE_UNAVAILABLE = (
    "The desktop browser is disconnected. Open Browser in the Electron app and retry."
)


def bridge_endpoint(value: str) -> str:
    try:
        parsed, port = urlsplit(value), urlsplit(value).port
    except ValueError:
        raise ValueError(
            "Desktop bridge requires its canonical loopback origin"
        ) from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or value != f"http://127.0.0.1:{port}"
    ):
        raise ValueError("Desktop bridge requires its canonical loopback origin")
    return value


class BridgeRegistration(NebulaModel):
    endpoint: str = Field(min_length=1, max_length=100)
    token: SecretStr = Field(min_length=20, max_length=512)
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    registration_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{20,100}$"
    )

    @field_validator("endpoint")
    @classmethod
    def local_endpoint(cls, value):
        return bridge_endpoint(value)

    @field_validator("token")
    @classmethod
    def opaque_token(cls, value):
        if re.fullmatch(r"[A-Za-z0-9_-]{20,512}", value.get_secret_value()) is None:
            raise ValueError("Desktop bridge requires an opaque token")
        return value


class BridgeRevocation(NebulaModel):
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    registration_id: str = Field(pattern=r"^[A-Za-z0-9_-]{20,100}$")


class BridgeState(NebulaModel):
    enabled: bool
    bridge: BridgeRegistration | None = None


@dataclass(repr=False)
class _Viewer:
    actor_id: str
    registration: BridgeRegistration
    deadline: float


class HostBrowserBridge:
    def __init__(
        self,
        coordination,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.coordination, self.clock, self.wall_clock = coordination, clock, wall_clock
        self.enabled = False
        self.viewer: _Viewer | None = None
        self.closed = False
        self.state_path: Path | None = None

    def _current(self):
        if self.viewer and self.viewer.deadline <= self.clock():
            self.viewer = None
            # Never leave an expired bearer token on disk.
            self._forget()
        if self.closed:
            self.viewer = None
        return self.viewer

    def restore(self, state_path: Path) -> bool:
        """Adopt the previous generation's unexpired registration.

        Called once the supervisor holds the host lock, so no other process
        writes the file. Returns whether a registration was adopted. An
        unreadable or expired file is discarded: the desktop registers again
        on its next refresh, as after a cold start.
        """
        from .runtime_launcher import read_private

        self.state_path = state_path
        try:
            document = json.loads(read_private(state_path))
        except FileNotFoundError:
            # diagnostic-expected: no desktop viewer was attached to the previous generation.
            return False
        except (OSError, ValueError) as error:
            record_caught_exception(
                "runtime",
                "runtime.browser_bridge.restore_failed",
                "The saved desktop browser registration was unreadable; the "
                "desktop attaches again on its next refresh.",
                error,
                stage="startup",
            )
            self._forget()
            return False
        try:
            if not isinstance(document, dict) or set(document) != {
                "actor_id",
                "registration",
                "expires_at",
            }:
                raise ValueError("Unexpected desktop browser registration file")
            actor_id = document["actor_id"]
            if (
                not isinstance(actor_id, str)
                or not 1 <= len(actor_id) <= 512
                or (actor_id != "operator" and not actor_id.startswith("device:"))
            ):
                raise ValueError("Invalid desktop browser registration actor")
            registration = BridgeRegistration.model_validate(document["registration"])
            if registration.registration_id is None:
                raise ValueError("Saved desktop browser registration lacks its id")
            expires_at = document["expires_at"]
            if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
                raise ValueError("Invalid desktop browser registration expiry")
        except (ValidationError, ValueError) as error:
            record_caught_exception(
                "runtime",
                "runtime.browser_bridge.restore_failed",
                "The saved desktop browser registration was invalid; the "
                "desktop attaches again on its next refresh.",
                error,
                stage="startup",
            )
            self._forget()
            return False
        remaining = min(float(expires_at) - self.wall_clock(), BRIDGE_TTL_SECONDS)
        if remaining <= 0:
            self._forget()
            return False
        self.enabled = True
        self.viewer = _Viewer(actor_id, registration, self.clock() + remaining)
        return True

    def _save(self, viewer: _Viewer):
        if self.state_path is None:
            return
        from .runtime_launcher import write_private

        registration = viewer.registration.model_dump(mode="json")
        registration["token"] = viewer.registration.token.get_secret_value()
        try:
            write_private(
                self.state_path,
                json.dumps(
                    {
                        "actor_id": viewer.actor_id,
                        "registration": registration,
                        "expires_at": self.wall_clock()
                        + (viewer.deadline - self.clock()),
                    }
                ).encode(),
            )
        except OSError as error:
            # The registration still serves this generation; only its
            # survival across the next supervisor swap is lost.
            record_caught_exception(
                "runtime",
                "runtime.browser_bridge.save_failed",
                "The desktop browser registration was not saved; after the next "
                "Core restart the desktop attaches again on its next refresh.",
                error,
                stage="browser-bridge",
            )

    def _forget(self):
        if self.state_path is None:
            return
        try:
            os.unlink(self.state_path)
        except FileNotFoundError:
            # diagnostic-expected: nothing was saved for this registration.
            pass
        except OSError as error:
            record_caught_exception(
                "runtime",
                "runtime.browser_bridge.forget_failed",
                "A revoked desktop browser registration could not be removed; "
                "it expires within its lease.",
                error,
                stage="browser-bridge",
            )

    def register_viewer(self, registration: BridgeRegistration, actor_id: str):
        if self.closed:
            raise HTTPException(503, BRIDGE_UNAVAILABLE)
        current = self._current()
        if registration.registration_id is not None:
            if (
                current is None
                or current.actor_id != actor_id
                or current.registration.device_id != registration.device_id
                or current.registration.registration_id != registration.registration_id
                or current.registration.endpoint != registration.endpoint
                or not secret_matches(
                    current.registration.token.get_secret_value(),
                    registration.token.get_secret_value(),
                )
            ):
                raise HTTPException(
                    409, "This desktop viewer registration is no longer current"
                )
        else:
            registration = registration.model_copy(
                update={"registration_id": secrets.token_urlsafe(24)}
            )
        self.enabled = True
        self.viewer = _Viewer(actor_id, registration, self.clock() + BRIDGE_TTL_SECONDS)
        self._save(self.viewer)
        return {
            "registration_id": registration.registration_id,
            "device_id": registration.device_id,
            "ttl_seconds": BRIDGE_TTL_SECONDS,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=BRIDGE_TTL_SECONDS)
            ).isoformat(),
        }

    def revoke_viewer(self, request: BridgeRevocation, actor_id: str):
        current = self._current()
        if current is None:
            return {"revoked": False}
        if (
            current.actor_id != actor_id
            or current.registration.registration_id != request.registration_id
            or current.registration.device_id != request.device_id
        ):
            raise HTTPException(
                409, "This desktop viewer registration is no longer current"
            )
        self.viewer = None
        self._forget()
        return {"revoked": True}

    def mount(self, app, authentication):
        async def read(request, model):
            await authentication.require_auth(
                request, await authentication.bearer(request)
            )
            try:
                local = (
                    ipaddress.ip_address(request.client.host).is_loopback
                    if request.client
                    else False
                )
            except ValueError:
                # diagnostic-expected: an invalid peer address fails the local-only registration check closed.
                local = False
            if not local:
                raise HTTPException(
                    403, "Desktop bridge registration requires the local Core host"
                )
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 16 * 1024:
                    raise HTTPException(
                        413, "Desktop bridge registration exceeds 16 KiB"
                    )
                body.extend(chunk)
            try:
                value = model.model_validate_json(bytes(body))
            except ValidationError:
                raise HTTPException(
                    422, "Invalid desktop bridge registration"
                ) from None
            device = getattr(request.state, "auth_device_id", None)
            return value, "device:" + device if device else "operator"

        @app.post("/api/v1/desktop/browser-bridge")
        async def register(request: Request):
            value, actor = await read(request, BridgeRegistration)
            return JSONResponse(
                self.register_viewer(value, actor),
                headers={"Cache-Control": "private, no-store"},
            )

        @app.delete("/api/v1/desktop/browser-bridge")
        async def revoke(request: Request):
            value, actor = await read(request, BridgeRevocation)
            return JSONResponse(
                self.revoke_viewer(value, actor),
                headers={"Cache-Control": "private, no-store"},
            )

    def register(self, rpc, runtime_id):
        async def resolve(payload):
            try:
                if set(payload) != {"lease"}:
                    raise ValueError("Invalid desktop bridge request")
                lease = ProjectLease(
                    **MemoryAuthority.model_validate(payload["lease"]).model_dump()
                )
                if (
                    lease.runtime_id != runtime_id
                    or lease.project_id == GLOBAL_EXECUTION_PROJECT
                ):
                    raise LeaseLost("Desktop browser requires its exact project owner")
                async with self.coordination.fenced(lease):
                    current = self._current()
                    value = (
                        current.registration.model_dump(mode="json")
                        if current
                        else None
                    )
                    if value is not None:
                        value["token"] = current.registration.token.get_secret_value()
                    return {
                        "protocol": BRIDGE_PROTOCOL,
                        "lease": asdict(lease),
                        "state": {"enabled": self.enabled, "bridge": value},
                    }
            except (ValidationError, ValueError, CoordinationConflict):
                raise HTTPException(
                    403, "Desktop browser requires a live authenticated project grant"
                ) from None

        rpc.register(BRIDGE_OPERATION, resolve)

    def close(self):
        # The saved registration stays for the next generation; it expires
        # by itself if none starts within its lease.
        self.closed, self.viewer = True, None


class RemoteBrowserBridge:
    def __init__(self, client):
        self.client = client

    async def resolve(self) -> BridgeState:
        lease = current_execution_lease()
        if lease is None or lease.project_id == GLOBAL_EXECUTION_PROJECT:
            raise LeaseLost(
                "Desktop browser requires the current project execution context"
            )
        try:
            result = await self.client.call(BRIDGE_OPERATION, {"lease": asdict(lease)})
        except httpx.TransportError as error:
            raise ValueError(BRIDGE_UNAVAILABLE) from error
        try:
            if result.get("protocol") != BRIDGE_PROTOCOL or result.get(
                "lease"
            ) != asdict(lease):
                raise ValueError("Mismatched desktop browser grant")
            return BridgeState.model_validate(result["state"])
        except (AttributeError, KeyError, ValidationError, ValueError):
            raise RuntimeIpcError("Invalid desktop browser bridge result") from None


class CurrentVisibleAdapter(VisibleElectronAdapter):
    def __init__(
        self, initial: BridgeRegistration, resolve: Callable[[], Awaitable[BridgeState]]
    ):
        super().__init__(initial.endpoint, initial.token.get_secret_value())
        self.resolve = resolve

    async def _request(self, method, path, payload=None):
        state = await self.resolve()
        if state.bridge is None:
            raise ValueError(BRIDGE_UNAVAILABLE)
        if (
            method != "POST"
            or re.fullmatch(
                r"/v1/(?:identities/ensure|companion/[A-Za-z0-9_-]{1,200})", path
            )
            is None
        ):
            raise ValueError("Unsupported desktop browser operation")
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            return await client.request(
                method,
                state.bridge.endpoint + path,
                json=payload,
                headers={
                    "Authorization": "Bearer " + state.bridge.token.get_secret_value(),
                    "Accept": "application/json",
                },
                timeout=self._deadline(path),
            )
