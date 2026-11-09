"""A kernel read-back barrier before a dedicated OCI job may compute."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import re
import secrets
import stat
import tempfile

from .memory_policy import (
    EffectiveMemoryPolicy,
    MemoryEnforcementEvidence,
    MemoryEnforcementUnavailable,
    MemoryLimits,
    MemoryTerminationReason,
    MemoryLaunchGroups,
)
from .domain import utc_now

_GUARD = r"""#!/bin/sh
set -eu
job_id=$1; nonce=$2; expected_max=$3; expected_swap=$4; expected_high=$5
shift 5
IFS= read -r memory_max < /sys/fs/cgroup/memory.max
IFS= read -r memory_swap < /sys/fs/cgroup/memory.swap.max
IFS= read -r memory_high < /sys/fs/cgroup/memory.high
[ "$memory_max" = "$expected_max" ] || exit 125
[ "$memory_swap" = "$expected_swap" ] || exit 125
if [ "$expected_high" != "none" ]; then [ "$memory_high" = "$expected_high" ] || exit 125; fi
[ ! -w /sys/fs/cgroup/cgroup.procs ] || exit 125
[ ! -w /sys/fs/cgroup/memory.max ] || exit 125
capabilities=unknown; no_new_privileges=0
while read -r key value rest; do
  case "$key" in CapEff:) capabilities=$value;; NoNewPrivs:) no_new_privileges=$value;; esac
