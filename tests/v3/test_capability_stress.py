"""Saturation must not amplify relation queries or occupy Core's request loop."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import inspect
from contextlib import suppress
import threading

import httpx
import pytest
from sqlalchemy import event

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import ResourceRelationRow
from nebula.v3.domain import (
    AgentRun,
    Asset,
    Engagement,
    Evidence,
    Finding,
    Observation,
    Report,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.relations import ResourceRelationService


AUTH = {"Authorization": "Bearer synthetic-capability-test"}


async def test_artifact_read_keeps_acknowledgement_integrity_and_authorization(
    tmp_path,
):
    store, artifacts, artifact, app = await _fixture(tmp_path)
    (
        await store.update(
            type(artifact),
            artifact.id,
            {"metadata": {"tool_call_id": "synthetic-call"}},
            expected_revision=artifact.revision,
        )
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://core.test/api/v1"
        ) as client:
            path = f"/artifacts/{artifact.id}/content"
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=AUTH)).status_code == 428
            acknowledged = {**AUTH, "X-Nebula-Sensitive-Data-Acknowledged": "true"}
            good = await client.get(path, headers=acknowledged)
            assert good.status_code == 200
            assert good.headers["X-Nebula-Artifact-SHA256"] == artifact.sha256
            blob = artifacts.path_for(artifact)
            blob.chmod(0o600)
            blob.write_bytes(b"Synthetic corruption")
            damaged = await client.get(path, headers=acknowledged)
            assert damaged.status_code == 409
            assert "integrity verification" in damaged.json()["detail"]

    try:
        (await exercise())
    finally:
        (await store.database.engine.dispose())


async def _fixture(tmp_path):
    store = NebulaStore(tmp_path / "stress.db")
    (
        await store.create_many(
            [
                Engagement(id="project", name="Synthetic project"),
                Observation(
                    id="note",
                    engagement_id="project",
                    observation_type="note",
                    title="Synthetic note λ",
                ),
                AgentRun(
                    id="run",
                    engagement_id="project",
                    objective="Synthetic replay",
                    status="complete",
                ),
            ]
        )
    )
    (await store.append_event("run", "synthetic.progress", {"step": 1}))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    artifact = await store.create(
        artifacts.put_bytes(
            b"Synthetic evidence\n" * 10_000,
            engagement_id="project",
            filename="synthetic.txt",
        )
    )
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="synthetic-capability-test",
        execution_data_root=tmp_path,
    )
    return store, artifacts, artifact, app


@pytest.mark.parametrize(
    "boundary",
    ["overview-global", "overview-project", "event-replay", "artifact-download"],
)
async def test_slow_saved_state_read_leaves_unrelated_settings_responsive(
    tmp_path, monkeypatch, boundary
):
    store, artifacts, artifact, app = await _fixture(tmp_path)
    owner, method, path = {
        "overview-global": (store, "overview", "/overview"),
        "overview-project": (store, "overview", "/engagements/project/overview"),
        "event-replay": (store, "replay_events", "/runs/run/events"),
        "artifact-download": (artifacts, "verify", f"/artifacts/{artifact.id}/content"),
    }[boundary]
    original = getattr(owner, method)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    readers = []

    if inspect.iscoroutinefunction(original):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(*args, **kwargs):
            readers.append(asyncio.current_task())
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=3)
            return await original(*args, **kwargs)
    else:

        def hold(*args, **kwargs):
            readers.append(threading.get_ident())
            entered.set()
            assert threading.get_ident() != loop_thread
            assert release.wait(3), "saved state reader was never released"
            return original(*args, **kwargs)

    monkeypatch.setattr(owner, method, hold)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test/api/v1",
            headers=AUTH,
        ) as client:
            slow = asyncio.create_task(client.get(path))
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.005)
                assert entered.is_set()
                if inspect.iscoroutinefunction(original):
                    assert all(task is not asyncio.current_task() for task in readers)
                else:
                    assert loop_thread not in readers
                fast = await asyncio.wait_for(
                    client.get("/engagements/project/assistant-defaults"), timeout=0.5
                )
                assert fast.status_code == 200, fast.text
                assert not slow.done(), (
                    "diagnostic gate did not hold the expensive read"
                )
                release.set()
                result = await asyncio.wait_for(slow, timeout=3)
                assert result.status_code == 200, result.text
                if boundary == "artifact-download":
                    assert result.content == b"Synthetic evidence\n" * 10_000
                elif boundary == "event-replay":
                    assert [item["sequence"] for item in result.json()["events"]] == [1]
                else:
                    assert result.json()["counts"]["observations"] == 1
            finally:
                release.set()
                if not slow.done():
                    slow.cancel()
                with suppress(asyncio.CancelledError):
                    await slow

    try:
        (await exercise())
    finally:
        (await store.database.engine.dispose())


@pytest.mark.parametrize("kind", ["observations", "findings", "evidence", "reports"])
async def test_list_relation_cost_does_not_grow_with_result_count(tmp_path, kind):
    store = NebulaStore(tmp_path / "projection.db")
    (
        await store.create_many(
            [
                Engagement(id="project", name="Synthetic"),
                Engagement(id="foreign", name="Other project"),
            ]
        )
    )
    entities = []
    edges = []
    now = utc_now()
    for index in range(32):
        asset = Asset(
            id=f"asset-{index}", engagement_id="project", name="Synthetic asset"
        )
        finding = Finding(
            id=f"finding-{index}",
            engagement_id="project",
            title="Synthetic finding",
            asset_ids=["stale-payload-id"],
        )
        evidence = Evidence(
            id=f"evidence-{index}",
            engagement_id="project",
            title="Synthetic evidence",
            evidence_type="note",
            finding_id="stale-payload-id",
        )
        note = Observation(
            id=f"note-{index}",
            engagement_id="project",
            observation_type="note",
            title="Synthetic note",
            evidence_ids=["stale-payload-id"],
        )
        report = Report(
            id=f"report-{index}",
            engagement_id="project",
            title="Synthetic report",
            finding_ids=["stale-payload-id"],
            observation_ids=["stale-payload-id"],
        )
        entities.extend([asset, finding, evidence, note, report])
        for source_kind, source_id, predicate, target_kind, target_id in (
            ("finding", finding.id, "affects", "asset", asset.id),
            ("evidence", evidence.id, "supports", "finding", finding.id),
            ("note", note.id, "references", "evidence", evidence.id),
            ("report", report.id, "includes", "finding", finding.id),
            ("report", report.id, "includes", "note", note.id),
        ):
            edges.append(
                ResourceRelationRow(
                    id=f"edge-{len(edges)}",
                    project_id="project",
                    source_kind=source_kind,
                    source_id=source_id,
                    source_revision=1,
                    predicate=predicate,
                    target_kind=target_kind,
                    target_id=target_id,
                    target_revision=1,
                    attribution="synthetic",
                    provenance={},
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        # Same identifiers in an unrelated project must never affect projection.
        edges.append(
            ResourceRelationRow(
                id=f"edge-{len(edges)}",
                project_id="foreign",
                source_kind="finding",
                source_id=finding.id,
                source_revision=1,
                predicate="affects",
                target_kind="asset",
                target_id="foreign-asset",
                target_revision=1,
                attribution="synthetic",
                provenance={},
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
    (await store.create_many(entities))
    async with store.database.session() as session:
        session.add_all(edges)
    app = create_app(
        store, auth_token="synthetic-capability-test", execution_data_root=tmp_path
    )
    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM resource_relations" in statement:
            queries.append(statement)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test/api/v1",
            headers=AUTH,
        ) as client:
            small = await client.get(
                f"/{kind}", params={"engagement_id": "project", "limit": 1}
            )
            assert small.status_code == 200, small.text
            first_count = len(queries)
            queries.clear()
            large = await client.get(
                f"/{kind}", params={"engagement_id": "project", "limit": 32}
            )
            assert large.status_code == 200, large.text
            assert len(large.json()) == 32
            assert small.json()[0] == large.json()[0]
            for item in large.json():
                index = item["id"].rsplit("-", 1)[1]
                expected = {
                    "observations": {"evidence_ids": [f"evidence-{index}"]},
                    "findings": {
                        "asset_ids": [f"asset-{index}"],
                        "evidence_ids": [f"evidence-{index}"],
                    },
                    "evidence": {"finding_id": f"finding-{index}"},
                    "reports": {
                        "finding_ids": [f"finding-{index}"],
                        "observation_ids": [f"note-{index}"],
                    },
                }[kind]
                for field, value in expected.items():
                    assert item[field] == value
            assert len(queries) <= first_count + 1, (
                "each result added another relationship query"
            )

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", record)
    try:
        (await exercise())
    finally:
        event.remove(store.database.engine.sync_engine, "before_cursor_execute", record)
        (await store.database.engine.dispose())


async def test_projection_preserves_large_mixed_page_and_more_than_500_edges(tmp_path):
    store = NebulaStore(tmp_path / "large-page.db")
    notes = [
        Observation(
            id=f"note-{i:04}",
            engagement_id="project",
            observation_type="note",
            title="Synthetic",
        )
        for i in range(1001)
    ]
    foreign = Observation(
        id="foreign-note",
        engagement_id="foreign",
        observation_type="note",
        title="Synthetic foreign",
    )
    evidence = [
        Evidence(
            id=f"evidence-{i:04}",
            engagement_id="project",
            evidence_type="note",
            title="Synthetic",
        )
        for i in range(505)
    ]
    (
        await store.create_many(
            [
                Engagement(id="project", name="Synthetic"),
                Engagement(id="foreign", name="Other"),
                *notes,
                foreign,
                *evidence,
            ]
        )
    )
    now = utc_now()
    async with store.database.session() as session:
        session.add_all(
            [
                ResourceRelationRow(
                    id=f"edge-{i:04}",
                    project_id="project",
                    source_kind="note",
                    source_id=notes[0].id,
                    source_revision=1,
                    predicate="references",
                    target_kind="evidence",
                    target_id=item.id,
                    target_revision=1,
                    attribution="synthetic",
                    provenance={},
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                for i, item in enumerate(evidence)
            ]
        )
    ordered = [notes[0], foreign, *reversed(notes[1:])]
    try:
        projected = await ResourceRelationService(store).project_legacy_many(ordered)
        assert [item.id for item in projected] == [item.id for item in ordered]
        assert projected[0].evidence_ids == [item.id for item in evidence]
        assert all(not item.evidence_ids for item in projected[1:])
    finally:
        (await store.database.engine.dispose())


pytestmark = pytest.mark.anyio
