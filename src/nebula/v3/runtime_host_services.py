"""One private host-memory service shared by independently retained workers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat

import uvicorn
from sqlalchemy import select
from starlette.responses import JSONResponse

from .async_utils import finish_cleanup
from .database import RuntimeInstanceRow, RuntimeProcessRow
from .provider_scheduler import ProviderSchedulerConfig
from .runtime_capacity import RuntimeCapacity
from .runtime_ipc import RPC_PATH, RuntimeIpcError, RuntimeRpcClient, RuntimeRpcServer
from .shared_butler_memory import SharedButlerMemory
from .shared_memory_rpc import register_shared_memory

HOST_SOCKET_NAME = "host-services.sock"
PEER_HEADER = b"x-nebula-ipc-worker"


def host_service_client(bootstrap, secret: bytes) -> RuntimeRpcClient:
    if not bootstrap.identity.endpoint.startswith("unix:///"):
        raise RuntimeIpcError("Host services require a private local runtime endpoint")
    endpoint = Path(bootstrap.identity.endpoint[7:]).parent / HOST_SOCKET_NAME
    client = RuntimeRpcClient("unix://" + str(endpoint), secret)
    client.client.headers[PEER_HEADER.decode()] = bootstrap.identity.runtime_id
    return client


class _HostServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The stable public listener owns supervisor signal handling.
        yield


class HostSharedServices:
    def __init__(self, host, *, knowledge_index=None, capacity_limits=None):
        self.host = host
        self.coordination = host.coordinator.coordination
        self.memory = SharedButlerMemory(
            self.coordination,
            host.data_root / "butler-memory-index",
            embedding_index=knowledge_index,
        )
        self.capacity = RuntimeCapacity(self.coordination)
        self.capacity_limits = capacity_limits
        self.endpoint: str | None = None
        self.server = None
        self.task = None
        self.closed = False
        self._close_task = None
        self._peers = {}
        self._peer_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(8)
        self._requests: set[asyncio.Task] = set()
        self._socket_identity = None

    async def _peer(self, runtime_id):
        async with self._peer_lock:
            async with self.coordination.database.session() as session:
                process = await session.get(RuntimeProcessRow, runtime_id)
                runtime = await session.get(RuntimeInstanceRow, runtime_id)
                now = await self.coordination._clock_ms(session)
                if (
                    process is None
                    or runtime is None
                    or process.role != "execution"
                    or runtime.role != "execution"
                    or process.state != "ready"
                    or runtime.lease_expires_at_ms <= now
                ):
                    return None
                if runtime_id not in self._peers:
                    # A retired/expired incarnation cannot revive. Prune only
                    # such peers, retaining live nonce windows through cache use.
                    live = set(
                        await session.scalars(
                            select(RuntimeInstanceRow.id).where(
                                RuntimeInstanceRow.id.in_(tuple(self._peers)),
                                RuntimeInstanceRow.lease_expires_at_ms > now,
                            )
                        )
                    )
                    self._peers = {
                        key: value for key, value in self._peers.items() if key in live
                    }
                    if len(self._peers) >= 16:
                        raise RuntimeIpcError("Shared host peer capacity is busy")
                    secret = self.host.credentials.read(process.credential_reference)
                    rpc = RuntimeRpcServer(secret)
                    register_shared_memory(rpc, self.memory, runtime_id=runtime_id)
                    self._peers[runtime_id] = rpc
                return self._peers[runtime_id]

    async def app(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4404})
            return
        if scope["path"] != RPC_PATH or scope["method"] != "POST":
            await JSONResponse({"detail": "Not found"}, status_code=404)(
                scope, receive, send
            )
            return
        if self.closed or self._slots.locked():
            await JSONResponse(
                {"detail": "Shared host services are reconnecting or busy"},
                status_code=503,
            )(scope, receive, send)
            return
        peers = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == PEER_HEADER
        ]
        if len(peers) != 1 or re.fullmatch(rb"[A-Za-z0-9_-]{1,200}", peers[0]) is None:
            await JSONResponse(
                {"detail": "Authenticated execution peer required"}, status_code=401
            )(scope, receive, send)
            return
        task = asyncio.current_task()
        self._requests.add(task)
        await self._slots.acquire()
        try:
            try:
                peer = await self._peer(peers[0].decode("ascii"))
            except (RuntimeIpcError, OSError):
                await JSONResponse(
                    {"detail": "Shared host peer is unavailable"}, status_code=503
                )(scope, receive, send)
                return
            if peer is None:
                await JSONResponse(
                    {"detail": "A live execution peer is required"}, status_code=401
                )(scope, receive, send)
                return
            async with asyncio.timeout(30):
                await peer.app(scope, receive, send)
        finally:
            self._slots.release()
            self._requests.discard(task)

    async def start(self):
        if self.closed:
            raise RuntimeError("Create a new host-services instance after detachment")
        if self.task is not None:
            return
        if self.host.lock.descriptor is None or self.host.socket_root is None:
            raise RuntimeError("Shared services require exclusive supervisor ownership")
        path = self.host.socket_root / HOST_SOCKET_NAME
        if path.exists() or path.is_symlink():
            details = path.lstat()
            if not stat.S_ISSOCK(details.st_mode) or details.st_uid != os.getuid():
                raise RuntimeIpcError(
                    "Shared-service endpoint is not an owned Unix socket"
                )
        if (await self.capacity.status())["revision"] == 0:
            config = ProviderSchedulerConfig.from_environment()
            await self.capacity.configure(
                self.capacity_limits
                or {
                    "provider": config.provider_limit,
                    "background": config.background_limit,
                    "tool": config.global_tool_limit,
                    "cpu": max(1, min(4096, (os.cpu_count() or 1) - 1)),
                }
            )
        self.endpoint = "unix://" + str(path)
        self.server = _HostServer(
            uvicorn.Config(
                self.app,
                interface="asgi3",
                uds=str(path),
                lifespan="off",
                access_log=False,
                log_level="error",
                timeout_graceful_shutdown=None,
            )
        )
        self.task = asyncio.create_task(
            self.server.serve(), name="nebula-host-services"
        )
        try:
            async with asyncio.timeout(10):
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise RuntimeIpcError("Shared host listener did not start")
                    await asyncio.sleep(0.01)
            details = path.stat()
            self._socket_identity = (details.st_dev, details.st_ino)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self._close_task is None:
            self.closed = True

            async def cleanup():
                if self.server is not None:
                    self.server.should_exit = True
                try:
                    if self.task is not None:
                        await self.task
                finally:
                    # A cancelled server must not leave host SQL/index work
                    # running after its exclusive supervisor lock is released.
                    if self._requests:
                        await asyncio.gather(
                            *tuple(self._requests), return_exceptions=True
                        )
                    if self.endpoint and self._socket_identity:
                        path = Path(self.endpoint[7:])
                        if path.exists():
                            details = path.lstat()
                            if (
                                details.st_dev,
                                details.st_ino,
                            ) != self._socket_identity:
                                raise RuntimeIpcError(
                                    "Shared-service socket changed during cleanup"
                                )
                            path.unlink()

            self._close_task = asyncio.create_task(
                cleanup(), name="nebula-host-services-detach"
            )
        await finish_cleanup(self._join_close())

    async def _join_close(self):
        await self._close_task
