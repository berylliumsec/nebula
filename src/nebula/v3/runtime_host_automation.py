"""Shared image preparation and observation without project session owners."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import platform
import re

from .automation_runtime import AutomationRuntimeInfo
from .domain import AutomationProjectPolicy, Engagement, RunnerProfile
from .runner_profiles import container_runner_profile
from .runtime_prepared_catalog import (
    _runner_profile_fingerprint,
    automation_runtime_metadata,
    persist_runtime_metadata,
)
from .sandbox import ContainerImagePreparer, ContainerSandboxRunner

KALI_REPOSITORY = "docker.io/kalilinux/kali-rolling"
KALI_SOURCE = KALI_REPOSITORY + ":latest"


class HostAutomationRuntime:
    def __init__(self, store, data_root, *, runner_factory=None, preparer_factory=None):
        self.store, self.data_root = store, Path(data_root)
        self.runner_factory = runner_factory or (
            lambda profile: ContainerSandboxRunner(
                profile=container_runner_profile(profile)
            )
        )
        self.preparer_factory = preparer_factory or ContainerImagePreparer
        self.memory_compute = None
        self.lock = asyncio.Lock()

    async def _profiles(self):
        return [
            profile
            for profile in await self.store.list_entities(RunnerProfile, limit=1000)
            if profile.enabled and profile.healthy
        ]

    async def runtime_info(self, *, engagement_id=None):
        if engagement_id:
            await self.store.get(Engagement, engagement_id)
            policies = await self.store.list_entities(
                AutomationProjectPolicy, engagement_id=engagement_id, limit=1
            )
            if policies and policies[0].execution_mode == "host":
                policy = policies[0]
                ready = (
                    policy.host_access_acknowledged
                    and os.name == "posix"
                    and Path("/bin/bash").is_file()
                )
                identity = f"host-v1:{platform.node()}:{platform.system()}:{os.getuid()}:/bin/bash"
                return AutomationRuntimeInfo(
                    configured=True,
                    ready=ready,
                    runner_profile_id="host",
                    digest="sha256:" + hashlib.sha256(identity.encode()).hexdigest(),
                    detail="Host mode: commands run as the Core OS user with host filesystem and network access"
                    if ready
                    else "Acknowledge host filesystem and network access in Project settings"
                    if not policy.host_access_acknowledged
                    else "Host mode requires POSIX and /bin/bash",
                )
        profiles = await self._profiles()
        if not profiles:
            return AutomationRuntimeInfo(
                configured=True,
                ready=False,
                detail="no verified healthy container runner is configured",
            )
        metadata = await asyncio.to_thread(automation_runtime_metadata, self.data_root)
        profile = next(
            (
                item
                for item in profiles
                if metadata and item.id == metadata["runner_profile_id"]
            ),
            None,
        )
        if profile is None or metadata.get(
            "runner_profile_fingerprint"
        ) != _runner_profile_fingerprint(profile):
            return AutomationRuntimeInfo(
                configured=True,
                ready=False,
                runner_profile_id=sorted(
                    profiles, key=lambda item: (item.created_at, item.id)
                )[0].id,
                detail="the existing Kali headless runtime has not been prepared for this runner configuration",
            )
        healthy, detail = await self.runner_factory(profile).available()
        return AutomationRuntimeInfo(
            configured=True,
            ready=healthy,
            image=metadata["image"],
            digest=metadata["digest"],
            runner_profile_id=profile.id,
            inventory=metadata["inventory"],
            detail="verified prepared Kali headless image is ready"
            if healthy
            else detail,
        )

    async def prepare(self):
        async with self.lock:
            projects = await self.store.list_entities(Engagement, limit=1)
            if not projects:
                return AutomationRuntimeInfo(
                    configured=True,
                    ready=False,
                    detail="create a project before preparing the Kali runtime",
                )
            policies = await self.store.list_entities(
                AutomationProjectPolicy, engagement_id=projects[0].id, limit=1
            )
            profiles = await self._profiles()
            requested = policies[0].runner_profile_id if policies else None
            profile = next(
                (item for item in profiles if item.id == (requested or "local")), None
            )
            if profile is None and len(profiles) == 1 and not requested:
                profile = profiles[0]
            if profile is None:
                return AutomationRuntimeInfo(
                    configured=True,
                    ready=False,
                    detail="select an enabled, verified container runner before preparing the Kali runtime",
                )
            source = (
                os.getenv("NEBULA_KALI_SOURCE_IMAGE")
                or os.getenv("NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE")
                or KALI_SOURCE
            )
            if (
                source != KALI_SOURCE
                and re.fullmatch(
                    re.escape(KALI_REPOSITORY) + r"@sha256:[0-9a-f]{64}", source
                )
                is None
            ):
                raise ValueError(
                    "NEBULA_KALI_SOURCE_IMAGE must reference the official Kali repository and be pinned by digest"
                )
            runner = self.runner_factory(profile)
            if self.memory_compute is not None:
                runner.memory_compute = self.memory_compute
                runner.memory_profile_id = profile.id
            image = await self.preparer_factory(
                runner=runner,
                platform=profile.platform,
                source_reference=source,
                expected_repository=KALI_REPOSITORY,
            ).prepare()
            # A concurrent trust/configuration change must not advertise this image.
            latest = await self.store.get(RunnerProfile, profile.id)
            if (
                not latest.enabled
                or not latest.healthy
                or _runner_profile_fingerprint(latest)
                != _runner_profile_fingerprint(profile)
            ):
                raise ValueError(
                    "Runner configuration changed during preparation; reload before retrying"
                )
            await asyncio.to_thread(
                persist_runtime_metadata, self.data_root, latest, image
            )
            return await self.runtime_info()
