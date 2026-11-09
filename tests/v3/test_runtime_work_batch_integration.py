"""Actual host capability and retained CLI Work batches on disposable SQL.

The role fixtures launch inert private peers. The public journey enters the
retained production desktop connector/runtime_service and invokes no provider,
harness, vendor, browser, or OCI runtime.
"""

import asyncio
import json
import os
import secrets
import signal
import sqlite3
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from nebula.v3.database import (
    DeploymentHostRow,
    DeploymentOperationRow,
    DeploymentReceiptRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
    RuntimeWorkBatchProjectRow,
)
from nebula.v3.deployment_protocol import GLOBAL_EXECUTION_PROJECT
from nebula.v3.domain import Engagement, ScopePolicy, WorkItem, WorkUpdate
from nebula.v3.process_identity import same_process, signal_process
from nebula.v3.runtime_host_services import host_service_client
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_launcher import discovery, operator_token
from nebula.v3.runtime_work_batches import WORK_BATCH_CAPABILITY, WORK_BATCH_FEATURE
from tests.v3.runtime_continuity_bundle import remove_owned_tree
from tests.v3.runtime_continuity_fixture import ContinuityFixture, completed_response
from tests.v3.test_runtime_continuity_acceptance import (
    continuity_bundles as continuity_bundles,
)
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_host_services import shared as shared
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def api_peer(shared, *, secret=None):
    await shared.managed.stage("work-batch-api-peer")
    identity = await shared.managed.host.spawn("work-batch-api-peer", "api")
    async with shared.managed.database.session() as session:
        row = await session.get(RuntimeProcessRow, identity.runtime_id)
        actual = shared.managed.host.credentials.read(row.credential_reference)
    client = host_service_client(
        SimpleNamespace(identity=identity), actual if secret is None else secret
    )
    shared.clients.append(client)
    return identity, client, actual


async def test_real_api_peer_has_only_work_capability_and_owned_readiness(shared):
    identity, client, _ = await api_peer(shared)
    expected = {
        "protocol": "nebula.work-batches/v1",
        "feature": WORK_BATCH_FEATURE,
        "ready": True,
    }
    assert await client.call(WORK_BATCH_CAPABILITY, {}) == expected
    assert set(shared.services._peers[identity.runtime_id].operations) == {
        WORK_BATCH_CAPABILITY
    }
    with pytest.raises(RuntimeIpcError, match=r"\(404\)"):
        await client.call("host.services/v1", {})
    with pytest.raises(RuntimeIpcError, match=r"\(404\)"):
        await shared.remote.client.call(WORK_BATCH_CAPABILITY, {})
    assert (
        WORK_BATCH_CAPABILITY
        not in shared.services._peers[shared.identity.runtime_id].operations
    )
    coordinator = shared.services.work_batches
    assert coordinator is not None and coordinator.task is not None
    await coordinator.close()
    assert coordinator.task.done()
    assert await client.call(WORK_BATCH_CAPABILITY, {}) == {
        **expected,
        "ready": False,
    }
    assert (await shared.remote.client.call("host.services/v1", {}))["memory"]


async def test_real_api_capability_rechecks_readiness_expiry_and_private_secret(shared):
    identity, client, actual = await api_peer(shared)
    assert (await client.call(WORK_BATCH_CAPABILITY, {}))["ready"]
    wrong = host_service_client(
        SimpleNamespace(identity=identity), secrets.token_bytes(32)
    )
    shared.clients.append(wrong)
    with pytest.raises(RuntimeIpcError, match=r"\(401\)"):
        await wrong.call(WORK_BATCH_CAPABILITY, {})
    assert (await client.call(WORK_BATCH_CAPABILITY, {}))["ready"]
    async with shared.managed.database.session() as session:
        await session.execute(
            update(RuntimeProcessRow)
            .where(RuntimeProcessRow.runtime_id == identity.runtime_id)
            .values(state="starting")
        )
    with pytest.raises(RuntimeIpcError, match=r"\(401\)"):
        await client.call(WORK_BATCH_CAPABILITY, {})
    async with shared.managed.database.session() as session:
        await session.execute(
            update(RuntimeProcessRow)
            .where(RuntimeProcessRow.runtime_id == identity.runtime_id)
            .values(state="ready")
        )
    assert (await client.call(WORK_BATCH_CAPABILITY, {}))["ready"]
    async with shared.managed.database.session() as session:
        await session.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id == identity.runtime_id)
            .values(lease_expires_at_ms=0)
        )
    with pytest.raises(RuntimeIpcError, match=r"\(401\)") as failure:
        await client.call(WORK_BATCH_CAPABILITY, {})
    assert actual.hex() not in str(failure.value)
    assert await shared.managed.store.count(WorkItem) == 0
    assert await shared.managed.store.count(WorkUpdate) == 0


