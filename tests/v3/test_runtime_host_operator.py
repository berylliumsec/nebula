"""Authenticated host controls and exact project setup with inert adapters."""

import asyncio
import json
import socket
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
import uvicorn
import websockets

from nebula.v3.database import (
    EntityRow,
    MemoryJobRow,
    ProjectOwnershipRow,
)
from nebula.v3.deployment_protocol import GLOBAL_EXECUTION_PROJECT
from nebula.v3.domain import HarnessProfile, RunnerProfile
from nebula.v3.host_workspace_folders import HostWorkspaceFolderCreateRequest
import nebula.v3.host_workspace_folders as host_folders
from nebula.v3.runtime_host_operator import is_host_operator_route
from nebula.v3.sandbox import ContainerSandboxRunner
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_host_settings import AUTH, settings as settings
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def inert_detection(monkeypatch):
    # No installed runtime or account is probed by this synthetic journey.
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", ())


def limits():
    return {
        "rateLimits": {
            "limitId": "synthetic",
            "primary": {"usedPercent": 25, "windowDurationMins": 300},
        }
    }


async def harness(settings, **changes):
    return await settings.store.create(
        HarnessProfile(
            id="operator-account",
            name="Synthetic account",
            kind="codex_app_server",
            **({"executable": "/inert/codex"} | changes),
        )
    )


async def no_project_owner(settings):
    async with settings.managed.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
        assert await session.get(EntityRow, GLOBAL_EXECUTION_PROJECT) is None


def test_host_operator_classifier_does_not_claim_project_image_or_capacity():
    for method, path in (
        ("GET", "/api/v1/setup/status"),
        ("POST", "/api/v1/setup/runtime/select"),
        ("POST", "/api/v1/setup/runtime/refresh"),
        ("GET", "/api/v1/workspace-folders"),
        ("POST", "/api/v1/workspace-folders"),
        ("POST", "/api/v1/harnesses/known/quota/refresh"),
    ):
        assert is_host_operator_route(method, path)
    for method, path in (
        ("POST", "/api/v1/setup/image/prepare"),
        ("POST", "/api/v1/setup/image/cancel"),
        ("GET", "/api/v1/setup/events"),
        ("GET", "/api/v1/container-terminal/capacity"),
        ("POST", "/api/v1/harnesses/known/quota/refresh/extra"),
    ):
        assert not is_host_operator_route(method, path)


async def test_cancelled_folder_response_joins_its_owned_filesystem_effect(
    tmp_path, monkeypatch
):
    entered, release = Event(), Event()
    original = host_folders._create

    def create(request):
        entered.set()
        assert release.wait(timeout=5)
        return original(request)

    monkeypatch.setattr(host_folders, "_create", create)
    task = asyncio.create_task(
        host_folders.create_host_workspace_folder(
            HostWorkspaceFolderCreateRequest(parent_path=str(tmp_path), name="joined")
        )
    )
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "joined").is_dir()


async def test_host_folder_create_replay_browse_and_recovery_without_project_owner(
    settings, tmp_path
):
    path = "/api/v1/workspace-folders"
    assert (
        await settings.client.get(path, headers={"Authorization": "Bearer invalid"})
    ).status_code == 401
    assert (await settings.client.post(path, json={})).status_code == 428
    body = {"parent_path": str(tmp_path), "name": "Chosen project"}
    first, retry = await asyncio.gather(
        settings.mutate("workspace-folders", body=body, key="folder"),
        settings.mutate("workspace-folders", body=body, key="folder"),
    )
    assert first.status_code == retry.status_code == 201, first.text
    assert first.json() == retry.json()
    assert first.headers["X-Nebula-Command-Id"] == retry.headers["X-Nebula-Command-Id"]
    created = tmp_path / body["name"]
    assert created.is_dir() and created.stat().st_mode & 0o777 == 0o700
    observed = await settings.client.get(path, params={"path": str(tmp_path)})
    assert {"name": body["name"], "path": str(created)} in observed.json()[
        "directories"
    ]
    duplicate = await settings.mutate("workspace-folders", body=body, key="duplicate")
    assert duplicate.status_code == 409 and "already exists" in duplicate.text
    invalid = await settings.mutate(
        "workspace-folders", body={**body, "name": "../outside"}, key="bad-name"
    )
    assert invalid.status_code == 422 and not (tmp_path.parent / "outside").exists()
    assert (
        await settings.client.get(path, params={"path": str(tmp_path / "missing")})
    ).status_code == 404
    assert (
        await settings.client.get(path, params={"path": "relative"})
    ).status_code == 422
    await no_project_owner(settings)