done < /proc/self/status
case "$capabilities" in *[!0]*) exit 125;; esac
[ "$no_new_privileges" = "1" ] || exit 125
printf '{"protocol":"nebula.memory-runner/v1","job_id":"%s","nonce":"%s","max_bytes":%s,"swap_bytes":%s,"protected_process_tree":true}\n' "$job_id" "$nonce" "$memory_max" "$memory_swap"
IFS= read -r release
[ "$release" = "$nonce" ] || exit 125
exec "$@"
"""

_READ_USAGE = r"""for name in memory.max memory.swap.max memory.high memory.current memory.peak; do
if [ -r "/sys/fs/cgroup/$name" ]; then IFS= read -r value < "/sys/fs/cgroup/$name"; printf '%s\n' "$value"; else printf 'unknown\n'; fi
done"""


async def observe_memory_group(
    runner, job_id: str, group_id: str
) -> MemoryEnforcementEvidence | None:
    document, error, code = await runner._capture(
        "inspect", group_id, "--format", "{{json .}}"
    )
    if code:
        if "no such" in (document + error).lower():
            return None
        raise MemoryEnforcementUnavailable(
            "runner group identity could not be verified"
        )
    identity = json.loads(document)
    if (
        identity.get("Id", identity.get("ID")) != group_id
        or identity.get("Config", {}).get("Labels", {}).get("nebula.memory.job")
        != job_id
    ):
        raise MemoryEnforcementUnavailable(
            "runner group identity differs from this job"
        )
    if not identity.get("State", {}).get("Running"):
        return None
    output, _error, code = await runner._capture(
        "exec", "--user", "65532:65532", group_id, "/bin/sh", "-c", _READ_USAGE
    )
    if code:
        return None
    values = output.splitlines()
    if len(values) != 5 or not all(
        value.isdecimal() for value in (values[0], values[1], values[3])
    ):
        raise MemoryEnforcementUnavailable(
            "runner usage readback did not verify kernel limits"
        )
    limits = MemoryLimits(
        max_bytes=int(values[0]),
        swap_bytes=int(values[1]),
        high_bytes=None if values[2] == "max" else int(values[2]),
    )
    return MemoryEnforcementEvidence(
        job_id=job_id,
        group_id=group_id,
        applied=limits,
        protected_process_tree=True,
        current_bytes=int(values[3]),
        peak_bytes=int(values[4]) if values[4].isdecimal() else None,
        observed_at=utc_now().isoformat(),
        termination_reason="running",
        detail="Kernel memory limits and usage observed on the owned group.",
    )


def install_memory_guard(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{hashlib.sha256(_GUARD.encode()).hexdigest()}.sh"
    descriptor, temporary = tempfile.mkstemp(prefix=".guard-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(_GUARD)
            output.flush()
            # The job runs as an unprivileged, different UID. Creation modes
            # alone are reduced by the host's umask (often 0077).
            os.fchmod(output.fileno(), 0o444)
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            # diagnostic-expected: concurrent generations share identical content.
            pass
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r") as installed:
            if (
                not stat.S_ISREG(os.fstat(installed.fileno()).st_mode)
                or installed.read() != _GUARD
            ):
                raise MemoryEnforcementUnavailable(
                    "memory guard integrity check failed"
                )
            os.fchmod(installed.fileno(), 0o444)
    finally:
        os.unlink(temporary)
    return path


@dataclass
class MemoryLaunchGuard:
    job_id: str
    policy: EffectiveMemoryPolicy
    guard_path: Path
    on_ready: Callable[[MemoryEnforcementEvidence], Awaitable[None]]
    on_finished: Callable[[MemoryEnforcementEvidence], Awaitable[None]]
    on_unstarted_cleanup: Callable[[str, str], Awaitable[None]]
    on_groups_created: Callable[[MemoryLaunchGroups], Awaitable[None]] | None = None
    nonce: str = field(default_factory=lambda: secrets.token_hex(24))
    ready_evidence: MemoryEnforcementEvidence | None = None
    finalized: bool = False
    creation_settled: bool = True
    last_sample: MemoryEnforcementEvidence | None = None
    egress_lease: Any = None
    container_id: str | None = None
    auxiliary_group_ids: list[str] = field(default_factory=list)

    async def prepare(self, runner, argv: list[str], group_id: str) -> list[str]:
        # Split creation from start. An interrupted `run` client can otherwise
        # create and start a container after an absence check has freed capacity.
        index = len(runner._runtime_argv())
        if argv[index] != "run":
            raise MemoryEnforcementUnavailable("unexpected managed runner operation")
        if self.policy.runner_overhead_bytes:
            helper_name = getattr(self.egress_lease, "helper_name", None)
            if helper_name is None:
                raise MemoryEnforcementUnavailable(
                    "runner did not identify its charged helper"
                )
            document, _error, code = await runner._capture(
                "inspect", helper_name, "--format", "{{json .}}"
            )
            helper = json.loads(document) if code == 0 else {}
            config = helper.get("HostConfig", {})
            identifier = helper.get("Id", helper.get("ID", ""))
            if (
                config.get("Memory") != self.policy.runner_overhead_bytes
                or config.get("MemorySwap") != self.policy.runner_overhead_bytes
                or not re.fullmatch(r"[a-f0-9]{64}", identifier)
            ):
                raise MemoryEnforcementUnavailable(
                    "runner helper memory or explicit zero-swap boundary could not be verified"
                )
            self.auxiliary_group_ids = [identifier]
        self.creation_settled = False
        output, error, code = await runner._capture("create", *argv[index + 1 :])
        self.creation_settled = True
        if code:
            raise MemoryEnforcementUnavailable(
                "Memory-managed job could not be created: " + error[-1000:]
            )
        if not re.fullmatch(r"[a-f0-9]{64}", output):
            raise MemoryEnforcementUnavailable(
                "runner did not return an immutable container identity"
            )
        self.container_id = output
        if self.on_groups_created is not None:
            await self.on_groups_created(
                MemoryLaunchGroups(
                    group_id=output, auxiliary_group_ids=self.auxiliary_group_ids
                )
            )
        return [
            *runner._runtime_argv(),
            "start",
            "--attach",
            "--interactive",
            self.container_id,
        ]

    def wrap_argv(self, argv: list[str], request, group_id: str) -> list[str]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", self.job_id):
            raise MemoryEnforcementUnavailable("memory job identity is invalid")
        limits = self.policy.limits
        if limits.high_bytes is not None:
            raise MemoryEnforcementUnavailable(
                "OCI runner cannot verify memory.high application"
            )
        image_index = len(argv) - len(request.command) - 1
        prefix = [
            item
            for item in argv[:image_index]
            if item != "--rm" and not item.startswith(("--memory=", "--memory-swap="))
        ]
        prefix.extend(
            [
                f"--memory={limits.max_bytes}",
                f"--memory-swap={limits.max_bytes + limits.swap_bytes}",
                "--cgroupns=private",
                "--entrypoint=/bin/sh",
                f"--label=nebula.memory.job={self.job_id}",
                f"--mount=type=bind,src={self.guard_path.resolve()},dst=/nebula-memory-guard.sh,readonly=true",
            ]
        )
        return [
            *prefix,
            request.image,
            "/nebula-memory-guard.sh",
            self.job_id,
            self.nonce,
            str(limits.max_bytes),
            str(limits.swap_bytes),
            "none",
            *request.command,
        ]

    async def release(self, process, group_id: str) -> None:
        group_id = self.container_id or group_id
        if process.stdout is None or process.stdin is None:
            raise MemoryEnforcementUnavailable("runner has no admission barrier")
        line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
        if len(line) > 2048:
            raise MemoryEnforcementUnavailable("runner handshake exceeded its bound")
        try:
            report = json.loads(line)
        except (ValueError, UnicodeError) as error:
            # diagnostic-expected: missing kernel proof never releases computation.
            raise MemoryEnforcementUnavailable(
                "runner did not verify kernel limits; workload was not released"
            ) from error
        limits = self.policy.limits
        if report != {
            "protocol": "nebula.memory-runner/v1",
            "job_id": self.job_id,
            "nonce": self.nonce,
            "max_bytes": limits.max_bytes,
            "swap_bytes": limits.swap_bytes,
            "protected_process_tree": True,
        }:
            raise MemoryEnforcementUnavailable(
                "runner kernel limits differ from the reserved job"
            )
        proof = MemoryEnforcementEvidence(
            job_id=self.job_id,
            group_id=group_id,
            applied=limits,
            protected_process_tree=True,
            termination_reason="running",
            auxiliary_group_ids=self.auxiliary_group_ids,
            detail="Kernel limits verified before workload release.",
        )
        self.ready_evidence = proof
        await self.on_ready(proof)
        process.stdin.write(self.nonce.encode() + b"\n")
        await process.stdin.drain()

    async def finalize(
        self,
        runner,
        group_id: str,
        reason: MemoryTerminationReason,
        *,
        before_release=None,
    ) -> MemoryEnforcementEvidence | None:
        group_id = self.container_id or group_id
        if self.finalized:
            return None
        output, error, code = await runner._capture(
            "inspect", group_id, "--format", "{{json .}}"
        )
        if code:
            if (
                "no such" not in (output + error).lower()
                or self.ready_evidence is not None
                or not self.creation_settled
            ):
                raise MemoryEnforcementUnavailable(
                    "cleanup evidence is unavailable; memory remains reserved for reconciliation"
                )
            if before_release is not None:
                await before_release()
            await self.on_unstarted_cleanup(
                group_id, "No job container exists; computation was not released."
            )
            self.finalized = True
            return None
        document = json.loads(output)
        if isinstance(document, list):
            document = document[0]
        if document.get("Config", {}).get("Labels", {}).get(
            "nebula.memory.job"
        ) != self.job_id or (
            self.container_id is not None
            and document.get("Id", document.get("ID")) != self.container_id
        ):
            raise MemoryEnforcementUnavailable(
                "container identity differs from the reserved job"
            )
        oom = document.get("State", {}).get("OOMKilled") is True
        await runner._force_remove(group_id, require_removed=True)
        if before_release is not None:
            await before_release()
        if self.ready_evidence is None:
            await self.on_unstarted_cleanup(
                group_id, "Unreleased container and descendants were removed."
            )
            self.finalized = True
            return None
        config = document.get("HostConfig", {})
        memory, combined = (
            int(config.get("Memory", 0)),
            int(config.get("MemorySwap", 0)),
        )
        if memory < 1 or combined < memory:
            raise MemoryEnforcementUnavailable(
                "final memory limits could not be verified"
            )
        proof = MemoryEnforcementEvidence(
            job_id=self.job_id,
            group_id=group_id,
            applied=MemoryLimits(max_bytes=memory, swap_bytes=combined - memory),
            protected_process_tree=True,
            oom_kill_count=1 if oom else 0,
            oom_victim_confirmed=oom,
            group_empty=True,
            auxiliary_group_ids=self.auxiliary_group_ids,
            auxiliary_groups_empty=True,
            peak_bytes=self.last_sample.peak_bytes if self.last_sample else None,
            current_bytes=0,
            observed_at=utc_now().isoformat(),
            termination_reason="memory_limit_exceeded" if oom else reason,
            detail="Kernel reported a memory-limit kill; descendants removed."
            if oom
            else "Container and descendants removed.",
        )
        await self.on_finished(proof)
        self.finalized = True
        return proof
