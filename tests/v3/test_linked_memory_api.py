"""The CLI's routes and Butler's typed actions share one revisioned stage."""

from __future__ import annotations

import httpx
import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Engagement
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore

pytestmark = pytest.mark.anyio

TOKEN = "synthetic-linked-memory"
PATH = "/api/v1/engagements/project/linked-memory"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def core(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    await store.create(Engagement(id="project", name="Synthetic project"))
    app = create_app(
        store, auth_token=TOKEN, execution_data_root=tmp_path / "execution"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            yield store, app, client
    finally:
        await store.database.dispose()


async def test_stage_routes_are_revisioned_and_report_status(core):
    _store, _app, client = core
    assert (await client.get(PATH, headers={"Authorization": ""})).status_code == 401
    initial = (await client.get(PATH)).json()
    assert (initial["stage"], initial["revision"]) == ("off", 0)
    assert initial["available_stages"] == ["off", "shadow"]

    saved = await client.put(PATH, json={"stage": "shadow", "expected_revision": 0})
    assert saved.status_code == 200
    assert (saved.json()["stage"], saved.json()["revision"]) == ("shadow", 1)
    assert saved.json()["updated_by"] == "operator"
    stale = await client.put(PATH, json={"stage": "off", "expected_revision": 0})
    assert stale.status_code == 409
    later = await client.put(PATH, json={"stage": "inject", "expected_revision": 1})
    assert later.status_code == 422

    status = (await client.get(PATH + "/status")).json()
    assert status["settings"]["stage"] == "shadow"
    assert status["backlog"] == 0
    assert status["cards"] == {
        "active": 0,
        "quarantined": 0,
        "superseded": 0,
        "total": 0,
    }
    assert status["curation"]["curator"] in {"idle", "unavailable"}
    assert (await client.get(PATH + "/cards")).json() == {"items": []}
    missing = await client.get("/api/v1/engagements/missing/linked-memory")
    assert missing.status_code == 404


async def test_butler_discovers_reads_and_sets_linked_memory_with_read_back(
    core, tmp_path
):
    store, app, client = core
    butler = await ButlerCapabilities.create(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        "project",
        settings_api=SettingsAssistantApi(app, TOKEN),
    )
    matches = await butler.search(
        {"query": "linked memory curation of completed goals"}
    )
    operations = {item["operation"] for item in matches["matches"]}
    assert "nebula.settings.linked_memory" in operations
    description = await butler.describe(
        {"operation": "nebula.settings.set_linked_memory"}
    )
    assert "expected_revision" in description["input_schema"]["properties"]

    current = await butler.invoke(
        {"operation": "nebula.settings.linked_memory", "arguments": {}}, change=False
    )
    assert current["settings"]["stage"] == "off"
    assert current["status"]["backlog"] == 0
    saved = await butler.invoke(
        {
            "operation": "nebula.settings.set_linked_memory",
            "arguments": {
                "stage": "shadow",
                "expected_revision": current["settings"]["revision"],
            },
        },
        change=True,
    )
    assert saved["read_back"] is True
    assert saved["result"]["stage"] == "shadow"
    observed = (await client.get(PATH)).json()
    assert (observed["stage"], observed["revision"]) == ("shadow", 1)
    assert observed["updated_by"] == "butler:project"


async def test_kernel_handoff_probe_reads_the_worker_coordination(core):
    """The curator stops at a chunk boundary once its project hands off."""

    from types import SimpleNamespace

    _store, app, _client = core
    curator = app.state.linked_memory_curator
    assert await curator.handoff_active() is False  # no worker handoff agent

    class Coordination:
        def __init__(self, **snapshot):
            self.snapshot = SimpleNamespace(**snapshot)
            self.leases = []

        async def dispatch_snapshot(self, lease):
            self.leases.append(lease)
            return self.snapshot

    for snapshot, expected in (
        ({"owned": True, "handoff_active": False}, False),
        ({"owned": True, "handoff_active": True}, True),
        ({"owned": False, "handoff_active": False}, True),
    ):
        coordination = Coordination(**snapshot)
        app.state.runtime_handoff = SimpleNamespace(
            coordinator=SimpleNamespace(coordination=coordination), lease="lease"
        )
        assert await curator.handoff_active() is expected
        assert coordination.leases == ["lease"]
