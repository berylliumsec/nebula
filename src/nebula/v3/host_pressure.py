"""Revisioned host-local pressure settings, applied without restarting Core."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
from uuid import uuid4

from pydantic import Field

from .domain import NebulaModel
from .host_load import HostLoadGate, HostPressureThresholds


class HostPressurePolicy(NebulaModel):
    host_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    thresholds: HostPressureThresholds = Field(default_factory=HostPressureThresholds)


class HostPressureUpdate(NebulaModel):
    host_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    thresholds: HostPressureThresholds


class HostPressureConflict(ValueError):
    pass


class HostPressureSettings:
    def __init__(self, path: Path, gate: HostLoadGate):
        self.path, self.gate = path, gate
        self._lock = asyncio.Lock()
        self._policy: HostPressurePolicy | None = None

    def _write(self, policy: HostPressurePolicy) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(
            prefix=".host-pressure-", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(policy.model_dump_json() + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self) -> HostPressurePolicy:
        if self.path.exists():
            return HostPressurePolicy.model_validate(json.loads(self.path.read_text()))
        policy = HostPressurePolicy(host_id=uuid4().hex, revision=1)
        self._write(policy)
        return policy

    def _apply(self, policy: HostPressurePolicy) -> None:
        self._policy = policy
        self.gate.thresholds = policy.thresholds
        self.gate.monitor.thresholds = policy.thresholds
        self.gate.wake()

    async def startup(self) -> None:
        async with self._lock:
            self._apply(await asyncio.to_thread(self._read))

    async def status(self) -> dict[str, object]:
        async with self._lock:
            if self._policy is None:
                self._apply(await asyncio.to_thread(self._read))
            assert self._policy is not None
            sample = await self.gate.snapshot()
            return {
                "host_id": self._policy.host_id,
                "host_name": socket.gethostname(),
                "revision": self._policy.revision,
                "saved_thresholds": self._policy.thresholds.model_dump(),
                "effective_thresholds": self.gate.thresholds.model_dump(),
                "readings": sample.readings(),
                "pressure_metric": sample.pressure_metric,
                "waiting": sample.busy,
                "reason": sample.reason,
                "sample": sample.public(),
            }

    async def update(self, request: HostPressureUpdate) -> None:
        async with self._lock:
            policy = await asyncio.to_thread(self._read)
            if (
                policy.host_id != request.host_id
                or policy.revision != request.expected_revision
            ):
                raise HostPressureConflict(
                    "Host pressure settings changed. Reload before saving."
                )
            saved = HostPressurePolicy(
                host_id=policy.host_id,
                revision=policy.revision + 1,
                thresholds=request.thresholds,
            )
            await asyncio.to_thread(self._write, saved)
            self._apply(saved)