async def test_host_folder_pagination_filter_and_sqlite_selection_skip_links(
    settings, tmp_path
):
    browse = tmp_path / "browse"
    browse.mkdir()
    for name in ("Alpha", "beta", "gamma"):
        (browse / name).mkdir()
    (browse / "account.sqlite3").write_text("synthetic only")
    (browse / "ordinary.txt").write_text("ignored")
    (browse / "linked").symlink_to(browse / "Alpha", target_is_directory=True)
    first = await settings.client.get(
        "/api/v1/workspace-folders", params={"path": str(browse), "limit": 2}
    )
    assert [row["name"] for row in first.json()["directories"]] == ["Alpha", "beta"]
    assert first.json()["next_offset"] == 2
    second = await settings.client.get(
        "/api/v1/workspace-folders",
        params={"path": str(browse), "offset": 2, "limit": 2},
    )
    assert [row["name"] for row in second.json()["directories"]] == ["gamma"]
    assert not second.json()["truncated"] and second.json()["next_offset"] is None
    sqlite = await settings.client.get(
        "/api/v1/workspace-folders",
        params={"path": str(browse), "include_sqlite_files": True, "filter": "ACCOUNT"},
    )
    assert sqlite.json()["files"] == [
        {"name": "account.sqlite3", "path": str(browse / "account.sqlite3")}
    ]
    assert sqlite.json()["directories"] == []


async def test_host_runtime_selection_replays_verified_fixed_candidate(
    settings, tmp_path, monkeypatch
):
    runtimes = (tmp_path / "podman", tmp_path / "docker")
    for path in runtimes:
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", runtimes)

    async def available(_runner):
        assert settings.managed.database.task_session() is None
        return True, "Synthetic fixed runtime"

    monkeypatch.setattr(ContainerSandboxRunner, "available", available)
    refreshed = await settings.mutate("setup/runtime/refresh", key="runtime-refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["terminal"]["status"] == "needs_runner"
    candidate = refreshed.json()["terminal"]["candidates"][0]["candidate_id"]
    selected = await settings.mutate(
        "setup/runtime/select",
        key="runtime-selection",
        body={"candidate_id": candidate},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["setup"]["terminal"]["status"] == "needs_image"
    saved = await settings.store.get(RunnerProfile, "local")
    repeated = await settings.mutate(
        "setup/runtime/select",
        key="runtime-selection",
        body={"candidate_id": candidate},
    )
    assert repeated.json() == selected.json()
    assert (await settings.store.get(RunnerProfile, "local")).revision == saved.revision
    arbitrary = await settings.mutate(
        "setup/runtime/select",
        key="arbitrary-path",
        body={"candidate_id": candidate, "executable": "/tmp/arbitrary"},
    )
    assert arbitrary.status_code == 422
    observed = await settings.client.get("/api/v1/setup/status")
    assert observed.json()["terminal"]["runner_profile_id"] == "local"
    assert observed.json()["terminal"]["image_preparation"]["operation_id"] is None
    await no_project_owner(settings)


async def test_host_quota_refresh_and_usage_share_protected_reader_and_replay(settings):
    await harness(settings)
    calls = []

    async def guarded(profile, credentials, controller):
        assert settings.managed.database.task_session() is None
        assert credentials is settings.host.credentials
        assert controller is settings.host.observations.memory_compute
        calls.append(profile.id)
        return limits()

    async def unguarded(*_args):
        pytest.fail("A spawned account reader escaped memory admission")

    settings.host.harness_factory = lambda _kind: SimpleNamespace(
        read_account_usage_memory_managed=guarded, read_account_usage=unguarded
    )
    first = await settings.mutate(
        "harnesses/operator-account/quota/refresh", key="quota"
    )
    retry = await settings.mutate(
        "harnesses/operator-account/quota/refresh", key="quota"
    )
    assert first.status_code == retry.status_code == 200, first.text
    assert first.json() == retry.json() and calls == ["operator-account"]
    assert first.json()["snapshots"][0]["remaining_percent"] == 75
    usage = await settings.client.get("/api/v1/runtime-usage")
    assert (
        usage.json()["items"][0]["vendor"]["quota_snapshots"][0]["remaining_percent"]
        == 75
    )
    assert calls == ["operator-account"]
    await settings.client.get("/api/v1/runtime-usage?refresh=true")
    assert calls == ["operator-account", "operator-account"]
    await no_project_owner(settings)
    async with settings.managed.database.session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EntityRow)
                .where(EntityRow.kind == "butler_harness_quotas")
            )
            == 0
        )


