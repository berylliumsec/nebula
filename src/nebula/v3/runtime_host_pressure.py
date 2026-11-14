"""Current host admission observations over signed exact project grants."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import time

from fastapi import HTTPException
from pydantic import ValidationError

from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .execution_scope import current_execution_lease
from .host_load import HostLoadGate, HostLoadSnapshot
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from .runtime_ipc import RuntimeIpcError
from .shared_memory_rpc import MemoryAuthority

HOST_PRESSURE = "host.pressure/v1"


def register_host_pressure(rpc, runtime_id, coordination, gate):
    async def snapshot(payload):
        try:
            lease = ProjectLease(**MemoryAuthority.model_validate(payload).model_dump())
            if (
                lease.runtime_id != runtime_id
                or lease.project_id == GLOBAL_EXECUTION_PROJECT
            ):
                raise LeaseLost(
                    "Host pressure requires this worker's exact project grant"
                )
            async with coordination.fenced(lease):
                pass
            sample = await gate.snapshot()
            async with coordination.fenced(lease):
                return {"lease": asdict(lease), "sample": asdict(sample)}
        except (ValueError, ValidationError, CoordinationConflict):
            raise HTTPException(
                403, "Host pressure is unavailable for this project grant"
            ) from None

    rpc.register(HOST_PRESSURE, snapshot)


class RemoteHostLoadGate(HostLoadGate):
    def __init__(self, client):
        super().__init__()
        self.client = client
        self._cached_sample = None
        self._cached_lease = None
        self._cached_at = 0.0
        self._observation: asyncio.Future | None = None
        self._observation_lease = None

    async def snapshot(self):
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost(
                "Host pressure requires the current project execution context"
            )
        if self._cached_lease == lease and time.monotonic() - self._cached_at < 1:
            return self._cached_sample
        # One host observation is in flight at a time, and every caller waiting
        # for it shares its outcome. While the host is being replaced, callers
        # queued behind it therefore fail or succeed together within one
        # reconnect window, instead of each starting a window of its own.
        observation = self._observation
        if (
            observation is None
            or observation.done()
            or self._observation_lease != lease
        ):
            # diagnostic-expected: every waiter receives this result or error; the callback only marks it retrieved when all waiters were cancelled.
            observation = asyncio.ensure_future(self._observe(lease))
            observation.add_done_callback(
                lambda done: done.cancelled() or done.exception()
            )
            self._observation, self._observation_lease = observation, lease
        return await asyncio.shield(observation)

    async def _observe(self, lease):
        result = await self.client.call(HOST_PRESSURE, asdict(lease))
        if not isinstance(result, dict) or result.get("lease") != asdict(lease):
            raise RuntimeIpcError("Invalid host pressure observation")
        try:
            sample = HostLoadSnapshot(**result["sample"])
        except (KeyError, TypeError):
            raise RuntimeIpcError("Invalid host pressure observation") from None
        self._cached_sample, self._cached_lease = sample, lease
        self._cached_at = time.monotonic()
        return sample
