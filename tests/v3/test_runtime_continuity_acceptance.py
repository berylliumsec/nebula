"""Actual retained source/API/execution continuity, on disposable SQL only."""

import asyncio
import json
import os
from pathlib import Path
import subprocess

import pytest

from nebula.v3.domain import ChatTurn, Engagement
from nebula.v3.process_identity import same_process
from nebula.v3.release_store import ReleaseStore
from tests.v3.runtime_continuity_bundle import build_bundle, remove_owned_tree, retained_bundle
from tests.v3.runtime_continuity_fixture import ContinuityFixture
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def continuity_bundles(tmp_path_factory):
    root = tmp_path_factory.mktemp("retained-continuity-bundles")
    source = Path(os.environ.get("NEBULA_ACCEPTANCE_SOURCE", str(Path(__file__).resolve().parents[2] / "src")))
    ui = Path(os.environ.get("NEBULA_ACCEPTANCE_UI", "/workspace/nebula/ui/dist"))
    dependencies = Path(os.environ.get("NEBULA_ACCEPTANCE_DEPENDENCIES", "/workspace/nebula/.venv/lib/python3.12/site-packages"))
    retained = ReleaseStore(root / "retained")
    bundles = []
    try:
        for version in ("acceptance-v1", "acceptance-v2", "acceptance-v3"):
            original = root / version
            bundle = build_bundle(original, version=version, source=source, ui=ui,
                                  dependencies=dependencies,
                                  runtime_template=bundles[0].path if bundles else None)
            bundles.append(retained_bundle(bundle, retained))
            remove_owned_tree(original)
        yield bundles
    finally:
        remove_owned_tree(root)


@pytest.fixture
async def continuity(tmp_path, location, continuity_bundles):
    fixture = ContinuityFixture(tmp_path / "installation", location, continuity_bundles)
    try:
        await fixture.initialize()
        yield fixture
    finally:
        await fixture.close()


async def test_retained_source_api_replacement_and_code_rollback_keep_one_turn(continuity):
    source, first_api = await continuity.ready()
    continuity.hold("hold-provider")
    command, frames = await continuity.capture("accepted-on-v1")
    started = next(frame for frame in frames if frame["type"] == "started")
    cursor = frames[-1]["sequence"]
    second_api = await continuity.control("api", version="acceptance-v2")
    await continuity.control("retire", runtime_id=first_api["runtime_id"])
    assert same_process(source["pid"], source["process_birth"])
    assert not same_process(first_api["pid"], first_api["process_birth"])
    saved = await continuity.client.patch("/api/v1/engagements/continuity-project", json={
        "changes": {"name": "Committed while candidate API was active"}, "expected_revision": 1,
    }, headers={"Idempotency-Key": "saved-on-v2"})
    assert saved.status_code in {200, 202}, saved.text
    async with asyncio.timeout(15):
        while (await continuity.store.get(Engagement, "continuity-project")).name != "Committed while candidate API was active":
            await asyncio.sleep(.03)
    await continuity.control("api", version="acceptance-v1")
    continuity.release("hold-provider")
    reply = await continuity.client.get(f"/api/v1/runtime/requests/{command}/response?after={cursor}")
    assert reply.status_code == 200 and "event: done" in reply.text
    assert reply.headers["x-nebula-api-version"] == "acceptance-v1"
    assert '"delta":"last"' in reply.text and '"delta":"First "' not in reply.text
    turns = await continuity.store.list_entities(ChatTurn)
    assert len(turns) == 1 and turns[0].id == started["turn_id"]
    assert (await continuity.store.get(Engagement, "continuity-project")).name == "Committed while candidate API was active"
    observed = await continuity.control("inspect")
    assert len([item for item in observed["events"] if item["event"] == "provider-start"]) == 1
    for event in observed["events"]:
        if event["event"] == "project-constructed":
            assert str(continuity.bundles[0].path) in event["source"]
    status = (await continuity.client.get("/api/v1/deployment/status")).json()
    assert "acceptance-v3" in status["staged_versions"]
    assert same_process(second_api["pid"], second_api["process_birth"])


