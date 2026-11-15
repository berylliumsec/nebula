"""Optimistic checkpoint validation without holding an ownership lease lock.

The migration's triggers advance these counters in the same transaction as
every checkpoint input change, including changes made by retained releases.
A missing counter means zero; counters are never deleted on project removal.
"""

from sqlalchemy import BigInteger, String, column, select, table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .runtime_coordination import CoordinationConflict


_generations = table(
    "project_checkpoint_generations",
    column("project_id", String(200)),
    column("generation", BigInteger),
)


async def read_checkpoint_generation(session, project_id: str) -> int:
    """Read the generation before/after a checkpoint's statement snapshot."""
    value = await session.scalar(
        select(_generations.c.generation).where(_generations.c.project_id == project_id)
    )
    return 0 if value is None else int(value)


async def lock_checkpoint_generation(session, project_id: str) -> int:
    """Lock the final generation until checkpoint publication commits.

    The caller must first acquire its normal ownership fence, then compare
    this value with both surrounding scan reads. This ordering matches writers
    that acquire ownership before their input-change triggers run. SQLite's
    caller must already hold BEGIN IMMEDIATE, as normal fenced writes do.
    """
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    await session.execute(
        insert(_generations)
        .values(project_id=project_id, generation=0)
        .on_conflict_do_nothing(index_elements=["project_id"])
    )
    value = await session.scalar(
        select(_generations.c.generation)
        .where(_generations.c.project_id == project_id)
        .with_for_update()
    )
    assert value is not None
    return int(value)


async def assert_checkpoint_generation(session, project_id: str, expected: int) -> None:
    """Keep the scanned project stable through the caller's fenced commit."""
    if await lock_checkpoint_generation(session, project_id) != expected:
        raise CoordinationConflict(
            "Project state changed during checkpoint; retry with a fresh snapshot"
        )
