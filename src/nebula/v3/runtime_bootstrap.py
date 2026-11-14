"""Host-owned recovery assets and truthful health without an API process."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from .database import RuntimeReleaseRow
from .deployment_protocol import ReleaseManifest
from .release_store import ReleaseIntegrityError, ReleaseStore
from .runtime_coordination import CoordinationConflict
from .runtime_http import HttpUpstream
from .runtime_ipc import RuntimeIpcError, RuntimeRpcClient
from .runtime_static import RuntimeStatic
from .version import build_metadata

RECOVERY_PATH = "/settings#memory-budget-settings"
# Health answers well within the desktop launcher's 2 s request timeout.
API_PROBE_TIMEOUT = 1.0
# Frequent health readers share one recent answer instead of probing each time.
API_PROBE_TTL = 1.0

ApiState = Literal["ready", "not_responding", "unavailable"]


@dataclass(frozen=True)
class RecoveryAssets:
    version: str
    app: RuntimeStatic


class RuntimeBootstrap:
    def __init__(self, coordinator, routing):
        self.coordinator, self.routing = coordinator, routing
        self.assets: RecoveryAssets | None = None
        self._probe: tuple[str, float, bool] | None = None

    async def select_release(self, version: str) -> None:
        """Select only a registered, completely verified retained bundle.

        The launcher selects a release explicitly, independently of API memory
        admission. A failed selection retains the previous valid snapshot.
        """
        async with self.coordinator.database.session() as session:
            release = await session.get(RuntimeReleaseRow, version)
            if release is None:
                raise CoordinationConflict(
                    "Stage the recovery release before selecting it"
                )
            directory = Path(release.install_path)
            digest = release.manifest_sha256
            expected = ReleaseManifest.model_validate(release.manifest)
        manifest = await asyncio.to_thread(
            ReleaseStore.verify, directory, expected_manifest_sha256=digest
        )
        if manifest != expected or manifest.version != version:
            raise ReleaseIntegrityError(
                "Recovery assets differ from their registered release"
            )
        if manifest.ui_directory + "/index.html" not in manifest.files:
            raise ReleaseIntegrityError(
                "Recovery release must include a verified index.html"
            )
        self.assets = RecoveryAssets(
            version=version,
            app=RuntimeStatic(directory=directory / manifest.ui_directory, html=True),
        )

    async def api_promoted(self) -> bool:
        """SQL-only check for hot paths: an API is promoted with a live lease."""
        try:
            await self.routing.api_upstream({})
        except (RuntimeIpcError, OSError):
            # diagnostic-expected: this routing check reports unavailable API admission to the caller.
            return False
        return True

    async def api_state(self) -> ApiState:
        """Whether the promoted API worker actually answers, not only its lease."""
        try:
            upstream = await self.routing.api_upstream({})
        except (RuntimeIpcError, OSError):
            # diagnostic-expected: this readiness probe reports unavailable API admission as degraded host health.
            return "unavailable"
        return "ready" if await self._answers(upstream) else "not_responding"

    async def _answers(self, upstream: HttpUpstream) -> bool:
        loop = asyncio.get_running_loop()
        cached = self._probe
        if (
            cached is not None
            and cached[0] == upstream.endpoint
            and loop.time() < cached[1]
        ):
            return cached[2]
        # The signed RPC shares the worker's HTTP listener and event loop, and
        # sends no viewer credentials to the socket.
        client = RuntimeRpcClient(upstream.endpoint, upstream.credential)
        try:
            identity = await client.call(
                "worker.identity", {}, timeout=API_PROBE_TIMEOUT
            )
        except (RuntimeIpcError, OSError, httpx.HTTPError):
            # diagnostic-expected: an unanswered identity call reports the promoted API as not responding.
            identity = None
        finally:
            await client.close()
        answered = (
            isinstance(identity, dict)
            and identity.get("role") == "api"
            and identity.get("endpoint") == upstream.endpoint
        )
        self._probe = (upstream.endpoint, loop.time() + API_PROBE_TTL, answered)
        return answered

    async def health(self) -> dict:
        state = await self.api_state()
        ready = state == "ready"
        return {
            **build_metadata(),
            **(await self.coordinator.database.health()),
            "status": "ok" if ready else "degraded",
            "mode": (
                "local"
                if self.coordinator.database.engine.dialect.name == "sqlite"
                else "team"
            ),
            "api_version": "v1",
            "api_ready": ready,
            "api_state": state,
            "recovery_path": RECOVERY_PATH,
            "recovery_version": self.assets.version if self.assets else None,
            # Host availability does not prove a project runtime or runner is ready.
            "runner": "unavailable",
            "human_pty": "unavailable",
            "container_terminal": "unavailable",
        }