async def command_state(fixture, key):
    async with fixture.database.session() as session:
        row = await session.scalar(
            select(RuntimeCommandRow).where(
                RuntimeCommandRow.actor_id == "operator",
                RuntimeCommandRow.idempotency_key == key,
            )
        )
        return (
            {"id": row.id, "state": row.state, "project_id": row.project_id}
            if row is not None
            else None
        )


async def wait_sql(fixture, key, *, terminal=False, request=None):
    observed = []
    async with asyncio.timeout(45):
        while True:
            current = await command_state(fixture, key)
            if current is not None:
                if not observed or observed[-1] != current:
                    observed.append(current)
                if not terminal or current["state"] not in {"queued", "running"}:
                    return current, observed
            if request is not None and request.done():
                response = request.result()
                assert response.status_code in {200, 202}, response.text
            await asyncio.sleep(0.02)


async def operation(client, action, parameters):
    identity = action + "-" + uuid4().hex
    response = await client.post(
        "/api/v1/deployment/operations",
        json={"operation_id": identity, "action": action, "parameters": parameters},
    )
    assert response.status_code == 200, response.text
    value = response.json()
    async with asyncio.timeout(60):
        while value["state"] == "running":
            response = await client.get("/api/v1/deployment/operations/" + identity)
            assert response.status_code == 200, response.text
            value = response.json()
            await asyncio.sleep(0.02)
    assert value["state"] == "succeeded", value
    return value["result"]


async def no_execution(fixture):
    async with fixture.database.session() as session:
        assert (
            list(
                await session.scalars(
                    select(RuntimeProcessRow).where(
                        RuntimeProcessRow.role == "execution"
                    )
                )
            )
            == []
        )
        owners = list(await session.scalars(select(ProjectOwnershipRow)))
    assert all(row.runtime_id is None for row in owners)
    assert all(row.project_id != GLOBAL_EXECUTION_PROJECT for row in owners)


async def wait_exit(pid, birth, *, timeout=25):
    async with asyncio.timeout(timeout):
        while same_process(pid, birth):
            await asyncio.sleep(0.05)


async def preserve(fixture, destination):
    destination.mkdir(exist_ok=True)
    paths = [*fixture.root.glob("runtime/*.log")]
    paths += list(fixture.root.glob("runtime/workers/*/logs/*.log"))
    for index, path in enumerate(paths[:20]):
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - 65536))
            (destination / f"{index}-{path.name}").write_bytes(source.read(65536))
    if fixture.database_ready:
        async with fixture.database.session() as session:
            commands = list(await session.scalars(select(RuntimeCommandRow)))
            batches = list(await session.scalars(select(RuntimeWorkBatchProjectRow)))
            processes = list(await session.scalars(select(RuntimeProcessRow)))
            deployment = {}
            for model in (
                DeploymentHostRow,
                DeploymentOperationRow,
                DeploymentReceiptRow,
                RuntimeInstanceRow,
            ):
                rows = list(await session.scalars(select(model)))
                deployment[model.__tablename__] = [
                    {
                        column.name: getattr(row, column.name)
                        for column in model.__table__.columns
                    }
                    for row in rows
                ]
        (destination / "sql-state.json").write_text(
            json.dumps(
                {
                    **deployment,
                    "commands": [
                        {
                            "id": row.id,
                            "state": row.state,
                            "name": row.name,
                            "key": row.idempotency_key,
                        }
                        for row in commands
                    ],
                    "batches": [
                        {
                            "batch": row.batch_id,
                            "project": row.project_id,
                            "state": row.state,
                        }
                        for row in batches
                    ],
                    "processes": [
                        {
                            "id": row.runtime_id,
                            "role": row.role,
                            "pid": row.pid,
                            "birth": row.process_birth,
                            "state": row.state,
                            "version": row.version,
                            "updated_at_ms": row.updated_at_ms,
                        }
                        for row in processes
                    ],
                },
                indent=2,
            )
        )
        url = make_url(fixture.database.url)
        if url.get_backend_name() == "sqlite":
            assert url.database is not None
            with (
                sqlite3.connect(url.database) as source,
                sqlite3.connect(destination / "core.db") as target,
            ):
                source.backup(target)


