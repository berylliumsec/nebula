"""Authenticated, nonsecret identities for durable client storage namespaces."""

from uuid import uuid4

from fastapi import Depends, Request, Response

from .database import RuntimeCoreIdentityRow
from .runtime_coordination import RuntimeCoordination


async def core_identity(database) -> str:
    coordination = RuntimeCoordination(database)
    async with coordination._write() as session:
        await coordination._insert_absent(
            session,
            RuntimeCoreIdentityRow,
            {
                "key": "core",
                "core_id": str(uuid4()),
                "created_at_ms": await coordination._clock_ms(session),
            },
            ["key"],
        )
        identity = await session.get(RuntimeCoreIdentityRow, "core")
        assert identity is not None
        return identity.core_id


def mount_runtime_identity(app, store, authentication):
    @app.get("/api/v1/runtime/identity")
    async def identity(
        request: Request,
        response: Response,
        _authenticated=Depends(authentication.require_auth),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        device = getattr(request.state, "auth_device_id", None)
        return {
            "core_id": await core_identity(store.database),
            "principal_id": "device:" + device if device else "operator",
        }
