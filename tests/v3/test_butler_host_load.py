"""Butler host pressure reads use current authenticated Core responses."""

import asyncio
from dataclasses import replace
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.control_assistant_work import dispatch_work_tool
from nebula.v3.domain import Engagement
from nebula.v3.host_load import HostLoadGate, HostLoadMonitor, HostLoadSnapshot
from nebula.v3.knowledge_index import ChromaKnowledgeIndex
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError
from tests.v3.test_butler_capabilities import _scheduled_capabilities


def test_core_startup_syncs_current_host_load_guidance_into_chroma(tmp_path):
    from scripts.build_product_knowledge import source_digest, source_paths

    store = NebulaStore(tmp_path / "core.db")
    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    index._product_collection.modify(metadata={"source_digest": "stale"})
    app = create_app(
        store,
        knowledge_index=index,
        auth_token="synthetic-core-guidance",
        execution_data_root=tmp_path / "execution",
    )
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/host-load",
                headers={"Authorization": "Bearer synthetic-core-guidance"},
            )
            assert response.status_code == 200
            assert index._product_collection.metadata["source_digest"] == source_digest(
                source_paths()
            )
            documents = index._product_collection.get()["documents"]
            assert any("nebula.work.host_load" in text for text in documents)
    finally:
        store.database.dispose()


class MutableMonitor(HostLoadMonitor):
    def __init__(self):
        super().__init__()
        self.value = HostLoadSnapshot(
            time.monotonic(), 4, 0.96, 0.4, True, "Waiting for host CPU capacity", True
        )

    def cached(self):
        return self.value

    def sample(self):
        return self.value


def fixture(tmp_path):
    store = NebulaStore(tmp_path / "host.db")
    project = store.create(Engagement(id="project", name="Host read project"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    monitor = MutableMonitor()
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="synthetic-host-read",
        host_load_gate=HostLoadGate(monitor),
        execution_data_root=tmp_path / "execution",
    )
    capabilities = ButlerCapabilities(
        store, artifacts, project.id, work=app.state.control_assistant_work
    )
    return store, project, monitor, app, capabilities


def test_host_load_is_discoverable_read_only_and_rereads_core(tmp_path):
    store, project, monitor, app, butler = fixture(tmp_path)
    calls = []

    @app.middleware("http")
    async def capture(request, next_handler):
        if request.url.path == "/api/v1/host-load":
            calls.append((request.method, request.headers.get("authorization")))
        return await next_handler(request)

    async def scenario():
        listed = await butler.search({"query": "host CPU memory"})
        assert any(
            item["operation"] == "nebula.work.host_load" for item in listed["matches"]
        ), listed
        described = await butler.describe({"operation": "nebula.work.host_load"})
        assert described["invoke_with"] == "nebula.capabilities.read", described
        arguments = {"operation": "nebula.work.host_load", "arguments": {}}
        first = await butler.invoke(arguments, change=False)
        assert (
            first["busy"] and first["cpu_ratio"] == 0.96 and first["cpu_capacity"] == 4
        )
        assert first["sample_age_seconds"] >= 0 and first["measurements_available"]
        monitor.value = replace(
            monitor.value,
            sampled_at=time.monotonic(),
            cpu_ratio=0.1,
            busy=False,
            reason=None,
        )
        second = await butler.invoke(arguments, change=False)
        assert (
            not second["busy"]
            and second["cpu_ratio"] == 0.1
            and second["reason"] is None
        )
        assert calls == [("GET", "Bearer synthetic-host-read")] * 2
        catalog = app.state.control_assistant_work.catalog(project.id)
        assert catalog["host_admission"]["read_operation"] == "nebula.work.host_load"
        with pytest.raises(CapabilityInputError):
            await butler.invoke(arguments, change=True)
        with pytest.raises(CapabilityInputError):
            await butler.invoke(
                {
                    "operation": "nebula.work.host_load",
                    "arguments": {"project_id": "other"},
                },
                change=False,
            )

    try:
        asyncio.run(scenario())
    finally:
        store.database.dispose()


def test_managed_host_load_uses_fixed_route_and_preserves_unknown_values(tmp_path):
    store, project, monitor, app, _butler = fixture(tmp_path)
    monitor.value = replace(
        monitor.value,
        cpu_ratio=None,
        memory_available_ratio=None,
        measurements_available=False,
        busy=False,
        reason=None,
    )

    async def scenario():
        value = await dispatch_work_tool(
            app.state.control_assistant_work, project.id, "nebula.work.host_load", {}
        )
        assert value["cpu_ratio"] is None and value["memory_available_ratio"] is None
        assert value["measurements_available"] is False
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://core.test"
        ) as client:
            assert (await client.get("/api/v1/host-load")).status_code == 401

    try:
        asyncio.run(scenario())
    finally:
        store.database.dispose()


def test_scheduled_global_butler_can_discover_and_reread_host_load(tmp_path):
    store, project, _chat, _foreign, _policy, _case, butler = _scheduled_capabilities(
        tmp_path
    )
    app = butler._work.app
    monitor = MutableMonitor()
    app.state.host_load.monitor = monitor

    async def scenario():
        matches = await butler.search({"query": "host CPU memory"})
        assert any(
            item["operation"] == "nebula.global.host_load"
            for item in matches["matches"]
        ), matches
        arguments = {"operation": "nebula.global.host_load", "arguments": {}}
        first = await butler.invoke(arguments, change=False)
        assert first["busy"] and first["cpu_ratio"] == 0.96
        monitor.value = replace(
            monitor.value,
            cpu_ratio=0.2,
            busy=False,
            reason=None,
            sampled_at=time.monotonic(),
        )
        second = await butler.invoke(arguments, change=False)
        assert not second["busy"] and second["cpu_ratio"] == 0.2

    try:
        asyncio.run(scenario())
    finally:
        store.database.dispose()


def test_expired_host_sample_refresh_runs_off_loop_and_is_shared(tmp_path, monkeypatch):
    store, project, monitor, app, butler = fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()
    reads = []
    loop_thread = threading.get_ident()
    monkeypatch.setattr(monitor, "cached", lambda: None)

    def sample():
        reads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "synthetic sample was not released"
        return monitor.value

    monkeypatch.setattr(monitor, "sample", sample)

    async def scenario():
        arguments = {"operation": "nebula.work.host_load", "arguments": {}}
        tasks = [
            asyncio.create_task(butler.invoke(arguments, change=False))
            for _ in range(8)
        ]
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
            assert reads == [reads[0]] and reads[0] != loop_thread
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://core.test",
                headers={"Authorization": "Bearer synthetic-host-read"},
            ) as client:
                response = await asyncio.wait_for(
                    client.get(f"/api/v1/engagements/{project.id}/assistant-defaults"),
                    timeout=0.5,
                )
                assert response.status_code == 200
            release.set()
            values = await asyncio.gather(*tasks)
            assert len(reads) == 1 and all(
                value["cpu_ratio"] == 0.96 for value in values
            )
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        store.database.dispose()
