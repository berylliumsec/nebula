"""Actual retained source/API/execution continuity, on disposable SQL only."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

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
    assert refused.status_code == 409 and "Core reserve needs at least" in refused.text
    assert (await continuity.control("inspect"))["processes"] == []
    current = (await continuity.client.get("/api/v1/memory-budgets")).json()
    assert current["policy"]["reserve_bytes"] == 2 * GIB
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
                    if job["state"] == "finished":
                        ended = await task
                        raise AssertionError(f"allocator ended before live application: {ended.text}")
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
        selected_environment = {**environment, "NEBULA_RUNTIME_BUNDLE": str(bundle.path)}
        result = await asyncio.to_thread(subprocess.run,
            [str(bundle.path / "nebula-core"), command, "--data-dir", str(data)],
            check=True, capture_output=True, text=True, timeout=120, env=selected_environment)
        return json.loads(result.stdout)

    connection = None
    stopped = False
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

            async def delivered(response):
                async with asyncio.timeout(90):
                    while response.status_code == 202:
                        document = response.json()
                        saved_id = document.get("command_id") or document["receipt"]["command_id"]
                        response = await client.get(f"/api/v1/runtime/requests/{saved_id}/response")
                        if response.status_code == 202:
                            await asyncio.sleep(.05)
                assert response.status_code in {200, 201}, response.text
                return response

            project = (await delivered(await client.post("/api/v1/engagements", json={
                "name": "Actual runtime-service project", "metadata": {},
            }, headers={"Idempotency-Key": "service-create-project"}))).json()
            listed = await client.get("/api/v1/engagements")
            assert listed.status_code == 200 and any(item["id"] == project["id"] for item in listed.json())
            provider = (await delivered(await client.post("/api/v1/providers", json={
                "name": "Inert runtime-service provider", "provider_type": "vllm", "is_local": True,
                "enabled": True, "model_allowlist": ["model-a"],
                "metadata": {"default_model": "model-a", "options": {"context_window": 32768}},
            }, headers={"Idempotency-Key": "service-create-provider"}))).json()
            chat = await delivered(await client.post("/api/v1/chat/completions", json={
                "provider_id": provider["id"], "engagement_id": project["id"], "model": "model-a",
                "include_knowledge": False, "stream": True,
                "messages": [{"role": "user", "content": "One actual runtime-service turn"}],
            }, headers={"Idempotency-Key": "service-first-chat"}))
            assert '"delta":"First "' in chat.text and "event: done" in chat.text
            starts = [json.loads(line) for line in (data / "acceptance-events.jsonl").read_text().splitlines()]
            assert len([item for item in starts if item["event"] == "provider-start"]) == 1
            async def operation(action, parameters):
                response = await client.post("/api/v1/deployment/operations", json={
                    "operation_id": action + "-" + uuid4().hex,
                    "action": action, "parameters": parameters,
                })
                assert response.status_code == 200, response.text
                value = response.json()
                assert value["state"] == "succeeded", value
                return value["result"]

            await operation("stage", {"source": str(bundles[1].path), "manifest_sha256": bundles[1].manifest_sha256})
            prepared = await operation("prepare_api", {"version": "acceptance-v2"})
            revision = (await client.get("/api/v1/deployment/status")).json()["revision"]
            await operation("promote_api", {"runtime_id": prepared["runtime_id"], "expected_revision": revision})
            candidate = await invoke(bundles[1], "desktop-connect")
            assert candidate == connection
            assert (await client.get("/api/v1/runtime/identity")).json() == identity
            assert (await client.get("/api/v1/deployment/status")).json()["api_version"] == "acceptance-v2"
            current = (await client.get("/api/v1/engagements/" + project["id"])).json()
            await delivered(await client.patch("/api/v1/engagements/" + project["id"], json={
                "expected_revision": current["revision"], "changes": {"name": "Saved under service candidate"},
            }, headers={"Idempotency-Key": "service-candidate-edit"}))
            revision = (await client.get("/api/v1/deployment/status")).json()["revision"]
            await operation("rollback_code", {"version": "acceptance-v1", "expected_revision": revision})
            rollback = await invoke(bundles[0], "desktop-connect")
            assert rollback == connection
            assert (await client.get("/api/v1/deployment/status")).json()["api_version"] == "acceptance-v1"
            assert (await client.get("/api/v1/engagements/" + project["id"])).json()["name"] == "Saved under service candidate"
            shutdown = await invoke(bundles[1], "runtime-stop")
            assert shutdown["state"] in {"succeeded", "stopped"}, shutdown
            async with asyncio.timeout(15):
                while True:
                    try:
                        await client.get("/api/v1/health", timeout=1)
                    except httpx.TransportError:
                        stopped = True
                        break
                    await asyncio.sleep(.05)
    finally:
        if connection is not None and not stopped:
            await invoke(bundles[-1], "runtime-stop")
        if stopped:
            remove_owned_tree(data)


async def test_prepared_restoration_holds_queued_provider_until_host_activation(continuity):
    source = (continuity.bundles[0].path / "source/nebula/v3/execution_worker.py").read_text()
    if '"execution.activate/v1"' not in source:
        pytest.skip("selected source predates explicit restoration activation")
    await continuity.ready()
    continuity.hold("hold-provider")
    await continuity.capture("original-active-turn")
    continuity.hold("hold-activation")
    target = await continuity.control("handoff", version="acceptance-v2", project_id="continuity-project")
    await continuity.wait_project("finishing")
    queued = await continuity.client.post("/api/v1/chat/completions", json={
        "provider_id": "continuity-provider", "engagement_id": "continuity-project",
        "model": "model-a", "include_knowledge": False, "stream": True,
        "messages": [{"role": "user", "content": "accepted-during-restoration"}],
    }, headers={"Idempotency-Key": "accepted-during-restoration"})
    assert queued.status_code == 202, queued.text
    command_id = queued.json()["receipt"]["command_id"]
    continuity.release("hold-provider")
    async with asyncio.timeout(90):
        while True:
            observed = await continuity.control("inspect")
            if any(item["event"] == "activation-wait" for item in observed["events"]):
                break
            await asyncio.sleep(.05)
    status = await continuity.control("execution-status", runtime_id=target["runtime_id"])
    assert status["state"] == "prepared" and status["activation_started"] is True
    assert len([item for item in observed["events"] if item["event"] == "provider-start"]) == 1
    from nebula.v3.database import RuntimeCommandRow
    async with continuity.database.session() as session:
        command = await session.get(RuntimeCommandRow, command_id)
        assert command.state == "queued"
    continuity.release("hold-activation")
    await continuity.wait_project("verified")
    async with asyncio.timeout(30):
        while True:
            response = await continuity.client.get(f"/api/v1/runtime/requests/{command_id}/response")
            if response.status_code == 200 and "event: done" in response.text:
                break
            await asyncio.sleep(.05)
    observed = await continuity.control("inspect")
    starts = [item for item in observed["events"] if item["event"] == "provider-start"]
    assert [item["version"] for item in starts] == ["acceptance-v1", "acceptance-v2"]


async def test_expired_real_owner_keeps_unconfirmed_effect_held_without_replay(continuity):
    from sqlalchemy import select
    from nebula.v3.database import ProjectOwnershipRow, RuntimeCommandRow
    from nebula.v3.runtime_coordination import LeaseLost, ProjectLease, RuntimeCoordination
    from nebula.v3.process_identity import signal_process
    import signal
    import time

    source_text = (continuity.bundles[0].path / "source/nebula/v3/deployments.py").read_text()
    if "async def begin_recovery(" not in source_text:
        pytest.skip("selected source predates explicit expired-owner recovery")
    source, _api = await continuity.ready()
    continuity.hold("hold-effect")
    task = asyncio.create_task(continuity.client.post(
        "/api/v1/engagements/continuity-project/acceptance-effect",
        json={}, headers={"Idempotency-Key": "unconfirmed-synthetic-effect"},
    ))
    try:
        async with asyncio.timeout(30):
            while True:
                observed = await continuity.control("inspect")
                if any(item["event"] == "effect-entered" for item in observed["events"]):
                    break
                await asyncio.sleep(.05)
        async with continuity.database.session() as session:
            old = await session.get(ProjectOwnershipRow, "continuity-project")
            old_lease = ProjectLease(old.project_id, old.runtime_id, old.fencing_token)
        assert signal_process(source["pid"], source["process_birth"], signal.SIGKILL)
        async with asyncio.timeout(40):
            while True:
                async with continuity.database.session() as session:
                    owner = await session.get(ProjectOwnershipRow, "continuity-project")
                    expired = owner.lease_expires_at_ms <= int(time.time() * 1000)
                if expired:
                    break
                await asyncio.sleep(.1)
        prepared = await continuity.client.post("/api/v1/deployment/operations", json={
            "operation_id": "prepare-expired-recovery", "action": "prepare_execution",
            "parameters": {"version": "acceptance-v2"},
        })
        assert prepared.status_code == 200, prepared.text
        result = prepared.json()
        assert result["state"] == "succeeded", result
        target = result["result"]
        payload = {"operation_id": "recover-unconfirmed-effect", "action": "recover_project", "parameters": {
            "project_id": "continuity-project", "target_runtime_id": target["runtime_id"],
            "expected_owner": {"project_id": old_lease.project_id, "runtime_id": old_lease.runtime_id,
                               "fencing_token": old_lease.fencing_token}, "expected_handoff_revision": None,
        }}
        recovered = await continuity.client.post("/api/v1/deployment/operations", json=payload)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["state"] == "uncertain"
        repeated = await continuity.client.post("/api/v1/deployment/operations", json=payload)
        assert repeated.json() == recovered.json()
        async with continuity.database.session() as session:
            command = await session.scalar(select(RuntimeCommandRow).where(RuntimeCommandRow.idempotency_key == "unconfirmed-synthetic-effect"))
            assert command.state == "uncertain" and command.runtime_id == source["runtime_id"]
        with pytest.raises(LeaseLost):
            async with RuntimeCoordination(continuity.database).fenced(old_lease):
                pass
        observed = await continuity.control("inspect")
        assert len([item for item in observed["events"] if item["event"] == "effect-entered"]) == 1
    finally:
        continuity.release("hold-effect")
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
