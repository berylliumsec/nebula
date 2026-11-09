"""Compatibility memory import preserves source and holds partial admissions."""

import asyncio
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from nebula.v3.butler_memory import ButlerMemoryService
from nebula.v3.database import (
    BootstrapStateRow,
    ButlerMemoryCursorRow,
    ButlerMemoryItemRow,
    Database,
    RuntimeInstanceRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import SHARED_MEMORY_BOOTSTRAP_KEY
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.shared_butler_memory import SharedButlerMemory
from nebula.v3.shared_memory_import import CompatibilityMemoryImport
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def legacy(managed):
    memory = ButlerMemoryService(managed.data / "butler-memory.sqlite3")
    rows = []
    for index in range(3):
        rows.append(
            await memory._save(
                source_project_id="admitted-project",
                scope="global_lesson" if index == 0 else "project_lesson",
                content=f"Check saved evidence before finalizing synthetic review number {index}.",
                source_kind="verified_core_result",
                source_label="Verified synthetic result",
                source_ref=f"synthetic-{index}",
            )
        )
    await memory._advance_event_cursor("admitted-project", 17)
    return memory, rows


async def marker(managed):
    async with managed.database.session() as session:
        row = await session.get(BootstrapStateRow, SHARED_MEMORY_BOOTSTRAP_KEY)
        return row.status if row else None


async def count(managed):
    async with managed.database.session() as session:
        return await session.scalar(
            select(func.count()).select_from(ButlerMemoryItemRow)
        )


async def reserve(coordinator):
    return await coordinator.reserve_worker(
        "v1",
        role="execution",
        endpoint="unix:///isolated/" + uuid4().hex,
        credential_reference=str(uuid4()),
    )


async def test_fresh_host_records_empty_import_without_creating_a_compatibility_file(
    managed,
):
    result = await CompatibilityMemoryImport(managed.host).run()
    assert result == {"state": "empty", "items": 0, "cursors": 0}
    assert await marker(managed) == "complete"
    assert not (managed.data / "butler-memory.sqlite3").exists()


async def test_import_preserves_memories_revisions_cursors_and_the_original_file(
    managed,
):
    memory, rows = await legacy(managed)
    edited = await memory.update(
        "admitted-project",
        rows[1].id,
        "Keep the selected project's reviewed evidence before publishing.",
        expected_revision=1,
    )
    before = hashlib.sha256(memory.path.read_bytes()).hexdigest()
    result = await CompatibilityMemoryImport(managed.host).run()
    assert result == {"state": "imported", "items": 3, "cursors": 1}
    assert hashlib.sha256(memory.path.read_bytes()).hexdigest() == before
    assert len((await memory.list("admitted-project")).items) == 3
    async with managed.database.session() as session:
        saved = await session.get(ButlerMemoryItemRow, edited.id)
        assert (
            saved.revision == 2
            and saved.content == edited.content
            and saved.source_ref == edited.source_ref
        )
        assert (
            await session.get(ButlerMemoryCursorRow, "admitted-project")
        ).last_event_sequence == 17


async def test_completed_import_never_restores_old_memory_over_new_work(
    managed, monkeypatch
):
    memory, rows = await legacy(managed)
    importer = CompatibilityMemoryImport(managed.host)
    await importer.run()
    coordination = managed.coordinator.coordination
    await coordination.register_runtime("fixture", "inert-release")
    lease = await coordination.claim_project("admitted-project", "fixture")
    shared = SharedButlerMemory(coordination, managed.data / "shared-index")
    with execution_context(lease):
        updated = await shared.update(
            "admitted-project",
            rows[1].id,
            "Keep newly committed evidence after the API update finishes.",
            expected_revision=1,
        )
        await shared._advance_event_cursor("admitted-project", 24)
    monkeypatch.setattr(
        importer,
        "_memory",
        lambda row: pytest.fail("Completed migration must not reread old memory"),
    )
    assert await importer.run() == {"state": "already_imported"}
    async with managed.database.session() as session:
        assert (
            await session.get(ButlerMemoryItemRow, updated.id)
        ).content == updated.content
        assert (
            await session.get(ButlerMemoryCursorRow, "admitted-project")
        ).last_event_sequence == 24
    assert (await memory.list("admitted-project")).items[0].revision == 1


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_partial_import_holds_runtime_admission_and_resumes_exact_checkpoints(
    managed, monkeypatch, failure
):
    await legacy(managed)
    await managed.stage("v1")
    monkeypatch.setattr("nebula.v3.shared_memory_import._BATCH", 1)
    importer = CompatibilityMemoryImport(managed.host)
    original = importer._copy_memories
    committed = asyncio.Event()

    async def interrupt(batch):
        await original(batch)
        committed.set()
        if failure == "error":
            raise RuntimeError("Synthetic candidate detail must not escape")
        await asyncio.Event().wait()

    monkeypatch.setattr(importer, "_copy_memories", interrupt)
    task = asyncio.create_task(importer.run())
    await asyncio.wait_for(committed.wait(), 3)
    if failure == "cancel":
        task.cancel()
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else RuntimeIpcError
    ):
        await task
    assert await marker(managed) == "blocked" and await count(managed) == 1
    with pytest.raises(CoordinationConflict, match="import is incomplete"):
        await reserve(managed.coordinator)
    monkeypatch.setattr(importer, "_copy_memories", original)
    assert (await importer.run())["items"] == 3
    assert await count(managed) == 3 and await marker(managed) == "complete"
    assert await reserve(managed.coordinator)