async def test_retained_cli_import_progresses_without_api_and_keeps_batch_receipts(
    tmp_path, location, continuity_bundles
):
    bundles = continuity_bundles
    fixture = ContinuityFixture(tmp_path / "owned-core", location, bundles)
    fixture.database._bootstrap = False
    home = tmp_path / "private-home"
    home.mkdir(mode=0o700)
    assert operator_token(fixture.root, fixture.token) == fixture.token
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TZ", "TMPDIR"}
    }
    environment.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_CACHE_HOME=str(home / "cache"),
        XDG_DATA_HOME=str(home / "data"),
        NEBULA_V3_DATABASE_URL=str(location),
        NEBULA_V3_DB_ADMIN_RESERVE="8",
        NEBULA_V3_DB_OTHER_CONNECTIONS="12",
        NEBULA_V3_DB_PEAK_PROCESSES="8",
        NEBULA_V3_DB_POOL_SIZE="3",
        NEBULA_V3_DB_POOL_TIMEOUT="5",
    )
    connection = None
    stopped = False
    pending = None
    receipts = {
        "scope": "retained production CLI/backend only; verified older static UI; no browser/provider/vendor/OCI"
    }

    async def invoke(bundle, command):
        result = await asyncio.to_thread(
            subprocess.run,
            [
                str(bundle.path / "nebula-core"),
                command,
                "--data-dir",
                str(fixture.root),
            ],
            env={**environment, "NEBULA_RUNTIME_BUNDLE": str(bundle.path)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        path = tmp_path / (command + "-" + uuid4().hex + ".private.json")
        path.write_text(
            json.dumps(
                {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                indent=2,
            )
        )
        path.chmod(0o600)
        assert result.returncode == 0, (
            f"{command}: {result.stderr[-4000:]} (private receipt {path})"
        )
        return json.loads(result.stdout)

    try:
        connection = await invoke(bundles[0], "desktop-connect")
        assert connection["protocol"] == "nebula-desktop-runtime-v1"
        assert connection["ipc_token"] == fixture.token
        service = discovery(fixture.root)
        assert service is not None and same_process(
            service["pid"], service["process_birth"]
        )
        receipts["core"] = {
            key: service[key] for key in ("pid", "process_birth", "endpoint")
        }
        await fixture.client.aclose()
        fixture.origin = f"http://{connection['host']}:{connection['port']}"
        fixture.client = httpx.AsyncClient(
            base_url=fixture.origin,
            headers={"Authorization": "Bearer " + fixture.token},
            trust_env=False,
            timeout=90,
        )
        await fixture.database.initialize()
        fixture.database_ready = True
        client = fixture.client
        async with asyncio.timeout(90):
            while True:
                health = await client.get("/api/v1/health")
                status = await client.get("/api/v1/deployment/status")
                if (
                    health.status_code == 200
                    and health.json()["api_ready"]
                    and status.json()["api_version"] == "acceptance-v1"
                ):
                    break
                await asyncio.sleep(0.05)
        await no_execution(fixture)
        initial_identity = (await client.get("/api/v1/runtime/identity")).json()
        assert initial_identity["principal_id"] == "operator"
        parent = (
            await completed_response(
                client,
                await client.post(
                    "/api/v1/engagements",
                    headers={"Idempotency-Key": "batch-parent"},
                    json={"name": "Synthetic Work batch parent"},
                ),
            )
        ).json()
        body = {
            "source": "retained-integration",
            "projects": [
                {
                    "external_id": f"project-{index}",
                    "name": f"Imported synthetic project {index}",
                    "items": [
                        {
                            "external_id": f"item-{index}",
                            "title": f"Imported Work {index}",
                            "update": {
                                "summary": f"Imported progress {index}",
                                "status": "review",
                            },
                        }
                    ],
                }
                for index in (1, 2)
            ],
        }
        key = "lost-work-import-response"
        pending = asyncio.create_task(
            client.post(
                "/api/v1/work/import", json=body, headers={"Idempotency-Key": key}
            )
        )
        accepted, acceptance_states = await wait_sql(fixture, key, request=pending)
        assert accepted["project_id"] == GLOBAL_EXECUTION_PROJECT
        async with fixture.database.session() as session:
            original_api = await session.scalar(
                select(RuntimeProcessRow).where(
                    RuntimeProcessRow.role == "api",
                    RuntimeProcessRow.version == "acceptance-v1",
                )
            )
        assert original_api is not None and same_process(
            original_api.pid, original_api.process_birth
        )
        assert signal_process(
            original_api.pid, original_api.process_birth, signal.SIGKILL
        )
        # Only direct SQL observation follows acceptance. No status/response GET,
        # reconciliation RPC, or browser is allowed to drive the host's progress.
        terminal, progress = await wait_sql(fixture, key, terminal=True)
        assert terminal["state"] == "succeeded", terminal
        await wait_exit(original_api.pid, original_api.process_birth)
        await no_execution(fixture)
        receipts.update(
            accepted=accepted,
            acceptance_states=acceptance_states,
            progress_without_api=progress,
        )
        try:
            discarded = await pending
            if discarded.status_code == 503:
                assert discarded.json() == {
                    "detail": "The runtime connection is unavailable. Reconnect to read durable progress."
                }
            else:
                assert discarded.status_code in {200, 202}, discarded.text
            receipts["discarded_response"] = {"status": discarded.status_code}
        except httpx.TransportError as error:
            receipts["discarded_response"] = {"transport_error": type(error).__name__}
        pending = None
        # Health/version can still describe the killed process until the host
        # reconciles it. Observe the exact replacement before an explicit update.
        async with asyncio.timeout(90):
            while True:
                async with fixture.database.session() as session:
                    host = await session.get(DeploymentHostRow, "host")
                    old = await session.get(RuntimeProcessRow, original_api.runtime_id)
                    recovered = (
                        await session.get(RuntimeProcessRow, host.api_runtime_id)
                        if host.api_runtime_id
                        else None
                    )
                    recovery_revision = host.revision
                health = await client.get("/api/v1/health")
                status = await client.get("/api/v1/deployment/status")
                if (
                    old.state == "retired"
                    and not same_process(old.pid, old.process_birth)
                    and recovered is not None
                    and recovered.runtime_id != old.runtime_id
                    and recovered.version == "acceptance-v1"
                    and recovered.state == "ready"
                    and same_process(recovered.pid, recovered.process_birth)
                    and health.json()["api_ready"]
                    and status.json()["api_version"] == "acceptance-v1"
                ):
                    break
                await asyncio.sleep(0.05)
        receipts["automatic_api_recovery"] = {
            "runtime_id": recovered.runtime_id,
            "pid": recovered.pid,
            "birth": recovered.process_birth,
            "revision": recovery_revision,
            "old_state": old.state,
        }
        await operation(
            client,
            "stage",
            {
                "source": str(bundles[1].path),
                "manifest_sha256": bundles[1].manifest_sha256,
            },
        )
        api = await operation(client, "prepare_api", {"version": "acceptance-v2"})
        revision = (await client.get("/api/v1/deployment/status")).json()["revision"]
        await operation(
            client,
            "promote_api",
            {"runtime_id": api["runtime_id"], "expected_revision": revision},
        )
        status = (await client.get("/api/v1/deployment/status")).json()
        assert status["api_version"] == "acceptance-v2", status
        async with fixture.database.session() as session:
            host = await session.get(DeploymentHostRow, "host")
            assert host.api_runtime_id == api["runtime_id"]
            target = await session.get(RuntimeProcessRow, api["runtime_id"])
            old = await session.get(RuntimeProcessRow, original_api.runtime_id)
            inactive = list(
                await session.scalars(
                    select(RuntimeProcessRow).where(
                        RuntimeProcessRow.role == "api",
                        RuntimeProcessRow.state != "retired",
                        RuntimeProcessRow.runtime_id != api["runtime_id"],
                    )
                )
            )
        assert target.state == "ready" and target.version == "acceptance-v2"
        assert same_process(target.pid, target.process_birth)
        assert target.runtime_id != original_api.runtime_id
        assert old.state == "retired" and not same_process(old.pid, old.process_birth)
        for row in inactive:
            await operation(client, "retire", {"runtime_id": row.runtime_id})
            await wait_exit(row.pid, row.process_birth)
        assert (await client.get("/api/v1/runtime/identity")).json() == initial_identity
        receipts["replacement_api"] = {
            "id": target.runtime_id,
            "version": target.version,
            "pid": target.pid,
            "birth": target.process_birth,
        }
        lookup = await client.get(
            "/api/v1/runtime/requests/lookup", params={"idempotency_key": key}
        )
        assert (
            lookup.status_code == 200 and lookup.json()["command_id"] == accepted["id"]
        ), lookup.text
        assert lookup.json()["state"] == "succeeded"
        response = await client.get(
            f"/api/v1/runtime/requests/{accepted['id']}/response"
        )
        assert response.status_code == 200, response.text
        imported = response.json()
        ids = [entry["engagement_id"] for entry in imported["projects"]]
        assert len(ids) == len(set(ids)) == 2
        before = [await fixture.store.get(Engagement, identity) for identity in ids]
        for entry in imported["projects"]:
            project = await fixture.store.get(Engagement, entry["engagement_id"])
            assert (
                await fixture.store.get(ScopePolicy, project.scope_policy_id)
            ).engagement_id == project.id
            assert len(entry["items"]) == 1
            item = await fixture.store.get(WorkItem, entry["items"][0]["item_id"])
            check_in = await fixture.store.get(
                WorkUpdate, entry["items"][0]["update_id"]
            )
            assert item.engagement_id == check_in.engagement_id == project.id
        assert (
            await fixture.store.count(WorkItem)
            == await fixture.store.count(WorkUpdate)
            == 2
        )
        assert GLOBAL_EXECUTION_PROJECT not in {
            row.id for row in await fixture.store.list_entities(Engagement)
        }
        edit_key = "hierarchy-and-work-setting"
        edited = await client.patch(
            "/api/v1/work/projects",
            json={
                "project_ids": ids,
                "parent_engagement_id": parent["id"],
                "work_enabled": True,
            },
            headers={"Idempotency-Key": edit_key},
        )
        assert edited.status_code in {200, 202}, edited.text
        state, edit_progress = await wait_sql(fixture, edit_key, terminal=True)
        assert state["state"] == "succeeded"
        edited = await client.get(f"/api/v1/runtime/requests/{state['id']}/response")
        assert edited.status_code == 200 and edited.json() == {"updated": 2}, (
            edited.text
        )
        for original in before:
            current = await fixture.store.get(Engagement, original.id)
            assert current.parent_engagement_id == parent["id"] and current.work_enabled
            assert current.revision == original.revision + 1
        cycle_key = "refused-hierarchy-cycle"
        refused = await client.patch(
            "/api/v1/work/projects",
            json={"project_ids": [parent["id"]], "parent_engagement_id": ids[0]},
            headers={"Idempotency-Key": cycle_key},
        )
        assert refused.status_code in {200, 202, 409}, refused.text
        cycle, cycle_progress = await wait_sql(fixture, cycle_key, terminal=True)
        assert cycle["state"] == "failed"
        refused = await client.get(f"/api/v1/runtime/requests/{cycle['id']}/response")
        assert refused.status_code == 409, refused.text
        unchanged = await fixture.store.get(Engagement, parent["id"])
        assert (
            unchanged.revision == parent["revision"]
            and unchanged.parent_engagement_id is None
        )
        await no_execution(fixture)
        receipts.update(
            imported=imported,
            accepted=accepted,
            acceptance_states=acceptance_states,
            progress_without_api=progress,
            lookup=lookup.json(),
            edit_progress=edit_progress,
            cycle_progress=cycle_progress,
            stable_identity=initial_identity,
        )
        result = await invoke(bundles[1], "runtime-stop")
        assert result["observed_state"] == "stopped", result
        stopped = True
        receipts["shutdown"] = result
        assert not same_process(service["pid"], service["process_birth"])
        registered = discovery(fixture.root)
        assert registered is None
    except Exception as error:
        # diagnostic-expected: retain SQL authority before cleanup can retire
        # workers or change revisions; pytest preserves the original exception.
        receipts["failure_type"] = type(error).__name__
        try:
            await preserve(fixture, tmp_path / "failure-evidence")
        except Exception as evidence_error:
            # diagnostic-expected: an evidence failure cannot replace the case's
            # original failure; the private receipt records this separate error.
            receipts["failure_evidence_error"] = type(evidence_error).__name__
        raise
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if connection is not None and not stopped:
            try:
                result = await invoke(bundles[-1], "runtime-stop")
                stopped = result.get("observed_state") == "stopped"
                receipts["cleanup_shutdown"] = result
            except Exception as error:
                receipts["cleanup_shutdown_error"] = f"{type(error).__name__}: {error}"
                registered = discovery(fixture.root)
                if registered is not None:
                    receipts["cleanup_core"] = {
                        key: registered[key] for key in ("pid", "process_birth")
                    }
                    signal_process(
                        registered["pid"], registered["process_birth"], signal.SIGTERM
                    )
                    try:
                        await wait_exit(registered["pid"], registered["process_birth"])
                    except TimeoutError:
                        receipts["cleanup_forced_kill"] = registered["pid"]
                        signal_process(
                            registered["pid"],
                            registered["process_birth"],
                            signal.SIGKILL,
                        )
                        await wait_exit(registered["pid"], registered["process_birth"])
        try:
            await preserve(fixture, tmp_path / "evidence")
        finally:
            (tmp_path / "integration-receipt.json").write_text(
                json.dumps(receipts, indent=2)
            )
            try:
                await fixture.close()
            finally:
                remove_owned_tree(fixture.root)
