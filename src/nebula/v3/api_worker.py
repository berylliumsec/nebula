"""Public API role: authenticate, accept durable commands and deliver results.

No execution factory, producer, scheduler or recovery controller is constructed
here. Private viewer channels and SQL delivery outlive replaceable API workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from pathlib import Path
import re

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.security import HTTPAuthorizationCredentials
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from .authentication import CoreAuthentication
from .authentication_api import AuthenticationApi
from .deployments import DeploymentCoordinator
from .domain import ChatTurn
from .runtime_chat_stream import RuntimeChatJournal
from .runtime_coordination import CoordinationConflict
from .runtime_delivery import HttpIntent, MAX_REQUEST_BYTES, RuntimeDelivery
from .runtime_http import HttpUpstream, RuntimeHttpProxy
from .runtime_ipc import RuntimeIpcError
from .runtime_routes import command_lane
from .storage import NebulaStore, NotFoundError


class _Forward(Response):
    def __init__(self, proxy):
        super().__init__()
        self.proxy = proxy

    async def __call__(self, scope, receive, send):
        await self.proxy(scope, receive, send)


class _ViewerResponse(StreamingResponse):
    def __init__(self, *args, slots, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = slots

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.slots.release()


class _ApiStatic(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code != 404 or "." in Path(path).name:
                raise
            return await super().get_response("index.html", scope)


def create_api_worker(
    store: NebulaStore,
    delivery: RuntimeDelivery,
    *,
    operator_token: str,
    project_for: Callable[[HttpIntent], Awaitable[str]],
    upstream_for: Callable[[dict], Awaitable[HttpUpstream]],
    chat_acceptance_for: Callable[[str], Awaitable[bool]] | None = None,
    static_dir: Path | None = None,
    maximum_waiting_views: int = 32,
    maximum_control_views: int = 8,
    command_start_timeout: float = 15,
    poll_interval: float = 0.25,
    allow_insecure_device_pairing: bool = False,
) -> FastAPI:
    if (
        not operator_token
        or not 1 <= maximum_waiting_views <= 128
        or not 1 <= maximum_control_views <= 32
    ):
        raise ValueError(
            "API workers require authentication and bounded viewer capacity"
        )
    if not 0.01 <= poll_interval <= 2 or not 0.01 <= command_start_timeout <= 60:
        raise ValueError("API delivery polling is outside its configured bounds")
    if store.database._bootstrap:
        raise ValueError(
            "API workers validate supervisor-owned migrations; they never migrate"
        )
    auth = CoreAuthentication(store, operator_token)
    coordinator = DeploymentCoordinator(store.database)
    proxy = RuntimeHttpProxy(upstream_for)
    work_slots = asyncio.Semaphore(maximum_waiting_views)
    control_slots = asyncio.Semaphore(maximum_control_views)
    journal = RuntimeChatJournal(delivery.coordination, store)

    @asynccontextmanager
    async def lifespan(_app):
        await store.database.initialize()
        try:
            yield
        finally:
            await proxy.close()
            await store.event_history.close()
            await store.database.dispose()

    app = FastAPI(
        title="Nebula public API worker",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime_delivery = delivery
    app.state.viewer_proxy = proxy
    app.state.store = store
    app.state.authentication_api = AuthenticationApi(
        app, store, auth, allow_insecure_pairing=allow_insecure_device_pairing
    )

    @app.exception_handler(CoordinationConflict)
    async def conflict(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(NotFoundError)
    async def unavailable(_request, _error):
        return JSONResponse(
            {"detail": "Accepted request is unavailable"}, status_code=404
        )

    @app.exception_handler(RuntimeIpcError)
    async def private_channel_unavailable(_request, _error):
        return JSONResponse(
            {
                "detail": "Runtime delivery is unavailable. The saved request has not been retried."
            },
            status_code=503,
        )

    async def principal(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(auth.bearer),
    ) -> str:
        await auth.require_auth(request, credentials)
        device = getattr(request.state, "auth_device_id", None)
        return "device:" + device if device else "operator"

    def receipt_response(receipt):
        return JSONResponse(
            {
                "state": "accepted",
                "receipt": asdict(receipt),
                "detail": "Saved; waiting for its execution owner",
            },
            status_code=202,
            headers={"X-Nebula-Command-Id": receipt.command_id, "Retry-After": "1"},
        )

    async def response_view(
        actor, command_id, *, after=0, slots=work_slots, receipt=None
    ):
        if slots.locked():
            if receipt is None:
                raise HTTPException(
                    429, "Viewer capacity is busy; reconnect using the saved request ID"
                )
            return receipt_response(receipt)
        await slots.acquire()
        handed_off = False
        try:
            deadline = asyncio.get_running_loop().time() + command_start_timeout
            while True:
                page = await delivery.page(actor, command_id, limit=1)
                binding = await delivery.chat_binding(actor, command_id)
                if binding is not None and await journal.has_history(binding.turn_id):
                    await journal.validate_cursor(binding.turn_id, after_sequence=after)
                    response = _ViewerResponse(
                        journal.sse(binding.turn_id, after_sequence=after),
                        slots=slots,
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-store",
                            "X-Accel-Buffering": "no",
                            "X-Nebula-Command-Id": command_id,
                        },
                    )
                    handed_off = True
                    return response
                if page.frames:
                    head = page.frames[0]
                    if head.kind != "head":
                        raise RuntimeIpcError("saved HTTP response has no header")
                    metadata = json.loads(head.content)
                    break
                if page.state in {"uncertain", "failed"}:
                    return JSONResponse(
                        {
                            "detail": "The saved request needs reconciliation. It has not been retried.",
                            "command_id": command_id,
                            "state": page.state,
                        },
                        status_code=409,
                        headers={"X-Nebula-Command-Id": command_id},
                    )
                if asyncio.get_running_loop().time() >= deadline:
                    if receipt is not None:
                        return receipt_response(receipt)
                    return JSONResponse(
                        {"state": page.state, "command_id": command_id},
                        status_code=202,
                        headers={"Retry-After": "1"},
                    )
                await asyncio.sleep(poll_interval)
            content_type = next(
                (
                    value
                    for name, value in metadata["headers"]
                    if name.lower() == "content-type"
                ),
                "",
            )

            async def body():
                cursor = max(1, after)
                while True:
                    current = await delivery.page(
                        actor, command_id, after=cursor, limit=4
                    )
                    for frame in current.frames:
                        cursor = frame.sequence
                        if frame.kind == "body":
                            yield frame.content
                        elif frame.kind == "end":
                            return
                    if not current.frames and current.state in {
                        "succeeded",
                        "failed",
                        "uncertain",
                    }:
                        if current.state == "uncertain" and content_type.startswith(
                            "text/event-stream"
                        ):
                            yield b'event: error\ndata: {"type":"error","detail":"The saved request needs reconciliation","retryable":false}\n\n'
                        return
                    await asyncio.sleep(poll_interval)

            response = _ViewerResponse(
                body(), status_code=metadata["status"], slots=slots
            )
            response.raw_headers = [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in metadata["headers"]
                if name.lower()
                not in {"content-length", "transfer-encoding", "connection"}
                and not name.lower().startswith("x-nebula-internal-")
            ] + [(b"x-nebula-command-id", command_id.encode())]
            handed_off = True
            return response
        finally:
            if not handed_off:
                slots.release()

    @app.get("/api/v1/deployment/status")
    async def deployment_status(_actor=Depends(principal)):
        return await coordinator.status()

    @app.get("/api/v1/runtime/requests/{command_id}")
    async def request_status(command_id: str, _actor=Depends(principal)):
        page = await delivery.page(_actor, command_id, limit=1)
        binding = await delivery.chat_binding(_actor, command_id)
        return {
            "command_id": command_id,
            "project_id": page.project_id,
            "state": page.state,
            **(
                {"turn_id": binding.turn_id, "session_id": binding.session_id}
                if binding is not None
                else {}
            ),
        }

    @app.get("/api/v1/runtime/requests/{command_id}/response")
    async def request_response(
        command_id: str, after: int = 0, _actor=Depends(principal)
    ):
        if after < 0:
            raise HTTPException(422, "Response cursor must be nonnegative")
        return await response_view(_actor, command_id, after=after)

    @app.get("/api/v1/chat/turns/{turn_id}/events")
    async def chat_events(
        turn_id: str,
        after: int = Query(default=0, ge=0),
        epoch: str | None = Query(default=None, min_length=1, max_length=64),
        _actor=Depends(principal),
    ):
        turn = await store.get(ChatTurn, turn_id)
        if turn.harness_turn_id or not await journal.has_history(turn_id):
            return _Forward(proxy)
        await journal.validate_cursor(turn_id, after_sequence=after, epoch=epoch)
        if work_slots.locked():
            raise HTTPException(
                429, "Viewer capacity is busy; reconnect with the same cursor"
            )
        await work_slots.acquire()
        return _ViewerResponse(
            journal.sse(turn_id, after_sequence=after, epoch=epoch),
            slots=work_slots,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.api_route(
        "/api/v1/{path:path}",
        methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def public_request(request: Request):
        if request.url.path.startswith("/api/v1/auth/"):
            raise HTTPException(404, "Authentication route is unavailable")
        actor = await principal(request, await auth.bearer(request))
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return _Forward(proxy)
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[!-~]{1,200}", key):
            raise HTTPException(
                428, "A client-generated Idempotency-Key is required for this mutation"
            )
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                raise HTTPException(
                    413, "The saved request exceeds the 8 MiB command bound"
                )
            body.extend(chunk)
        try:
            intent = HttpIntent.capture(
                request.method,
                request.scope["path"],
                request.scope.get("query_string", b""),
                request.headers.raw,
                bytes(body),
            )
        except (ValueError, UnicodeError):
            raise HTTPException(
                422, "The request cannot be represented as a bounded durable command"
            ) from None
        lane = command_lane(intent.path)
        project_id = await project_for(intent)
        chat_acceptance = False
        if (
            intent.method == "POST"
            and intent.path == "/api/v1/chat/completions"
            and chat_acceptance_for is not None
        ):
            try:
                chat_body = json.loads(intent.body())
                requested = (
                    isinstance(chat_body, dict)
                    and chat_body.get("stream") is True
                    and chat_body.get("backend", "provider") == "provider"
                )
            except (ValueError, UnicodeError, RecursionError):
                requested = False
            if requested:
                chat_acceptance = await chat_acceptance_for(project_id)
        receipt = await delivery.accept(
            intent,
            project_id=project_id,
            actor_id=actor,
            idempotency_key=key,
            lane=lane,
            chat_acceptance=chat_acceptance,
        )
        del body, intent
        return await response_view(
            actor,
            receipt.command_id,
            receipt=receipt,
            slots=control_slots if lane == "control" else work_slots,
        )

    @app.websocket("/api/v1/{path:path}")
    async def public_websocket(websocket: WebSocket):
        # The execution adapter retains the existing bearer/cookie WebSocket
        # authentication and revocation checks. This is a viewer channel only.
        await proxy(websocket.scope, websocket.receive, websocket.send)

    if static_dir is not None:
        app.mount("/", _ApiStatic(directory=static_dir, html=True), name="workspace")
    return app
