import asyncio
import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select

from nebula.v3.database import (
    EntityRow,
    WorkspaceProvenanceBlobRow,
    WorkspaceProvenanceIndexRow,
    WorkspaceProvenanceOwnerRow,
)
from nebula.v3.domain import ChatSession, Engagement, WorkspaceProvenanceObservation
from nebula.v3.storage import NebulaStore
from nebula.v3 import workspace_provenance
from nebula.v3.workspace_provenance import (
    PROVENANCE_SCHEMA,
    WorkspaceProvenanceService,
    actor_id_for,
    snapshot,
)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Nebula Test")
    _git(root, "config", "user.email", "nebula@example.invalid")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "baseline")
    return root


async def _service(tmp_path: Path) -> tuple[WorkspaceProvenanceService, NebulaStore]:
    store = NebulaStore(tmp_path / "core.db")
    (await store.create(Engagement(id="eng", name="Project")))
    return WorkspaceProvenanceService(store), store


async def _begin(
    service: WorkspaceProvenanceService,
    root: Path,
    *,
    scope: str,
    actor: str,
):
    return await service.begin(
        root,
        engagement_id="eng",
        scope_kind="turn",
        scope_id=scope,
        actor_id=actor,
        owner_kind="chat",
        owner_id=actor,
        chat_session_id=None,
        chat_turn_id=scope,
    )


async def test_provenance_attributes_only_state_changed_after_the_actor_baseline(
    tmp_path,
):
    root = _repository(tmp_path)
    (root / "preexisting.txt").write_text("someone else\n", encoding="utf-8")
    service, _ = await _service(tmp_path)

    started = await _begin(service, root, scope="turn-a", actor="chat:a")
    (root / "owned.txt").write_text("mine\n", encoding="utf-8")
    completed = await service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-a"
    )

    assert started.status == "active"
    assert completed.status == "complete"
    assert completed.confidence == "exact"
    assert [item["path"] for item in completed.attribution["owned"]] == ["owned.txt"]
    assert [item["path"] for item in completed.attribution["preexisting"]] == [
        "preexisting.txt"
    ]
    assert service.receipt(completed)["schema"] == PROVENANCE_SCHEMA
    assert service.receipt(completed)["current_paths"] == [
        "owned.txt",
        "preexisting.txt",
    ]


async def test_provenance_keeps_prior_actor_ownership_across_later_turns(tmp_path):
    root = _repository(tmp_path)
    service, _ = await _service(tmp_path)
    (await _begin(service, root, scope="turn-a", actor="chat:a"))
    (root / "shared.txt").write_text("from a\n", encoding="utf-8")
    (
        await service.finish(
            root, engagement_id="eng", scope_kind="turn", scope_id="turn-a"
        )
    )

    (await _begin(service, root, scope="turn-b", actor="chat:b"))
    completed = await service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-b"
    )

    assert completed.attribution["owned"] == []
    assert [
        (item["path"], item["actor_id"]) for item in completed.attribution["other"]
    ] == [("shared.txt", "chat:a")]


async def test_prior_actor_index_preserves_an_earlier_matching_mutation(tmp_path):
    root = _repository(tmp_path)
    service, _ = await _service(tmp_path)
    (await _begin(service, root, scope="turn-a", actor="chat:a"))
    (root / "shared.txt").write_text("from a\n", encoding="utf-8")
    (
        await service.finish(
            root, engagement_id="eng", scope_kind="turn", scope_id="turn-a"
        )
    )
    (await _begin(service, root, scope="turn-b", actor="chat:b"))
    (root / "shared.txt").write_text("from b\n", encoding="utf-8")
    (
        await service.finish(
            root, engagement_id="eng", scope_kind="turn", scope_id="turn-b"
        )
    )

    # An unobserved restore makes the state equal to A's older mutation. The
    # indexed state match still finds A rather than only the latest path owner.
    (root / "shared.txt").write_text("from a\n", encoding="utf-8")
    (await _begin(service, root, scope="turn-c", actor="chat:c"))
    completed = await service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-c"
    )

    assert [
        (row["path"], row["actor_id"]) for row in completed.attribution["other"]
    ] == [("shared.txt", "chat:a")]


async def test_active_observation_survives_store_reopen(tmp_path):
    root = _repository(tmp_path)
    service, _ = await _service(tmp_path)
    (await _begin(service, root, scope="turn-restart", actor="chat:a"))
    (root / "after-restart.txt").write_text("durable\n", encoding="utf-8")

    restarted = WorkspaceProvenanceService(NebulaStore(tmp_path / "core.db"))
    completed = await restarted.finish(
        root,
        engagement_id="eng",
        scope_kind="turn",
        scope_id="turn-restart",
    )

    assert completed.status == "complete"
    assert [item["path"] for item in completed.attribution["owned"]] == [
        "after-restart.txt"
    ]


