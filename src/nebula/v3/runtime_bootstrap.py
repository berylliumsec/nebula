"""Host-owned recovery assets and truthful health without an API process."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .database import RuntimeReleaseRow
from .deployment_protocol import ReleaseManifest
from .release_store import ReleaseIntegrityError, ReleaseStore
from .runtime_coordination import CoordinationConflict
from .runtime_ipc import RuntimeIpcError
from .runtime_static import RuntimeStatic
from .version import build_metadata

RECOVERY_PATH = "/settings#memory-budget-settings"


@dataclass(frozen=True)
class RecoveryAssets:
    version: str
    app: RuntimeStatic


class RuntimeBootstrap:
    def __init__(self, coordinator, routing):
        self.coordinator, self.routing = coordinator, routing
        self.assets: RecoveryAssets | None = None

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

    async def api_ready(self) -> bool:
        try:
            await self.routing.api_upstream({})
        except (RuntimeIpcError, OSError):
            return False
        return True

    async def health(self) -> dict:
        ready = await self.api_ready()
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
            "recovery_path": RECOVERY_PATH,
            "recovery_version": self.assets.version if self.assets else None,
            # Host availability does not prove a project runtime or runner is ready.
            "runner": "unavailable",
            "human_pty": "unavailable",
            "container_terminal": "unavailable",
        }
