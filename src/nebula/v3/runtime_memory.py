"""Account for resident runtime processes inside the Core/services reserve.

This is admission accounting for essential Core processes, not per-job kernel
enforcement. Managed compute still requires its independently verified group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from .database import RuntimeProcessRow, RuntimeReleaseRow
from .deployment_protocol import ReleaseManifest
from .memory_policy import MemoryBudgetError, MemoryBudgetPolicy, validate_policy

if TYPE_CHECKING:
    from .memory_budget import MemoryBudgetService


@dataclass(frozen=True)
class RuntimeMemoryFootprint:
    process_count: int
    reserved_bytes: int
    minimum_core_reserve_bytes: int


async def runtime_memory_footprint(
    session, *, services_floor: int
) -> RuntimeMemoryFootprint:
    processes = list(
        await session.scalars(
            select(RuntimeProcessRow).where(
                RuntimeProcessRow.state != "retired",
            )
        )
    )
    manifests = {}
    reserved = 0
    for process in processes:
        if process.version not in manifests:
            release = await session.get(RuntimeReleaseRow, process.version)
            if release is None:
                raise MemoryBudgetError(
                    "Retained runtime metadata is missing; reconcile deployment before changing the Core reserve"
                )
            manifests[process.version] = ReleaseManifest.model_validate(
                release.manifest
            )
        reserved += manifests[process.version].execution_reserve_bytes
    return RuntimeMemoryFootprint(len(processes), reserved, services_floor + reserved)


class RuntimeMemoryAdmission:
    """Reserve-worker checks share the policy lock used by save/job admission."""

    def __init__(self, budgets: MemoryBudgetService):
        self.budgets = budgets

    async def check(self, session, manifest: ReleaseManifest, role: str):
        if role not in {"api", "execution"}:
            raise MemoryBudgetError(
                "Unknown runtime role cannot reserve service memory"
            )
        row = await self.budgets._policy(session)
        administrator = self.budgets.administrator()
        policy = MemoryBudgetPolicy.model_validate(row.policy)
        validate_policy(policy, administrator)
        current = await runtime_memory_footprint(
            session, services_floor=administrator.minimum_reserve_bytes
        )
        required = current.minimum_core_reserve_bytes + manifest.execution_reserve_bytes
        if required > policy.reserve_bytes:
            raise MemoryBudgetError(
                f"Core reserve needs at least {required} bytes for essential services and this resident-runtime overlap; "
                f"the saved reserve is {policy.reserve_bytes} bytes. Increase Core reserve in Memory budgets or retire an inactive runtime."
            )

    async def __call__(self, manifest: ReleaseManifest, role: str):
        # A useful preflight before expensive verification or process creation.
        # DeploymentCoordinator repeats check inside atomic reservation itself.
        async with self.budgets._write() as session:
            await self.check(session, manifest, role)