async def test_changed_source_cannot_overwrite_a_committed_import_checkpoint(
    managed, monkeypatch
):
    memory, _ = await legacy(managed)
    monkeypatch.setattr("nebula.v3.shared_memory_import._BATCH", 1)
    importer = CompatibilityMemoryImport(managed.host)
    original = importer._copy_memories

    async def interrupt(batch):
        await original(batch)
        raise RuntimeError("Synthetic interrupted import")

    monkeypatch.setattr(importer, "_copy_memories", interrupt)
    with pytest.raises(RuntimeIpcError):
        await importer.run()
    async with managed.database.session() as session:
        saved = await session.scalar(select(ButlerMemoryItemRow))
        identity, prior = saved.id, saved.content
    await memory.update(
        "admitted-project",
        identity,
        "This source changed after its import checkpoint was committed.",
        expected_revision=1,
    )
    monkeypatch.setattr(importer, "_copy_memories", original)
    with pytest.raises(RuntimeIpcError, match="original file is preserved"):
        await importer.run()
    assert await marker(managed) == "blocked"
    async with managed.database.session() as session:
        assert (await session.get(ButlerMemoryItemRow, identity)).content == prior


@pytest.mark.parametrize("kind", ["symlink", "permissions", "corrupt"])
async def test_unsafe_or_invalid_compatibility_source_blocks_without_raw_data_in_errors(
    managed, kind
):
    memory, _ = await legacy(managed)
    if kind == "symlink":
        original = memory.path.with_name("preserved-memory.sqlite3")
        memory.path.rename(original)
        memory.path.symlink_to(original)
    elif kind == "permissions":
        memory.path.chmod(0o640)
    else:
        memory.path.write_bytes(b"synthetic-invalid-memory-secret")
    with pytest.raises(RuntimeIpcError) as rejected:
        await CompatibilityMemoryImport(managed.host).run()
    assert "synthetic-invalid-memory-secret" not in str(rejected.value)
    assert await marker(managed) == "blocked" and await count(managed) == 0
    assert memory.path.exists()


async def test_import_requires_retirement_even_when_an_execution_lease_has_expired(
    managed,
):
    await managed.stage("v1")
    identity = await managed.host.spawn("v1", "execution")
    importer = CompatibilityMemoryImport(managed.host)
    with pytest.raises(CoordinationConflict, match="coordinated shutdown"):
        await importer.run()
    async with managed.database.session() as session:
        runtime = await session.get(RuntimeInstanceRow, identity.runtime_id)
        runtime.lease_expires_at_ms = 0
    with pytest.raises(CoordinationConflict, match="coordinated shutdown"):
        await importer.run()
    assert await marker(managed) is None
    await managed.host.retire(identity.runtime_id)
    assert (await importer.run())["state"] == "empty"


async def test_import_requires_the_active_supervisor_lock(managed):
    await managed.host.close()
    with pytest.raises(RuntimeError, match="exclusive host ownership"):
        await CompatibilityMemoryImport(managed.host).run()
    assert await marker(managed) is None


async def test_another_database_pool_cannot_admit_during_an_import(
    managed, monkeypatch
):
    await legacy(managed)
    await managed.stage("v1")
    importer = CompatibilityMemoryImport(managed.host)
    original = importer._copy_memories
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def held(batch):
        entered.set()
        await proceed.wait()
        await original(batch)

    monkeypatch.setattr(importer, "_copy_memories", held)
    other_database = Database(
        managed.database.url, bootstrap=False, pool_settings=PoolSettings(5, 12, 2, 5)
    )
    other = DeploymentCoordinator(other_database)
    task = asyncio.create_task(importer.run())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(CoordinationConflict, match="import is incomplete"):
            await reserve(other)
        proceed.set()
        await task
        assert await reserve(other)
    finally:
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)
        await other_database.dispose()
