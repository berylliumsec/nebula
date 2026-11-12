"""Bounded account observations retain project ownership checks."""

from __future__ import annotations

import asyncio

from .async_utils import finish_cleanup
from .execution_scope import current_execution_lease
from .memory_runtime import managed_compute
from .runtime_coordination import LeaseLost


async def _bounded_read(operation):
    # diagnostic-expected: this owned task is cancelled and joined on every
    # exit; vendor exceptions are sanitized by RuntimeUsageService.
    task = asyncio.create_task(operation, name="nebula-harness-account-read")
    try:
        async with asyncio.timeout(45):
            return await asyncio.shield(task)
    finally:
        if not task.done():
            task.cancel()

        async def join():
            await asyncio.gather(task, return_exceptions=True)

        # Cancellation cannot detach a managed reader's process cleanup or
        # imply verified cleanup of a reservation that remains uncertain.
        await finish_cleanup(join())


async def guarded_harness_account_read(
    profile,
    credentials,
    *,
    controller=None,
    adapter_factory=None,
    project_id=None,
):
    """Read account usage with an optional managed adapter and owned cleanup."""
    from .harnesses import HarnessRuntimeService

    service = getattr(controller, "service", None)
    lease = getattr(service, "lease", None)

    async def check_fence():
        if lease is not None:
            if current_execution_lease() != lease or project_id != lease.project_id:
                raise LeaseLost("Account reads require the bound project task")
            async with service.coordination.fenced(lease):
                pass

    await check_fence()
    factory = adapter_factory or HarnessRuntimeService._default_adapter
    adapter = factory(profile.kind)
    reader = getattr(adapter, "read_account_usage_memory_managed", None)
    if callable(reader) and controller is not None:
        with managed_compute(project_id, "harness_account", profile.id):
            result = await _bounded_read(reader(profile, credentials, controller))
    else:
        result = await _bounded_read(adapter.read_account_usage(profile, credentials))
    await check_fence()
    return result
