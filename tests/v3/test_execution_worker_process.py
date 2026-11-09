"""Retained execution processes with the actual kernel and disposable services."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from nebula.v3.process_identity import same_process
from nebula.v3.domain import ChatTurn, ProviderProfile
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_http import internal_headers
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _API_WORKER, _TOKEN
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_http import server
from tests.v3.test_supervisor import supervised as supervised

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def execution_status(host, identity):
    client = await host.rpc(identity.runtime_id)
    try:
        return await client.call("execution.status/v1", {})
    finally:
        await client.close()


async def test_real_kernel_grant_delivery_and_supervisor_replacement(supervised):
    root, fixture = supervised.supervisor, supervised.managed
    await fixture.stage("v1", _API_WORKER)
    execution = await root.prepare_execution("v1")
    assert (await execution_status(root.host, execution))["state"] == "unassigned"
    lease = await root.attach_execution("admitted-project", execution.runtime_id)
    assert (
        await root.attach_execution("admitted-project", execution.runtime_id) == lease
    )
    with pytest.raises(CoordinationConflict, match="new process"):
        await root.attach_execution("synthetic-project", execution.runtime_id)
    api = await root.prepare_api("v1")
    await root.promote_api(
        api.runtime_id, expected_revision=(await root.coordinator.status()).revision
    )
    async with fixture.database.session() as session:
        from nebula.v3.database import RuntimeProcessRow

        process = await session.get(RuntimeProcessRow, api.runtime_id)
        secret = root.host.credentials.read(process.credential_reference)
    transport = httpx.AsyncHTTPTransport(uds=api.endpoint[7:])
    async with httpx.AsyncClient(
        transport=transport, base_url="http://synthetic", trust_env=False
    ) as client:

        def headers():
            return {
                **dict(
                    (key.decode(), value.decode())
                    for key, value in internal_headers(secret, "http", lane="work")
                ),
                "Authorization": "Bearer " + _TOKEN,
                "Idempotency-Key": "one-durable-project-change",
            }

        path = "/api/v1/engagements/admitted-project"
        body = {
            "changes": {"name": "Committed by retained execution"},
            "expected_revision": 1,
        }
        first = await client.patch(path, json=body, headers=headers())
        assert first.status_code in {200, 202}, first.text
        again = await client.patch(path, json=body, headers=headers())
        assert again.status_code in {200, 202}, again.text
        assert (
            first.headers["x-nebula-command-id"] == again.headers["x-nebula-command-id"]
        )
        async with asyncio.timeout(10):
            while True:
                read = await client.get(
                    path,
                    headers={
                        **dict(
                            (key.decode(), value.decode())
                            for key, value in internal_headers(secret, "http")
                        ),
                        "Authorization": "Bearer " + _TOKEN,
                    },
                )
                assert read.status_code == 200, read.text
                if read.json()["name"] == body["changes"]["name"]:
                    break
                await asyncio.sleep(0.05)
        assert read.json()["revision"] == 2
        assert read.headers["x-nebula-execution-version"] == "v1"
    assert not (fixture.data / "butler-memory.sqlite3").exists()
    assert (fixture.data / "runtime" / "workers" / execution.runtime_id).is_dir()
    assert (fixture.data / "diagnostics-settings.json").is_file()
    await root.close()
    assert same_process(execution.pid, execution.process_birth)
    assert same_process(api.pid, api.process_birth)
    replacement_host = fixture.new_host()
    replacement = RuntimeSupervisor(replacement_host, operator_token=_TOKEN)
    try:
        await replacement.start()
        assert (
            await replacement.attach_execution("admitted-project", execution.runtime_id)
            == lease
        )
        assert (await execution_status(replacement_host, execution))["state"] == "ready"
        assert same_process(execution.pid, execution.process_birth)
    finally:
        await replacement.close()


async def test_unavailable_host_service_does_not_create_a_local_fallback(managed):
    await managed.stage("v1", _API_WORKER)
    identity = await managed.host.spawn("v1", "execution")
    lease = await managed.coordinator.coordination.claim_project(
        "admitted-project", identity.runtime_id
    )
    client = await managed.host.rpc(identity.runtime_id)
    try:
        from dataclasses import asdict

        await client.call("execution.attach/v1", asdict(lease))
        async with asyncio.timeout(10):
            while True:
                status = await client.call("execution.status/v1", {})
                if status["state"] != "starting":
                    break
                await asyncio.sleep(0.05)
        assert status["state"] == "failed"
        assert (await client.call("execution.attach/v1", asdict(lease)))[
            "state"
        ] == "failed"
        assert not (managed.data / "butler-memory.sqlite3").exists()
        assert not (managed.data / "knowledge-index").exists()
    finally:
        await client.close()


_JOURNALED_WORKER = r"""
import asyncio, sys
from contextlib import asynccontextmanager
from pathlib import Path
sys.path.insert(0, __REPOSITORY__)
from tests.v3.test_chat import FakeProvider
from nebula.v3.execution_worker import ProjectExecution, project_application
from nebula.v3.providers import ModelStreamEvent, StreamEventType
from nebula.v3.runtime_processes import read_worker_bootstrap
from nebula.v3.runtime_worker import run_worker

