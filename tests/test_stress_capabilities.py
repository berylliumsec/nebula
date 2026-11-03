"""A stress receipt must retain failures and clean up its owned Core."""

import argparse
import asyncio
import json
from pathlib import Path
import socket

import httpx
import pytest

from scripts import stress_capabilities as stress
from nebula.v3.api import create_app
from nebula.v3.domain import Engagement, Observation
from nebula.v3.storage import NebulaStore


def test_phase_counts_all_failures_even_when_samples_are_bounded(monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        if request.url.path == "/__stress/metrics":
            return httpx.Response(200, json={"cpu_s": 1, "fixture_counts": {}})
        if request.url.path.endswith("assistant-defaults"):
            return httpx.Response(503, json={"detail": "Synthetic probe failure"})
        return httpx.Response(500, json={"detail": "Synthetic workload failure"})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        stress.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )

    async def exercise():
        async with original(base_url="http://core.test", transport=transport) as client:
            result = await stress.phase(
                client,
                client,
                [stress.Scenario("failure", "/failure")],
                32,
                2,
                0,
                "synthetic-errors",
            )
            assert result["operations"] == result["error_count"] == 64
            assert len(result["errors"]) == 20
            assert result["sentinel_errors"] > 0
            assert result["max_ms"] is not None

    asyncio.run(exercise())


def test_record_workload_uses_server_ids_and_checks_durable_revisions(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    store.create(Engagement(id="project-0", name="Synthetic project"))
    app = create_app(store, auth_token="synthetic-token", execution_data_root=tmp_path)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test",
            headers={"Authorization": "Bearer synthetic-token"},
        ) as client:
            await stress.execute(
                client,
                stress.Scenario("record-lifecycle", "/api/v1/observations"),
                "synthetic-operation",
            )

    try:
        asyncio.run(exercise())
        assert store.count(Observation, engagement_id="project-0") == 0
    finally:
        store.database.engine.dispose()


def test_successful_http_with_wrong_data_fails_the_workload():
    async def exercise():
        async with httpx.AsyncClient(
            base_url="http://core.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"id": "wrong-note"})
            ),
        ) as client:
            with pytest.raises(AssertionError):
                await stress.execute(
                    client,
                    stress.Scenario("note-detail", "/note"),
                    "synthetic-operation",
                )

    asyncio.run(exercise())


def test_failed_startup_still_writes_failure_and_removes_temporary_data(
    tmp_path, monkeypatch
):
    class FailedProcess:
        returncode = 7

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(
        stress.subprocess, "Popen", lambda *args, **kwargs: FailedProcess()
    )
    monkeypatch.setattr(
        stress.subprocess, "check_output", lambda *args, **kwargs: "synthetic-build"
    )
    data = tmp_path / "owned-core"
    data.mkdir()
    monkeypatch.setattr(stress.tempfile, "mkdtemp", lambda **kwargs: str(data))
    output = tmp_path / "receipt.json"
    args = argparse.Namespace(
        source_tree=Path(__file__).resolve().parents[1],
        output=output,
        lan_address=socket.gethostbyname(socket.gethostname()),
        scenario=None,
        scale=1,
        concurrency=[1],
        waves=1,
        mixed=False,
        soak_seconds=0,
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        asyncio.run(stress.run(args))
    report = json.loads(output.read_text())
    assert report["fatal_error"]["type"] == "RuntimeError"
    assert report["cleanup"] == {"server_exit": 7, "temporary_data_removed": True}
    assert not data.exists()
