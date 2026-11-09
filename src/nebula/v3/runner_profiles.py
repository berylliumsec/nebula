"""One stored-profile conversion for workload launch and lost-owner cleanup."""

from pathlib import Path

from .domain import RunnerIsolation, RunnerProfile as StoredRunnerProfile
from .sandbox import (
    ContainerRuntimeType,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
)


def container_runner_profile(stored: StoredRunnerProfile) -> RunnerProfile:
    platform = (
        RunnerPlatform.LINUX
        if stored.isolation == RunnerIsolation.ROOTLESS
        else RunnerPlatform.MACOS
    )
    if stored.isolation == RunnerIsolation.ROOTLESS:
        isolation, machine = RunnerIsolationMode.LINUX_ROOTLESS, None
    elif stored.isolation == RunnerIsolation.PODMAN_MACHINE:
        isolation, machine = (
            RunnerIsolationMode.PODMAN_MACHINE,
            stored.context or "podman-machine-default",
        )
    else:
        isolation, machine = RunnerIsolationMode.DOCKER_DESKTOP_VM, None
    return RunnerProfile(
        runtime_type=ContainerRuntimeType(stored.runtime.value),
        executable=Path(stored.executable),
        platform=platform,
        isolation_mode=isolation,
        context=stored.context,
        socket=stored.socket if stored.isolation == RunnerIsolation.ROOTLESS else None,
        machine_name=machine,
        seccomp_profile=Path(stored.seccomp_profile)
        if stored.seccomp_profile
        else None,
    )