@asynccontextmanager
async def project(context, lease):
    async with project_application(context, lease) as app:
        root = Path(context.bootstrap.data_root)
        class InertProvider(FakeProvider):
            async def stream(self, request):
                with (root / 'fixture-provider-starts').open('a') as output:
                    output.write('one\n')
                yield ModelStreamEvent(type=StreamEventType.STARTED)
                yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta='First ')
                while not (root / 'fixture-provider-release').exists():
                    await asyncio.sleep(.02)
                yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta='last')
                response=(await super().complete(request)).model_copy(update={'text':'First last'})
                yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=response)
        chat = app.state.control_assistant_work.chat
        chat.provider_factory = lambda profile: InertProvider(profile.id, local=True)
        chat.provider_scheduler.host_load = None
        yield app

@asynccontextmanager
async def execution(context):
    worker=ProjectExecution(context, application_factory=project)
    try:
        yield worker.app
    finally:
        await worker.close()

asyncio.run(run_worker(read_worker_bootstrap(sys.stdin.buffer), execution_factory=execution))
""".replace("__REPOSITORY__", repr(str(Path(__file__).resolve().parents[2])))


async def test_api_promotion_reconnects_durable_chat_without_restarting_producer(
    supervised,
):
    root, fixture = supervised.supervisor, supervised.managed
    await fixture.store.create(
        ProviderProfile(
            id="inert-provider",
            name="Inert fixture",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    await fixture.stage("v1", _JOURNALED_WORKER)
    await fixture.stage("v2", _API_WORKER)
    execution = await root.prepare_execution("v1")
    await root.attach_execution("admitted-project", execution.runtime_id)
    first_api = await root.prepare_api("v1")
    await root.promote_api(
        first_api.runtime_id,
        expected_revision=(await root.coordinator.status()).revision,
    )
    release = fixture.data / "fixture-provider-release"
    observed = []
    try:
        async with server(root.app) as endpoint:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=endpoint[7:]),
                base_url="http://synthetic-stable-listener",
                trust_env=False,
                timeout=20,
            ) as client:
                headers = {
                    "Authorization": "Bearer " + _TOKEN,
                    "Idempotency-Key": "one-chat-during-promotion",
                }
                body = {
                    "provider_id": "inert-provider",
                    "engagement_id": "admitted-project",
                    "model": "model-a",
                    "messages": [
                        {"role": "user", "content": "Synthetic API replacement"}
                    ],
                    "include_knowledge": False,
                    "stream": True,
                }
                async with client.stream(
                    "POST", "/api/v1/chat/completions", json=body, headers=headers
                ) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            payload = json.loads(line[6:])
                            observed.append(payload)
                            if payload.get("type") == "delta":
                                break
                    assert observed[-1]["delta"] == "First "
                    cursor = observed[-1]["sequence"]
                    epoch = observed[-1]["epoch"]
                    turn_id = next(
                        frame["turn_id"]
                        for frame in observed
                        if frame.get("type") == "started"
                    )
                    assert epoch == "durable-v1"
                    second_api = await root.prepare_api("v2")
                    await root.promote_api(
                        second_api.runtime_id,
                        expected_revision=(await root.coordinator.status()).revision,
                    )
                assert same_process(execution.pid, execution.process_birth)
                await root.host.retire(first_api.runtime_id)
                release.write_text("finish inert response")
                response = await client.get(
                    f"/api/v1/chat/turns/{turn_id}/events?after={cursor}&epoch={epoch}",
                    headers={"Authorization": "Bearer " + _TOKEN},
                )
                assert response.status_code == 200
                assert response.headers["x-nebula-api-version"] == "v2"
                rest = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                assert (
                    "".join(
                        item.get("delta", "")
                        for item in [*observed, *rest]
                        if item.get("type") == "delta"
                    )
                    == "First last"
                )
                assert all(
                    item["sequence"] > cursor and item["epoch"] == epoch
                    for item in rest
                )
                assert any(item.get("type") == "done" for item in rest)
                turns = await fixture.store.list_entities(ChatTurn)
                assert len(turns) == 1 and turns[0].id == turn_id
                assert (
                    fixture.data / "fixture-provider-starts"
                ).read_text().splitlines() == ["one"]
                assert same_process(execution.pid, execution.process_birth)
                assert not same_process(first_api.pid, first_api.process_birth)
    finally:
        release.write_text("fixture cleanup")


async def test_detached_acceptance_runs_two_chats_in_one_project_through_api_replacement(
    supervised,
):
    from sqlalchemy import select
    from nebula.v3.database import (
        RuntimeChatBindingRow,
        RuntimeCommandRow,
        RuntimeResourceRow,
    )

    root, fixture = supervised.supervisor, supervised.managed
    await fixture.store.create(
        ProviderProfile(
            id="inert-provider",
            name="Inert fixture",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    source = _JOURNALED_WORKER.replace(
        "chat.provider_scheduler.host_load = None",
        "from nebula.v3.provider_scheduler import ProviderScheduler, ProviderSchedulerConfig\n        chat.provider_scheduler = ProviderScheduler(app.state.store, worker_id=chat.worker_id, config=ProviderSchedulerConfig(provider_limit=2, background_limit=1), runtime_capacity=chat.runtime_capacity)",
    )
    await fixture.stage("v1", source, features=["chat.accept/v1"])
    await fixture.stage("v2", _API_WORKER, features=["chat.accept/v1"])
    limits = await root.shared_services.capacity.status()
    await root.shared_services.capacity.configure(
        {"provider": 2, "background": 1, "tool": 2, "cpu": 2},
        expected_revision=limits["revision"],
    )
    execution = await root.prepare_execution("v1")
    await root.attach_execution("admitted-project", execution.runtime_id)
    first_api = await root.prepare_api("v1")
    await root.promote_api(
        first_api.runtime_id,
        expected_revision=(await root.coordinator.status()).revision,
    )
    release = fixture.data / "fixture-provider-release"
    body = {
        "provider_id": "inert-provider",
        "engagement_id": "admitted-project",
        "model": "model-a",
        "messages": [{"role": "user", "content": "Two independent chats"}],
        "include_knowledge": False,
        "stream": True,
    }

    async def capture(client, key):
        headers = {"Authorization": "Bearer " + _TOKEN, "Idempotency-Key": key}
        async with client.stream(
            "POST", "/api/v1/chat/completions", json=body, headers=headers
        ) as response:
            assert response.status_code == 200
            received = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    frame = json.loads(line[6:])
                    received.append(frame)
                    if frame.get("type") == "delta":
                        break
            assert received[-1]["delta"] == "First "
            return {
                "command_id": response.headers["x-nebula-command-id"],
                "turn_id": next(
                    frame["turn_id"]
                    for frame in received
                    if frame.get("type") == "started"
                ),
                "sequence": received[-1]["sequence"],
                "epoch": received[-1]["epoch"],
            }

    try:
        async with server(root.app) as endpoint:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=endpoint[7:]),
                base_url="http://synthetic-listener",
                trust_env=False,
                timeout=20,
            ) as client:
                first = await capture(client, "parallel-first")
                second = await capture(client, "parallel-second")
                assert first["turn_id"] != second["turn_id"]
                assert (
                    fixture.data / "fixture-provider-starts"
                ).read_text().splitlines() == ["one", "one"]
                async with asyncio.timeout(8):
                    while True:
                        async with fixture.database.session() as session:
                            commands = list(
                                await session.scalars(select(RuntimeCommandRow))
                            )
                            resources = list(
                                await session.scalars(
                                    select(RuntimeResourceRow).where(
                                        RuntimeResourceRow.state == "active"
                                    )
                                )
                            )
                            bindings = list(
                                await session.scalars(select(RuntimeChatBindingRow))
                            )
                        if len(commands) == 2 and all(
                            command.state == "succeeded" for command in commands
                        ):
                            break
                        await asyncio.sleep(0.02)
                assert {command.name for command in commands} == {"chat.accept/v1"}
                assert len(bindings) == 2 and len(resources) == 2
                assert {resource.kind for resource in resources} == {"provider_turn"}
                assert (await root.shared_services.capacity.status())["used"][
                    "provider"
                ] == 2
                new_api = await root.prepare_api("v2")
                await root.promote_api(
                    new_api.runtime_id,
                    expected_revision=(await root.coordinator.status()).revision,
                )
                await root.host.retire(first_api.runtime_id)
                retry = await capture(client, "parallel-first")
                assert (
                    retry["command_id"] == first["command_id"]
                    and retry["turn_id"] == first["turn_id"]
                )
                assert (
                    fixture.data / "fixture-provider-starts"
                ).read_text().splitlines() == ["one", "one"]
                release.write_text("finish both inert producers")
                for accepted in (first, second):
                    response = await client.get(
                        f"/api/v1/chat/turns/{accepted['turn_id']}/events?after={accepted['sequence']}&epoch={accepted['epoch']}",
                        headers={"Authorization": "Bearer " + _TOKEN},
                    )
                    assert (
                        response.status_code == 200
                        and response.headers["x-nebula-api-version"] == "v2"
                    )
                    frames = [
                        json.loads(line[6:])
                        for line in response.text.splitlines()
                        if line.startswith("data: ")
                    ]
                    assert (
                        "".join(
                            frame.get("delta", "")
                            for frame in frames
                            if frame.get("type") == "delta"
                        )
                        == "last"
                    )
                    assert any(frame.get("type") == "done" for frame in frames)
                    status = await client.get(
                        "/api/v1/runtime/requests/" + accepted["command_id"],
                        headers={"Authorization": "Bearer " + _TOKEN},
                    )
                    assert status.json()["turn_id"] == accepted["turn_id"]
                    assert status.json()["state"] == "succeeded"
                assert len(await fixture.store.list_entities(ChatTurn)) == 2
                async with fixture.database.session() as session:
                    assert (
                        list(
                            await session.scalars(
                                select(RuntimeResourceRow).where(
                                    RuntimeResourceRow.state != "closed"
                                )
                            )
                        )
                        == []
                    )
                assert same_process(execution.pid, execution.process_birth)
    finally:
        release.write_text("fixture cleanup")


async def test_explicit_resume_detaches_and_replays_its_new_segment_after_api_replacement(
    supervised,
):
    from sqlalchemy import select
    from nebula.v3.database import RuntimeCommandRow
    from nebula.v3.domain import ChatTurnStatus

    root, fixture = supervised.supervisor, supervised.managed
    await fixture.store.create(
        ProviderProfile(
            id="inert-provider",
            name="Inert fixture",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    features = ["chat.accept/v1", "chat.resume/v1"]
    await fixture.stage("v1", _JOURNALED_WORKER, features=features)
    await fixture.stage("v2", _API_WORKER, features=features)
    execution = await root.prepare_execution("v1")
    await root.attach_execution("admitted-project", execution.runtime_id)
    first_api = await root.prepare_api("v1")
    await root.promote_api(
        first_api.runtime_id,
        expected_revision=(await root.coordinator.status()).revision,
    )
    release = fixture.data / "fixture-provider-release"
    starts = fixture.data / "fixture-provider-starts"
    auth = {"Authorization": "Bearer " + _TOKEN}
    body = {
        "provider_id": "inert-provider",
        "engagement_id": "admitted-project",
        "model": "model-a",
        "messages": [{"role": "user", "content": "Resume fixture"}],
        "include_knowledge": False,
        "stream": True,
    }

    async def capture(client, path, key, **kwargs):
        async with client.stream(
            "POST", path, headers={**auth, "Idempotency-Key": key}, **kwargs
        ) as response:
            assert response.status_code == 200, await response.aread()
            frames = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if frames[-1].get("type") == "delta":
                        break
            assert frames[-1]["delta"] == "First "
            return response.headers["x-nebula-command-id"], frames

    try:
        async with server(root.app) as endpoint:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=endpoint[7:]),
                base_url="http://synthetic-listener",
                trust_env=False,
                timeout=20,
            ) as client:
                original, frames = await capture(
                    client, "/api/v1/chat/completions", "create", json=body
                )
                turn_id = next(
                    frame["turn_id"]
                    for frame in frames
                    if frame.get("type") == "started"
                )
                release.write_text("Finish initial segment")
                await client.get(
                    f"/api/v1/chat/turns/{turn_id}/events?after={frames[-1]['sequence']}",
                    headers=auth,
                )
                async with asyncio.timeout(8):
                    while (
                        await fixture.store.get(ChatTurn, turn_id)
                    ).status != ChatTurnStatus.COMPLETE:
                        await asyncio.sleep(0.02)
                # Synthetic crash-recovery state, using the actual saved request.
                turn = await fixture.store.get(ChatTurn, turn_id)
                await fixture.store.update(
                    ChatTurn,
                    turn_id,
                    {"status": ChatTurnStatus.INTERRUPTED, "final_message_id": None},
                    expected_revision=turn.revision,
                )
                release.unlink()
                path = f"/api/v1/chat/turns/{turn_id}/resume"
                resumed, new_frames = await capture(client, path, "resume")
                assert resumed != original
                assert (
                    min(frame["sequence"] for frame in new_frames)
                    > frames[-1]["sequence"]
                )
                assert not any(frame.get("type") == "done" for frame in new_frames)
                assert starts.read_text().splitlines() == ["one", "one"]
                attached, attached_frames = await capture(
                    client, path, "attach-existing"
                )
                assert attached != resumed and attached_frames == new_frames
                assert starts.read_text().splitlines() == ["one", "one"]
                # Both resume commands finish while their producer is still held.
                async with asyncio.timeout(8):
                    while True:
                        async with fixture.database.session() as session:
                            commands = list(
                                await session.scalars(select(RuntimeCommandRow))
                            )
                        if len(commands) == 3 and all(
                            command.state == "succeeded" for command in commands
                        ):
                            break
                        await asyncio.sleep(0.02)
                assert [command.name for command in commands].count(
                    "chat.resume/v1"
                ) == 2
                new_api = await root.prepare_api("v2")
                await root.promote_api(
                    new_api.runtime_id,
                    expected_revision=(await root.coordinator.status()).revision,
                )
                await root.host.retire(first_api.runtime_id)
                retry, retry_frames = await capture(client, path, "resume")
                assert retry == resumed and retry_frames == new_frames
                assert starts.read_text().splitlines() == ["one", "one"]
                release.write_text("Finish resumed producer")
                response = await client.get(
                    f"/api/v1/runtime/requests/{resumed}/response?after={new_frames[-1]['sequence']}",
                    headers=auth,
                )
                assert (
                    response.status_code == 200
                    and response.headers["x-nebula-api-version"] == "v2"
                )
                assert (
                    '"delta":"last"' in response.text and "event: done" in response.text
                )
                assert '"delta":"First "' not in response.text
                assert len(await fixture.store.list_entities(ChatTurn)) == 1
                assert same_process(execution.pid, execution.process_birth)
    finally:
        release.write_text("Fixture cleanup")


_HANDOFF_WORKER = r"""
import asyncio, sys
from contextlib import asynccontextmanager
from pathlib import Path
sys.path.insert(0, __REPOSITORY__)
from tests.v3.test_chat import FakeProvider
import nebula.v3.execution_kernel as kernel
from nebula.v3.execution_worker import ProjectExecution, project_application
from nebula.v3.providers import ModelStreamEvent, StreamEventType
from nebula.v3.runtime_processes import read_worker_bootstrap
from nebula.v3.runtime_worker import run_worker