async def test_concurrent_begin_same_scope_returns_one_durable_observation(
    tmp_path, monkeypatch
):
    service, _ = await _service(tmp_path)
    started = Barrier(2)

    def synthetic_snapshot(workspace):
        started.wait(timeout=5)
        return workspace_provenance._Snapshot(True, str(workspace), {})

    monkeypatch.setattr(workspace_provenance, "snapshot", synthetic_snapshot)

    async def begin(root):
        return await _begin(service, Path(root), scope="same-scope", actor="chat:a")

    first, second = list(
        (
            await asyncio.gather(
                *(begin(_item) for _item in ("/synthetic/one", "/synthetic/two"))
            )
        )
    )
    assert first.id == second.id
    assert first.workspace_root == second.workspace_root


async def test_parallel_actors_are_uncertain_instead_of_misattributed(tmp_path):
    root = _repository(tmp_path)
    service, _ = await _service(tmp_path)
    (await _begin(service, root, scope="turn-a", actor="chat:a"))
    (await _begin(service, root, scope="turn-b", actor="chat:b"))
    (root / "parallel.txt").write_text("ambiguous\n", encoding="utf-8")

    completed = await service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-a"
    )

    assert completed.confidence == "uncertain_parallel_turns"
    assert completed.concurrent_scope_ids == ["turn-b"]
    assert [item["path"] for item in completed.attribution["uncertain"]] == [
        "parallel.txt"
    ]
    assert completed.attribution["owned"] == []


async def test_non_git_workspace_is_explicitly_unsupported_and_subagent_id_is_stable(
    tmp_path,
):
    service, store = await _service(tmp_path)
    (
        await store.create(
            ChatSession(
                id="child-chat",
                engagement_id="eng",
                title="Child",
                provider_profile_id="provider",
                model="model",
                metadata={"subagent_id": "child-agent"},
            )
        )
    )
    root = tmp_path / "folder"
    root.mkdir()

    assert snapshot(root).reason == "workspace_is_not_a_git_checkout"
    assert (
        await actor_id_for(
            store,
            owner_kind="chat",
            owner_id="child-chat",
            chat_session_id="child-chat",
        )
    ) == "subagent:child-agent"
    started = await _begin(
        service, root, scope="turn-child", actor="subagent:child-agent"
    )
    completed = await service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-child"
    )
    assert not started.supported
    assert completed.confidence == "unsupported"
    assert completed.unsupported_reason == "workspace_is_not_a_git_checkout"


async def test_large_dirty_checkout_has_no_path_byte_or_mutation_ceiling(tmp_path):
    root = _repository(tmp_path)
    large = root / "large.bin"
    with large.open("wb") as handle:
        handle.truncate(257 * 1024 * 1024)
    service, _ = await _service(tmp_path)
    started = await _begin(service, root, scope="large-turn", actor="chat:large")
    for index in range(2_001):
        (root / f"created-{index:04d}.txt").write_text("owned\n", encoding="utf-8")

    completed = await service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="large-turn"
    )

    assert started.supported
    assert completed.supported
    assert len(completed.mutations) == 2_001
    assert len(completed.attribution["owned"]) == 2_001
    assert completed.attribution["preexisting"][0]["path"] == "large.bin"


async def test_large_unchanged_checkout_shares_chunks_and_queries_only_compact_index(
    tmp_path, monkeypatch
):
    service, store = await _service(tmp_path)
    paths = {
        f"source/file-{index:05d}.txt": {
            "status": " M",
            "kind": "file",
            "sha256": f"{index:064x}",
            "size": 1,
        }
        for index in range(15_313)
    }

    def synthetic_snapshot(_workspace):
        return workspace_provenance._Snapshot(True, "/synthetic/checkout", dict(paths))

    monkeypatch.setattr(workspace_provenance, "snapshot", synthetic_snapshot)

    def no_historical_full_rows(*_args, **_kwargs):
        raise AssertionError("attribution loaded historical full entity rows")

    monkeypatch.setattr(store, "find_entities", no_historical_full_rows)
    (await _begin(service, Path("/synthetic/checkout"), scope="first", actor="chat:a"))
    first = await service.finish(
        Path("/synthetic/checkout"),
        engagement_id="eng",
        scope_kind="turn",
        scope_id="first",
    )
    assert first.supported
    assert len(first.attribution["preexisting"]) == len(paths)
    async with store.database.session() as session:
        first_blobs = list(session.query(WorkspaceProvenanceBlobRow))
        first_bytes = sum(len(str(row.payload)) for row in first_blobs)
        stored = await session.get(EntityRow, first.id)
        assert stored is not None
        assert stored.payload["baseline"] == {}
        assert stored.payload["current"] == {}
        assert stored.payload["attribution"] == {}

    (await _begin(service, Path("/synthetic/checkout"), scope="second", actor="chat:b"))
    paths["source/file-00000.txt"] = {
        **paths["source/file-00000.txt"],
        "sha256": "f" * 64,
    }
    second = await service.finish(
        Path("/synthetic/checkout"),
        engagement_id="eng",
        scope_kind="turn",
        scope_id="second",
    )
    assert second.supported
    assert [item["path"] for item in second.attribution["owned"]] == [
        "source/file-00000.txt"
    ]
    assert len(second.attribution["preexisting"]) == len(paths) - 1
    async with store.database.session() as session:
        next_blobs = list(session.query(WorkspaceProvenanceBlobRow))
        next_bytes = sum(len(str(row.payload)) for row in next_blobs)
        assert len(next_blobs) - len(first_blobs) <= 4
        assert next_bytes - first_bytes < 80_000
        stored = await session.get(EntityRow, second.id)
        assert stored is not None
        assert len(str(stored.payload)) < 10_000

    reopened = WorkspaceProvenanceService(NebulaStore(tmp_path / "core.db"))
    assert (
        await reopened.get(engagement_id="eng", scope_kind="turn", scope_id="second")
    ).attribution == second.attribution