@pytest.mark.parametrize("phase", ["requested", "finishing", "transferable", "transferred", "verified"])
async def test_actual_supervisor_crash_resumes_each_committed_handoff_phase(continuity, phase):
    source, _api = await continuity.ready()
    continuity.hold("hold-provider")
    _command, frames = await continuity.capture("running-across-supervisor-crash")
    if phase != "verified":
        continuity.hold("hold-phase-" + phase)
    target = await continuity.control("handoff", version="acceptance-v2", project_id="continuity-project")
    if phase != "requested" and phase != "finishing":
        continuity.release("hold-provider")
    before = await continuity.wait_project(phase)
    await continuity.kill_supervisor()
    assert same_process(source["pid"], source["process_birth"])
    assert same_process(target["pid"], target["process_birth"])
    continuity.release("hold-phase-" + phase)
    continuity.release("hold-provider")
    await continuity.start()
    after = await continuity.wait_project("verified")
    assert after["runtime_id"] == target["runtime_id"]
    assert after["fencing_token"] == before["fencing_token"] + (phase in {"requested", "finishing", "transferable"})
    turn_id = next(frame["turn_id"] for frame in frames if frame["type"] == "started")
    replay = await continuity.client.get(f"/api/v1/chat/turns/{turn_id}/events?after=0")
    assert replay.status_code == 200 and "event: done" in replay.text
    assert "event: cancelled" not in replay.text and "event: paused" not in replay.text
    observed = await continuity.control("inspect")
    assert len([item for item in observed["events"] if item["event"] == "provider-start"]) == 1


async def test_retained_bundle_uses_its_own_python_source_and_dependencies(continuity_bundles):
    bundle = continuity_bundles[0]
    result = await asyncio.to_thread(subprocess.run,
        [str(bundle.path / "nebula-core"), "acceptance-probe"],
        check=True, capture_output=True, text=True, timeout=45,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/unusable/source"},
    )
    result = json.loads(result.stdout)
    assert all(str(bundle.path) in result[key] for key in ("python", "core", "sqlalchemy", "chroma"))


async def test_zero_api_recovery_serves_verified_ui_and_admits_after_reserve_readback(continuity):
    import hashlib
    from nebula.v3.memory_policy import GIB

    await continuity.control("recovery-release", version="acceptance-v1")
    current = (await continuity.client.get("/api/v1/memory-budgets")).json()
    policy = {**current["policy"], "reserve_bytes": 2 * GIB}
    saved = await continuity.client.put("/api/v1/memory-budgets", json={"expected_revision": current["revision"], "policy": policy})
    assert saved.status_code == 200, saved.text
    health = await continuity.client.get("/api/v1/health")
    assert health.status_code == 200 and health.json()["api_ready"] is False
    index = await continuity.client.get("/index.html")
    assert index.status_code == 200
    assert hashlib.sha256(index.content).hexdigest() == continuity.bundles[0].index_sha256
    observed = await continuity.control("inspect")
    assert observed["processes"] == []
    refused = await continuity.client.post("/__acceptance/control", json={"operation": "api", "version": "acceptance-v1"})
    assert refused.status_code >= 400
    current = (await continuity.client.get("/api/v1/memory-budgets")).json()
    restored = await continuity.client.put("/api/v1/memory-budgets", json={
        "expected_revision": current["revision"], "policy": {**current["policy"], "reserve_bytes": 4 * GIB},
    })
    assert restored.status_code == 200, restored.text
    assert (await continuity.client.get("/api/v1/memory-budgets")).json()["policy"]["reserve_bytes"] == 4 * GIB
    await continuity.control("api", version="acceptance-v1")
    assert (await continuity.client.get("/api/v1/health")).json()["api_ready"] is True


@pytest.fixture
async def allocator_continuity(tmp_path, location, continuity_bundles):
    if not os.environ.get("NEBULA_TEST_MEMORY_RUNTIME") or not os.environ.get("NEBULA_TEST_MEMORY_IMAGE"):
        pytest.skip("explicit disposable OCI runtime and cached image required for physical allocator gate")
    fixture = ContinuityFixture(tmp_path / "allocator-installation", location, continuity_bundles)
    try:
        await fixture.initialize(memory=True)
        yield fixture
    finally:
        await fixture.close()


