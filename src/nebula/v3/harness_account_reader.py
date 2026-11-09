"""Account observations share the protected host/project launch boundary."""

from __future__ import annotations

import asyncio

from .async_utils import finish_cleanup
from .domain import HarnessConnectionMode, HarnessKind
from .execution_scope import current_execution_lease
from .memory_policy import MemoryEnforcementUnavailable
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
    """Inspect an endpoint, or require a managed native reader with owned cleanup."""
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
    if (
        profile.kind == HarnessKind.CODEX_APP_SERVER
        and profile.connection_mode == HarnessConnectionMode.ENDPOINT
    ):
        # Connecting to an existing account endpoint starts no native process.
        result = await _bounded_read(adapter.read_account_usage(profile, credentials))
    else:
        if controller is None:
            raise MemoryEnforcementUnavailable(
                "Harness account refresh requires an initialized protected memory-runner/v1 reader. Connect a supported account endpoint or reconnect Core, then retry. No account process was started."
            )
        reader = getattr(adapter, "read_account_usage_memory_managed", None)
        if not callable(reader):
            await controller.reject_unsupported(
                "local",
                project_id=project_id,
                resource_kind="harness_account",
                resource_id=profile.id,
                detail="This harness account reader has no protected memory-runner/v1 launch contract. Connect a supported account endpoint or memory-managed harness reader, then retry. No account process was started.",
            )
        with managed_compute(project_id, "harness_account", profile.id):
            result = await _bounded_read(reader(profile, credentials, controller))
    await check_fence()
    return result
