"""Retained execution processes with the actual kernel and disposable services."""

import asyncio

import httpx
import pytest

from nebula.v3.process_identity import same_process
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_http import internal_headers
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _API_WORKER, _TOKEN
from tests.v3.test_runtime_processes import managed as managed
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