async def test_unprotected_native_account_reader_is_held_for_both_entry_points(
    settings,
):
    await harness(settings)

    async def unguarded(*_args):
        pytest.fail("An unsupported native account process was started")

    settings.host.harness_factory = lambda _kind: SimpleNamespace(
        read_account_usage=unguarded
    )
    refreshed = await settings.mutate(
        "harnesses/operator-account/quota/refresh", key="held"
    )
    assert refreshed.status_code == 200 and refreshed.json()["state"] == "unavailable"
    assert "protected memory-runner/v1" in refreshed.json()["detail"]
    usage = await settings.client.get("/api/v1/runtime-usage?refresh=true")
    assert usage.status_code == 200
    assert usage.json()["items"][0]["vendor"]["state"] == "unavailable"
    async with settings.managed.database.session() as session:
        jobs = list(await session.scalars(select(MemoryJobRow)))
        account_jobs = [job for job in jobs if job.resource_kind == "harness_account"]
        assert len(account_jobs) == 2
        assert all(
            job.project_id is None and job.state == "blocked" for job in account_jobs
        )


async def test_existing_codex_endpoint_refreshes_without_a_native_launch(
    settings, monkeypatch
):
    calls = []

    async def endpoint(connection):
        async for raw in connection:
            request = json.loads(raw)
            calls.append(request["method"])
            if "id" in request:
                result = {} if request["method"] == "initialize" else limits()
                await connection.send(
                    json.dumps({"id": request["id"], "result": result})
                )

    async with websockets.serve(endpoint, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await harness(
            settings,
            connection_mode="endpoint",
            transport="websocket",
            endpoint=f"ws://127.0.0.1:{port}",
            executable=None,
        )

        async def no_spawn(*_args, **_kwargs):
            pytest.fail("An endpoint quota inspection spawned a native process")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
        response = await settings.mutate(
            "harnesses/operator-account/quota/refresh", key="endpoint"
        )
    assert response.status_code == 200 and response.json()["state"] == "available", (
        response.text
    )
    assert response.json()["snapshots"][0]["remaining_percent"] == 75
    assert calls == ["initialize", "initialized", "account/rateLimits/read"]


async def test_host_operator_requires_active_exclusive_host_lock(settings):
    host = settings.managed.host
    descriptor = host.lock.descriptor
    host.lock.descriptor = None
    try:
        response = await settings.client.get("/api/v1/setup/status")
        assert response.status_code == 503 and "reconnecting" in response.text
    finally:
        host.lock.descriptor = descriptor


_SETUP_WORKER = """
import asyncio
from types import SimpleNamespace
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.sandbox import ContainerSandboxRunner

async def available(_runner):
    return True, 'Synthetic runtime inspection'

async def prepare(self, project_id, *, on_progress=None):
    if on_progress:
        await on_progress('Waiting for synthetic image fixture')
    while not (self.data_root / 'operator-image-release').exists():
        await asyncio.sleep(0.01)
    return SimpleNamespace(image=SimpleNamespace(digest='sha256:'+'a'*64, detail='Synthetic image ready'))

ContainerSandboxRunner.available = available
ContainerSandboxRunner._trusted_runtime_paths = ()
RuntimePlatform.resolve_human_terminal_runtime = prepare
from nebula.v3.runtime_worker import main
main()
"""


async def test_actual_retained_project_setup_status_cancel_retry_and_capacity_are_exact(
    settings,
):
    await settings.store.create(
        RunnerProfile(
            id="local",
            name="Inert runner",
            runtime="docker",
            executable="/inert/docker",
            platform="linux/amd64",
            isolation="rootless",
            healthy=True,
        )
    )
    root, fixture = settings.root, settings.managed
    await fixture.stage("operator-v1", _SETUP_WORKER)
    execution = await root.prepare_execution("operator-v1")
    await root.attach_execution("admitted-project", execution.runtime_id)
    other = await root.prepare_execution("operator-v1")
    await root.attach_execution("synthetic-project", other.runtime_id)
    api = await root.prepare_api("operator-v1")
    await root.promote_api(
        api.runtime_id, expected_revision=(await root.coordinator.status()).revision
    )
    started = await settings.mutate(
        "setup/image/prepare",
        key="image-start",
        body={"project_id": "admitted-project"},
    )
    assert started.status_code == 200, started.text
    operation_id = started.json()["operation_id"]
    exact = await settings.client.get(
        "/api/v1/setup/status", params={"project_id": "admitted-project"}
    )
    assert exact.json()["terminal"]["image_preparation"]["operation_id"] == operation_id
    other_status = await settings.client.get(
        "/api/v1/setup/status", params={"project_id": "synthetic-project"}
    )
    assert other_status.json()["terminal"]["image_preparation"]["operation_id"] is None
    refused = await settings.mutate(
        "setup/image/cancel",
        key="other-cancel",
        body={"project_id": "synthetic-project", "operation_id": operation_id},
    )
    assert refused.status_code == 404, refused.text
    cancelled = await settings.mutate(
        "setup/image/cancel",
        key="image-cancel",
        body={"project_id": "admitted-project", "operation_id": operation_id},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert (
        cancelled.json()["setup"]["terminal"]["image_preparation"]["phase"]
        == "cancelled"
    )
    retried = await settings.mutate(
        "setup/image/retry", key="image-retry", body={"project_id": "admitted-project"}
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["operation_id"] != operation_id
    (fixture.data / "operator-image-release").write_text("inert fixture release")
    async with asyncio.timeout(10):
        while True:
            observed = await settings.client.get(
                "/api/v1/setup/status", params={"project_id": "admitted-project"}
            )
            if observed.json()["terminal"]["image_preparation"]["phase"] == "ready":
                break
            await asyncio.sleep(0.02)
    events = await settings.client.get(
        "/api/v1/setup/events",
        params={"project_id": "admitted-project", "follow": False},
    )
    assert events.status_code == 200 and '"image_preparation_ready"' in events.text
    capacity = await settings.client.get(
        "/api/v1/container-terminal/capacity",
        params={"engagement_id": "admitted-project"},
    )
    assert capacity.status_code == 200 and capacity.json()["active_sessions"] == 0, (
        capacity.text
    )
    assert (
        capacity.json()["available_sessions"] == capacity.json()["max_active_sessions"]
    )


async def test_authenticated_nonloopback_host_folder_journey(settings, tmp_path):
    host = socket.gethostbyname(socket.gethostname())
    assert not host.startswith("127.")
    listener = socket.socket()
    listener.bind((host, 0))
    listener.listen()
    origin = f"http://{host}:{listener.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(settings.root.app, log_level="error", lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            assert (await client.get("/api/v1/workspace-folders")).status_code == 401
            body = {"parent_path": str(tmp_path), "name": "LAN selected project"}
            headers = {**AUTH, "Idempotency-Key": "lan-folder"}
            created = await client.post(
                "/api/v1/workspace-folders", headers=headers, json=body
            )
            assert created.status_code == 201, created.text
            replay = await client.post(
                "/api/v1/workspace-folders", headers=headers, json=body
            )
            assert replay.json() == created.json()
            listing = await client.get(
                "/api/v1/workspace-folders",
                headers=AUTH,
                params={"path": str(tmp_path), "filter": "LAN selected"},
            )
            assert listing.json()["directories"][0]["path"] == created.json()["path"]
            (tmp_path / "host-operator-lan-receipt.json").write_text(
                json.dumps(
                    {
                        "origin": origin,
                        "synthetic_only": True,
                        "auth_required": True,
                        "created_status": created.status_code,
                        "replay_command": replay.headers["x-nebula-command-id"],
                        "visible_path": listing.json()["directories"][0]["path"],
                    },
                    indent=2,
                )
            )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
