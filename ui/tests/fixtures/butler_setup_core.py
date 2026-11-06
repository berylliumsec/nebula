"""Real setup state machine with inert fixed-candidate and image boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nebula.v3 import cli
from nebula.v3.api import create_app as _create_app
from nebula.v3.domain import Engagement, RunnerProfile
from nebula.v3.runtime_platform import (
    HumanTerminalRuntimeResolution,
    RuntimePlatformError,
)
from nebula.v3.sandbox import ContainerSandboxRunner, PreparedContainerImage


class InertSetupIO:
    def __init__(self, store, platform):
        self.store = store
        self.platform = platform
        self.attempts = 0
        self.prepared_projects: list[str] = []
        self.image = PreparedContainerImage(
            source_reference="docker.io/kalilinux/kali-rolling:latest",
            base_resolved_reference="docker.io/kalilinux/kali-rolling@sha256:"
            + "a" * 64,
            base_digest="sha256:" + "a" * 64,
            resolved_reference="sha256:" + "b" * 64,
            digest="sha256:" + "b" * 64,
            platform="linux/amd64",
            configured_user="",
            installed_packages=("kali-linux-headless", "iputils-ping"),
            refreshed=True,
            detail="Synthetic image metadata; no container or download ran",
            binary_inventory=(("python3", "/usr/bin/python3", "Synthetic fixture"),),
        )

    async def verify(self, identity):
        profile = await self.store.get(RunnerProfile, identity)
        if not profile.healthy:
            profile = await self.store.update(
                RunnerProfile,
                profile.id,
                {
                    "healthy": True,
                    "last_health_detail": "Synthetic fixed runner verification",
                },
                expected_revision=profile.revision,
            )
        return profile

    async def resolve(self, project_id, *, on_progress=None):
        (await self.store.get(Engagement, project_id))
        profile = self.platform.resolve_human_terminal_profile(project_id)
        if on_progress is not None:
            self.attempts += 1
            self.prepared_projects.append(project_id)
            await on_progress("Synthetic image preparation; no OCI process is running.")
            if self.attempts == 1:
                raise RuntimePlatformError(
                    "Synthetic image registry failure; retry is available"
                )
            if self.attempts == 2:
                await asyncio.Event().wait()
        self.platform._persist_runtime_metadata(profile, self.image)
        return HumanTerminalRuntimeResolution(
            profile=profile,
            runner=self,
            workspace=self.platform.workspace_for(project_id),
            image=self.image,
        )


async def inert_available(_runner):
    return True, "Synthetic fixed-candidate probe; no program ran"


async def forbidden_capture(_runner, *_args, **_kwargs):
    raise AssertionError("the setup fixture must not execute an OCI program")


def configure_setup_io(store, platform, root: Path):
    platform.execution_enabled = True
    candidates = tuple(root / "fixed" / name for name in ("podman", "docker"))
    candidates[0].parent.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        candidate.write_text("#!/bin/sh\nexit 99\n")
        candidate.chmod(0o755)
    # These files only establish fixed identity/executable metadata. All runner
    # probes and process boundaries below are inert or explicitly forbidden.
    ContainerSandboxRunner._trusted_runtime_paths = candidates
    ContainerSandboxRunner.available = inert_available
    ContainerSandboxRunner._capture = forbidden_capture
    boundary = InertSetupIO(store, platform)
    platform.verify_runner = boundary.verify
    platform.resolve_human_terminal_runtime = boundary.resolve
    return boundary


def create_fixture_app(*args: Any, **kwargs: Any):
    store = args[0] if args else kwargs["store"]
    platform = kwargs["tool_platform"]
    store.create(Engagement(name="Setup controls fixture", status="active"))
    store.create(Engagement(name="Other setup fixture", status="active"))
    configure_setup_io(store, platform, Path(platform.workspace_root).parent)
    return _create_app(*args, **kwargs)


if __name__ == "__main__":
    cli.create_app = create_fixture_app
    cli.main()
