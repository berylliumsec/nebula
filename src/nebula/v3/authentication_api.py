"""API-owned pairing and device sessions with no execution-controller lifecycle."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import hashlib
import hmac
import ipaddress
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy import delete, func, select, text
from starlette.responses import JSONResponse

from .action_broker import ActionBroker
from .authentication import CoreAuthentication
from .database import DevicePairingOfferRow
from .domain import (
    DeviceCapabilitySnapshot,
    NebulaModel,
    PairedDeviceSession,
    ResourceRef,
)
from .storage import ConflictError, NebulaStore, NotFoundError, StoreTransaction

PAIRING_CONFIRMATION_ATTEMPTS = 5
MAX_PENDING_OFFERS = 256
MAX_AUTH_BODY_BYTES = 128 * 1024


class AuthenticationBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/auth/"):
            await self.app(scope, receive, send)
            return

        async def private_send(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.lower() != b"cache-control"
                    ]
                    + [(b"cache-control", b"private, no-store")],
                }
            await send(message)

        if scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, private_send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_AUTH_BODY_BYTES:
                await JSONResponse(
                    {
                        "detail": "Authentication request exceeds the 128 KiB control-message bound"
                    },
                    status_code=413,
                )(scope, receive, private_send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        supplied = False

        async def replay():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, private_send)


class PairingCreateRequest(NebulaModel):
    name: str = Field(default="Mobile device", min_length=1, max_length=200)


class PairingCreateResponse(NebulaModel):
    secret: str = Field(repr=False)
    confirmation_code: str = Field(repr=False)
    expires_at: datetime


class PairingRedeemRequest(NebulaModel):
    secret: str = Field(min_length=20, max_length=500, repr=False)
    confirmation_code: str = Field(pattern=r"^[0-9]{6}$", repr=False)
    name: str = Field(default="Mobile device", min_length=1, max_length=200)


class PairedDeviceResponse(NebulaModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    current: bool = False
    platform: str | None = None
    app_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    ownership_claims: list[ResourceRef] = Field(default_factory=list)
    heartbeat_at: datetime | None = None
    healthy: bool = False


class PairingRedeemResponse(NebulaModel):
    device: PairedDeviceResponse
    csrf_token: str = Field(repr=False)


class DevicePairingOffers:
    def __init__(self, store: NebulaStore, authentication: CoreAuthentication):
        self.store, self.authentication = store, authentication

    @asynccontextmanager
    async def _write(self):
        async with self.store.database.session() as session:
            if self.store.database.engine.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtext(current_database()), hashtext(current_schema() || ':nebula:pairing-offers:v1'))"
                    )
                )
            yield session

    @staticmethod
    def _confirmation(secret: str, code: str) -> str:
        # A database reader cannot brute-force the six-digit confirmation code
        # without the independent high-entropy offer secret, which is never stored.
        return hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()

    async def create(self, name: str) -> PairingCreateResponse:
        secret, code = secrets.token_urlsafe(32), f"{secrets.randbelow(1_000_000):06d}"
        now = self.authentication.clock()
        expires = now + timedelta(minutes=5)
        async with self._write() as session:
            await session.execute(
                delete(DevicePairingOfferRow).where(
                    DevicePairingOfferRow.expires_at_ms <= int(now.timestamp() * 1000)
                )
            )
            count = await session.scalar(
                select(func.count()).select_from(DevicePairingOfferRow)
            )
            if count is not None and count >= MAX_PENDING_OFFERS:
                raise HTTPException(
                    429,
                    "Too many pending invitations; wait for unused offers to expire",
                )
            session.add(
                DevicePairingOfferRow(
                    token_sha256=hashlib.sha256(secret.encode()).hexdigest(),
                    confirmation_mac=self._confirmation(secret, code),
                    name=name,
                    expires_at_ms=int(expires.timestamp() * 1000),
                    attempts=0,
                )
            )
        return PairingCreateResponse(
            secret=secret, confirmation_code=code, expires_at=expires
        )

    async def redeem(
        self, body: PairingRedeemRequest, *, client: str
    ) -> tuple[PairedDeviceSession, str, str]:
        digest = hashlib.sha256(body.secret.encode()).hexdigest()
        # Unauthenticated random guesses perform a bounded indexed read, without
        # taking SQLite's writer lock or pruning another client's valid offers.
        async with self.store.database.session() as session:
            known = await session.get(DevicePairingOfferRow, digest)
        if known is None:
            raise HTTPException(401, "pairing secret is invalid or expired")
        failure, device, raw_token, csrf = None, None, None, None
        async with self._write() as session:
            offer = await session.get(
                DevicePairingOfferRow, digest, with_for_update=True
            )
            now = self.authentication.clock()
            if offer is None or offer.expires_at_ms <= int(now.timestamp() * 1000):
                if offer is not None:
                    await session.delete(offer)
                failure = "pairing secret is invalid or expired"
            elif not hmac.compare_digest(
                offer.confirmation_mac,
                self._confirmation(body.secret, body.confirmation_code),
            ):
                offer.attempts += 1
                if offer.attempts >= PAIRING_CONFIRMATION_ATTEMPTS:
                    await session.delete(offer)
                failure = "pairing confirmation code did not match"
            else:
                raw_token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                device = PairedDeviceSession(
                    name=body.name or offer.name,
                    token_sha256=hashlib.sha256(raw_token.encode()).hexdigest(),
                    csrf_sha256=hashlib.sha256(csrf.encode()).hexdigest(),
                    created_at=now,
                    last_used_at=now,
                    idle_expires_at=now + timedelta(days=30),
                    absolute_expires_at=now + timedelta(days=90),
                    metadata={"client": client[:500]},
                )
                await StoreTransaction(session).add(device)
                await session.delete(offer)
        # Wrong-code attempts and expired-offer deletion must commit before the
        # rejection is raised; cancellation/failed device creation rolls back both.
        if failure is not None:
            raise HTTPException(401, failure)
        assert device is not None and raw_token is not None and csrf is not None
        return device, raw_token, csrf


class AuthenticationApi:
    def __init__(
        self,
        app: FastAPI,
        store: NebulaStore,
        authentication: CoreAuthentication,
        *,
        allow_insecure_pairing=False,
    ):
        self.store, self.authentication = store, authentication
        self.offers, self.broker = (
            DevicePairingOffers(store, authentication),
            ActionBroker(store),
        )
        app.add_middleware(AuthenticationBodyLimit)

        @app.post(
            "/api/v1/auth/pairings",
            response_model=PairingCreateResponse,
            dependencies=[Depends(authentication.require_bearer_auth)],
        )
        async def create(request: Request, body: PairingCreateRequest):
            host = request.client.host if request.client else ""
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise HTTPException(
                    403,
                    "new device pairings may be created only from the local machine",
                )
            return await self.offers.create(body.name)

        @app.post("/api/v1/auth/pairings/redeem", response_model=PairingRedeemResponse)
        async def redeem(
            request: Request, response: Response, body: PairingRedeemRequest
        ):
            if request.url.scheme != "https" and not allow_insecure_pairing:
                raise HTTPException(400, "device pairing requires HTTPS")
            device, token, csrf = await self.offers.redeem(
                body, client=request.headers.get("user-agent", "")
            )
            secure = request.url.scheme == "https"
            response.set_cookie(
                "nebula_device",
                token,
                max_age=90 * 24 * 60 * 60,
                secure=secure,
                httponly=True,
                samesite="strict",
                path="/",
            )
            response.set_cookie(
                "nebula_csrf",
                csrf,
                max_age=90 * 24 * 60 * 60,
                secure=secure,
                httponly=False,
                samesite="strict",
                path="/",
            )
            return PairingRedeemResponse(
                device=self.public(device, current_id=device.id), csrf_token=csrf
            )

        @app.get(
            "/api/v1/auth/devices",
            dependencies=[Depends(authentication.require_auth)],
            response_model=list[PairedDeviceResponse],
        )
        async def devices(request: Request):
            current = await authentication.device_for_token(
                request.cookies.get("nebula_device")
            )
            return [
                self.public(item, current_id=current.id if current else None)
                for item in await store.find_entities(
                    PairedDeviceSession, {"revoked_at": None}, bulk=False
                )
            ]

        @app.put(
            "/api/v1/auth/devices/current/capabilities",
            dependencies=[Depends(authentication.require_auth)],
            response_model=PairedDeviceResponse,
        )
        async def heartbeat(request: Request, snapshot: DeviceCapabilitySnapshot):
            identity = getattr(request.state, "auth_device_id", None)
            if not identity:
                raise HTTPException(
                    403, "capability heartbeat requires paired-device authentication"
                )
            try:
                device = await self.broker.heartbeat(identity, snapshot)
            except ConflictError as error:
                raise HTTPException(409, str(error)) from error
            return self.public(device, current_id=device.id)

        @app.delete(
            "/api/v1/auth/devices/{device_id}",
            status_code=204,
            dependencies=[Depends(authentication.require_auth)],
        )
        async def revoke(device_id: str, request: Request, response: Response):
            current = await authentication.device_for_token(
                request.cookies.get("nebula_device")
            )
            for attempt in range(3):
                try:
                    device = await store.get(PairedDeviceSession, device_id)
                except NotFoundError:
                    raise HTTPException(404, "paired device is unavailable") from None
                if device.revoked_at is not None:
                    break
                try:
                    await store.update(
                        PairedDeviceSession,
                        device.id,
                        {"revoked_at": authentication.clock()},
                        expected_revision=device.revision,
                    )
                    break
                except ConflictError:
                    if attempt == 2:
                        raise HTTPException(
                            409, "Device changed; refresh before revoking it"
                        ) from None
            if current is not None and current.id == device_id:
                response.delete_cookie("nebula_device", path="/")
                response.delete_cookie("nebula_csrf", path="/")
            response.status_code = 204
            return response

    def public(
        self, device: PairedDeviceSession, *, current_id=None
    ) -> PairedDeviceResponse:
        return PairedDeviceResponse(
            **{
                name: getattr(device, name)
                for name in PairedDeviceResponse.model_fields
                if name not in {"current", "healthy"}
            },
            current=device.id == current_id,
            healthy=self.broker.healthy(device),
        )