@asynccontextmanager
async def project(context, lease):
    root = Path(context.bootstrap.data_root)
    class InertProvider(FakeProvider):
        async def stream(self, request):
            with (root / 'fixture-provider-starts').open('a') as output:
                output.write(context.bootstrap.identity.version + '\n')
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta='First ')
            while not (root / 'fixture-provider-release').exists():
                await asyncio.sleep(.02)
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta='last')
            response = (await super().complete(request)).model_copy(update={'text':'First last'})
            yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=response)
    original = kernel.create_execution_kernel
    def configured_kernel(*args, **kwargs):
        app = original(*args, **kwargs)
        chat = app.state.control_assistant_work.chat
        chat.provider_factory = lambda profile: InertProvider(profile.id, local=True)
        chat.provider_scheduler.host_load = None
        return app
    # Startup restoration captures its provider before the app yields. Install
    # the inert dependency at construction, before any lifespan controller runs.
    kernel.create_execution_kernel = configured_kernel
    try:
        async with project_application(context, lease) as app:
            yield app
    finally:
        kernel.create_execution_kernel = original

@asynccontextmanager
async def execution(context):
    worker = ProjectExecution(context, application_factory=project)
    try:
        yield worker.app
    finally:
        await worker.close()

asyncio.run(run_worker(read_worker_bootstrap(sys.stdin.buffer), execution_factory=execution))
""".replace("__REPOSITORY__", repr(str(Path(__file__).resolve().parents[2])))


async def test_active_execution_handoff_preserves_queued_turns_while_another_project_is_pinned(
    supervised,
):
    from sqlalchemy import select
    from nebula.v3.database import RuntimeCommandRow
    from nebula.v3.deployment_protocol import ResourceSnapshot
    from nebula.v3.domain import Engagement

    root, fixture = supervised.supervisor, supervised.managed
    await fixture.store.create(
        ProviderProfile(
            id="inert-provider",
            name="Inert",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    await fixture.store.create(
        Engagement(id="pinned-project", name="Pinned synthetic project")
    )
    features = ["chat.accept/v1", "chat.resume/v1", "project.handoff/v1"]
    await fixture.stage("v1", _HANDOFF_WORKER, features=features)
    await fixture.stage("v2", _HANDOFF_WORKER, features=features)
    limits = await root.shared_services.capacity.status()
    await root.shared_services.capacity.configure(
        {"provider": 1, "background": 1, "tool": 2, "cpu": 2},
        expected_revision=limits["revision"],
    )
    source = await root.prepare_execution("v1")
    await root.attach_execution("admitted-project", source.runtime_id)
    pinned = await root.prepare_execution("v1")
    pinned_lease = await root.attach_execution("pinned-project", pinned.runtime_id)
    await root.coordinator.resource(
        pinned_lease,
        ResourceSnapshot(
            id="synthetic-terminal",
            project_id="pinned-project",
            runtime_id=pinned.runtime_id,
            fencing_token=pinned_lease.fencing_token,
            kind="terminal",
            state="active",
            restoration="pinned",
            detail="The inert terminal has no restoration adapter",
        ),
    )
    candidate = await root.prepare_execution("v2")
    pinned_candidate = await root.prepare_execution("v2")
    api = await root.prepare_api("v2")
    await root.promote_api(
        api.runtime_id, expected_revision=(await root.coordinator.status()).revision
    )
    release = fixture.data / "fixture-provider-release"
    starts = fixture.data / "fixture-provider-starts"
    auth = {"Authorization": "Bearer " + _TOKEN}
    base_body = {
        "provider_id": "inert-provider",
        "engagement_id": "admitted-project",
        "model": "model-a",
        "include_knowledge": False,
        "stream": True,
    }
    pending = None

    async def capture(client, key, until):
        async with client.stream(
            "POST",
            "/api/v1/chat/completions",
            json={**base_body, "messages": [{"role": "user", "content": key}]},
            headers={**auth, "Idempotency-Key": key},
        ) as response:
            assert response.status_code == 200, await response.aread()
            frames = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if frames[-1].get("type") == until:
                        return response.headers["x-nebula-command-id"], frames

    async def phases():
        return {
            item.project_id: item for item in (await root.coordinator.status()).projects
        }

    try:
        async with server(root.app) as endpoint:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=endpoint[7:]),
                base_url="http://synthetic-listener",
                trust_env=False,
                timeout=45,
            ) as client:
                first, first_frames = await capture(client, "admitted-first", "delta")
                first_turn = next(
                    frame["turn_id"]
                    for frame in first_frames
                    if frame.get("type") == "started"
                )
                second, queued_frames = await capture(
                    client, "accepted-before-handoff", "queued"
                )
                second_turn = queued_frames[-1]["turn_id"]
                await root.request_project_handoff(
                    "admitted-project", candidate.runtime_id
                )
                await root.request_project_handoff(
                    "pinned-project", pinned_candidate.runtime_id
                )
                async with asyncio.timeout(12):
                    while True:
                        state = await phases()
                        if (
                            state["admitted-project"].phase == "finishing"
                            and state["pinned-project"].blockers
                        ):
                            break
                        await asyncio.sleep(0.05)
                assert state["admitted-project"].runtime_id == source.runtime_id
                assert state["admitted-project"].admission_held
                assert state["pinned-project"].runtime_id == pinned.runtime_id
                assert not state["pinned-project"].admission_held
                assert any(
                    "terminal" in detail for detail in state["pinned-project"].blockers
                )
                pending = asyncio.create_task(
                    client.post(
                        "/api/v1/chat/completions",
                        json={
                            **base_body,
                            "messages": [
                                {"role": "user", "content": "Accepted during drain"}
                            ],
                        },
                        headers={**auth, "Idempotency-Key": "during-handoff"},
                    )
                )
                async with asyncio.timeout(8):
                    while True:
                        async with fixture.database.session() as session:
                            commands = list(
                                await session.scalars(select(RuntimeCommandRow))
                            )
                        if len(commands) == 3:
                            break
                        await asyncio.sleep(0.02)
                assert (
                    next(
                        command
                        for command in commands
                        if command.idempotency_key == "during-handoff"
                    ).state
                    == "queued"
                )
                assert starts.read_text().splitlines() == ["v1"]
                release.write_text("Finish original version")
                finished = await client.get(
                    f"/api/v1/chat/turns/{first_turn}/events?after={first_frames[-1]['sequence']}",
                    headers=auth,
                )
                assert "event: done" in finished.text
                async with asyncio.timeout(30):
                    while (await phases())["admitted-project"].phase != "verified":
                        await asyncio.sleep(0.05)
                state = await phases()
                assert state["admitted-project"].runtime_id == candidate.runtime_id
                assert state["pinned-project"].runtime_id == pinned.runtime_id
                # Reconnect to the receipt accepted on the old owner. Its queued
                # segment may end during detach; the fresh journal continues it.
                async with asyncio.timeout(15):
                    while (
                        await fixture.store.get(ChatTurn, second_turn)
                    ).final_message_id is None:
                        await asyncio.sleep(0.05)
                replay = await client.get(
                    f"/api/v1/runtime/requests/{second}/response", headers=auth
                )
                assert (
                    "event: done" in replay.text
                    and "event: cancelled" not in replay.text
                    and "event: paused" not in replay.text
                )
                third = await asyncio.wait_for(pending, 15)
                assert third.status_code == 200 and "event: done" in third.text
                assert starts.read_text().splitlines() == ["v1", "v2", "v2"]
                assert len(await fixture.store.list_entities(ChatTurn)) == 3
                assert same_process(pinned.pid, pinned.process_birth)
                await root.host.retire(source.runtime_id)
                assert not same_process(source.pid, source.process_birth)
    finally:
        release.write_text("Fixture cleanup")
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