async def test_live_apply_after_api_replacement_reaches_original_physical_allocator(allocator_continuity):
    fixture = allocator_continuity
    from nebula.v3.memory_policy import MIB
    source, _api = await fixture.ready()
    identity = "physical-continuity-allocator"
    task = asyncio.create_task(fixture.client.post(
        "/api/v1/engagements/continuity-project/acceptance-allocation",
        json={"job_id": identity}, headers={"Idempotency-Key": identity},
    ))
    try:
        async with asyncio.timeout(30):
            while True:
                response = await fixture.client.get("/api/v1/memory-budgets/jobs/" + identity)
                if response.status_code == 200:
                    job = response.json()
                    if job["state"] == "running" and (job["evidence"].get("current_bytes") or 0) >= 4 * MIB:
                        break
                await asyncio.sleep(.05)
        await fixture.control("api", version="acceptance-v2")
        current = (await fixture.client.get("/api/v1/memory-budgets/jobs/" + identity)).json()
        update = {"expected_revision": current["revision"], "limits": {"max_bytes": 16 * MIB}, "acknowledge_possible_termination": True}
        applied = await fixture.client.post(f"/api/v1/memory-budgets/jobs/{identity}/apply", json=update,
                                           headers={"Idempotency-Key": "apply-physical-limit"})
        assert applied.status_code == 200, applied.text
        job = applied.json()
        assert job["application_state"] == "verified"
        assert job["evidence"]["applied"]["max_bytes"] == 16 * MIB
        assert same_process(source["pid"], source["process_birth"])
        environment = {name: value for name, value in os.environ.items() if name not in {
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
        }}
        check = await asyncio.to_thread(subprocess.run, [os.environ["NEBULA_TEST_MEMORY_RUNTIME"],
            "--host=" + os.environ.get("NEBULA_TEST_MEMORY_SOCKET", "unix:///var/run/docker.sock"),
            "inspect", "--format", "{{.HostConfig.Memory}}", job["evidence"]["group_id"]],
            check=True, capture_output=True, text=True, timeout=10, env=environment)
        assert int(check.stdout.strip()) == 16 * MIB
        finished = await asyncio.wait_for(task, 45)
        assert finished.status_code == 200, finished.text
        final = (await fixture.client.get("/api/v1/memory-budgets/jobs/" + identity)).json()
        assert final["evidence"]["group_empty"] and final["reserved_bytes"] == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_actual_runtime_service_connector_updates_rolls_back_and_stops(continuity_bundles, tmp_path):
    import httpx
    bundles = continuity_bundles
    if not (bundles[0].path / "source/nebula/v3/runtime_launcher.py").exists():
        pytest.skip("selected source predates the persistent runtime_service launcher integration")
    data = tmp_path / "actual-runtime-service"
    environment = {name: value for name, value in os.environ.items()
                   if name in {"HOME", "PATH", "LANG", "LC_ALL", "TZ"}}

    async def invoke(bundle, command):
        result = await asyncio.to_thread(subprocess.run,
            [str(bundle.path / "nebula-core"), command, "--data-dir", str(data)],
            check=True, capture_output=True, text=True, timeout=120, env=environment)
        return json.loads(result.stdout.strip().splitlines()[-1])

    connection = None
    try:
        connection = await invoke(bundles[0], "desktop-connect")
        assert connection["protocol"] == "nebula-desktop-runtime-v1"
        origin = f"http://{connection['host']}:{connection['port']}"
        async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=30,
            headers={"Authorization": "Bearer " + connection["ipc_token"]}) as client:
            identity = (await client.get("/api/v1/runtime/identity")).json()
            initial = (await client.get("/api/v1/deployment/status")).json()
            assert initial["api_version"] == "acceptance-v1"
            assert (await client.get("/api/v1/health")).status_code == 200
            candidate = await invoke(bundles[1], "desktop-connect")
            assert candidate == connection
            assert (await client.get("/api/v1/runtime/identity")).json() == identity
            assert (await client.get("/api/v1/deployment/status")).json()["api_version"] == "acceptance-v2"
            rollback = await invoke(bundles[0], "desktop-connect")
            assert rollback == connection
            assert (await client.get("/api/v1/deployment/status")).json()["api_version"] == "acceptance-v1"
            await invoke(bundles[1], "runtime-stop")
            async with asyncio.timeout(15):
                while True:
                    try:
                        await client.get("/api/v1/health", timeout=1)
                    except httpx.TransportError:
                        break
                    await asyncio.sleep(.05)
    finally:
        if connection is not None:
            await invoke(bundles[-1], "runtime-stop")
