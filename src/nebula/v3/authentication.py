"""Stateless authentication shared by replaceable API and execution boundaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import hashlib
import hmac
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .domain import PairedDeviceSession, utc_now
from .storage import NebulaStore

bearer = HTTPBearer(auto_error=False)


def secret_matches(candidate: str | None, expected: str | None) -> bool:
    """Constant-time equality that survives non-ASCII credential text.

    Starlette decodes header and cookie values as latin-1 and
    ``hmac.compare_digest`` raises ``TypeError`` for ``str`` operands outside
    ASCII, so a stray byte in ``Authorization``, ``Origin`` or the CSRF token
    turned the normal 401/403 into an unhandled 500 (and a pre-accept crash on
    every websocket). Comparing UTF-8 bytes keeps the constant-time property
    and simply reports a mismatch.
    """

    if candidate is None or expected is None:
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    )


class CoreAuthentication:
    bearer = bearer

    def __init__(
        self,
        store: NebulaStore,
        token: str,
        *,
        allow_unauthenticated: bool = False,
        clock: Callable[[], datetime] = utc_now,
        secrets_equal: Callable[[str | None, str | None], bool] = secret_matches,
    ):
        self.store, self.token = store, token
        self.allow_unauthenticated = allow_unauthenticated
        self.clock, self.secrets_equal = clock, secrets_equal

    async def device_for_token(
        self, raw_token: str | None
    ) -> PairedDeviceSession | None:
        if not raw_token:
            return None
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        # Look the hash up in the database: scanning the oldest 1,000 sessions
        # stopped recognising any device paired after that many pairings.
        device = await self.store.find_entity(
            PairedDeviceSession, "token_sha256", digest
        )
        if device is None:
            return None
        now = self.clock()
        if (
            device.revoked_at is not None
            or now >= device.idle_expires_at
            or now >= device.absolute_expires_at
        ):
            return None
        return device

    async def cookie_websocket_authenticated(self, websocket: WebSocket) -> bool:
        device = await self.device_for_token(websocket.cookies.get("nebula_device"))
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin", "")
        parsed = urlsplit(f"//{host}")
        scheme = "https" if websocket.url.scheme == "wss" else "http"
        return bool(
            device
            and host
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and origin
            and self.secrets_equal(origin, f"{scheme}://{host}")
        )

    async def require_bearer_auth(
        self,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> str:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not self.secrets_equal(credentials.credentials, self.token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="local bearer authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials.credentials

    async def require_auth(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> str:
        if self.allow_unauthenticated:
            return "unauthenticated-local-mode"
        if (
            credentials is not None
            and credentials.scheme.lower() == "bearer"
            and self.secrets_equal(credentials.credentials, self.token)
        ):
            return credentials.credentials
        device = await self.device_for_token(request.cookies.get("nebula_device"))
        if device is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="valid bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.auth_device_id = device.id
        host_header = request.headers.get("host", "")
        parsed_host = urlsplit(f"//{host_header}")
        if (
            not host_header
            or not parsed_host.hostname
            or parsed_host.username
            or parsed_host.password
            or parsed_host.path
            or parsed_host.query
            or parsed_host.fragment
        ):
            raise HTTPException(
                status_code=400, detail="invalid paired-device Host header"
            )
        expected_origin = f"{request.url.scheme}://{host_header}"
        origin = request.headers.get("origin")
        if origin and not self.secrets_equal(origin, expected_origin):
            raise HTTPException(
                status_code=403, detail="paired-device origin validation failed"
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("X-Nebula-CSRF")
            cookie_csrf = request.cookies.get("nebula_csrf")
            digest = hashlib.sha256((csrf or "").encode("utf-8")).hexdigest()
            if (
                not csrf
                or not cookie_csrf
                or not self.secrets_equal(csrf, cookie_csrf)
                or not self.secrets_equal(digest, device.csrf_sha256)
            ):
                raise HTTPException(
                    status_code=403, detail="paired-device CSRF validation failed"
                )
            if not origin:
                raise HTTPException(
                    status_code=403, detail="paired-device mutation requires Origin"
                )
        now = self.clock()
        if (now - device.last_used_at).total_seconds() >= 300:
            (
                await self.store.update(
                    PairedDeviceSession,
                    device.id,
                    {
                        "last_used_at": now,
                        "idle_expires_at": min(
                            now + timedelta(days=30), device.absolute_expires_at
                        ),
                    },
                    expected_revision=device.revision,
                )
            )
        return f"device:{device.id}"