async def test_old_full_observations_are_not_scanned_after_cutover(
    tmp_path, monkeypatch
):
    root = _repository(tmp_path)
    (root / "old.txt").write_text("already dirty\n", encoding="utf-8")
    service, store = await _service(tmp_path)
    old_state = snapshot(root).paths["old.txt"]
    (
        await store.create(
            WorkspaceProvenanceObservation(
                engagement_id="eng",
                workspace_root=str(root),
                scope_kind="turn",
                scope_id="legacy",
                actor_id="chat:old",
                status="complete",
                completed_at=datetime.now(timezone.utc),
                current={"old.txt": old_state},
                mutations=[
                    {
                        "path": "old.txt",
                        "before": None,
                        "after": old_state,
                        "confidence": "exact",
                    }
                ],
            )
        )
    )

    def no_historical_full_rows(*_args, **_kwargs):
        raise AssertionError("attribution loaded historical full entity rows")

    monkeypatch.setattr(store, "find_entities", no_historical_full_rows)
    (await _begin(service, root, scope="cutover", actor="chat:new"))
    completed = await service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="cutover"
    )
    assert completed.attribution["other"] == []
    assert [item["path"] for item in completed.attribution["preexisting"]] == [
        "old.txt"
    ]


def test_ledger_migration_keeps_old_entities_without_backfill(tmp_path):
    database = tmp_path / "old-core.db"
    engine = create_engine(f"sqlite:///{database}")
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[2] / "src" / "nebula" / "v3" / "migrations"),
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0022_cached_usage_rollup")
    entities = Table("entities", MetaData(), autoload_with=engine)
    old = WorkspaceProvenanceObservation(
        id="old-observation",
        engagement_id="eng",
        workspace_root="/synthetic/checkout",
        scope_kind="turn",
        scope_id="old",
        actor_id="chat:old",
        baseline={"old.txt": {"status": "??", "sha256": "a" * 64}},
    )
    payload = old.model_dump(mode="json")
    for field in (
        "baseline_snapshot_id",
        "current_snapshot_id",
        "attribution_snapshot_id",
    ):
        payload.pop(field)
    with engine.begin() as connection:
        connection.execute(
            entities.insert().values(
                id=old.id,
                kind=old.entity_kind,
                engagement_id=old.engagement_id,
                revision=old.revision,
                payload=payload,
                created_at=old.created_at,
                updated_at=old.updated_at,
            )
        )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert {
        "workspace_provenance_blobs",
        "workspace_provenance_index",
        "workspace_provenance_owners",
    } <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert (
            connection.scalar(select(entities.c.payload).where(entities.c.id == old.id))
            == payload
        )
        assert (
            connection.scalar(
                select(func.count()).select_from(WorkspaceProvenanceIndexRow)
            )
            == 0
        )
    engine.dispose()


async def test_project_delete_removes_provenance_chunks_and_owner_index(tmp_path):
    root = _repository(tmp_path)
    service, store = await _service(tmp_path)
    (await _begin(service, root, scope="delete-project", actor="chat:a"))
    (root / "owned.txt").write_text("mine\n", encoding="utf-8")
    (
        await service.finish(
            root, engagement_id="eng", scope_kind="turn", scope_id="delete-project"
        )
    )
    archived = await store.update(Engagement, "eng", {"status": "archived"})
    (await store.delete_archived_engagement("eng", expected_revision=archived.revision))

    async with store.database.session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(WorkspaceProvenanceBlobRow)
            )
        ) == 0
        assert (
            await session.scalar(
                select(func.count()).select_from(WorkspaceProvenanceIndexRow)
            )
        ) == 0
        assert (
            await session.scalar(
                select(func.count()).select_from(WorkspaceProvenanceOwnerRow)
            )
        ) == 0


pytestmark = pytest.mark.anyio
