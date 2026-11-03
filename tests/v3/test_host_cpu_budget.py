"""Host-local policy, observed application and Butler access use real Core routes."""

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Engagement
from nebula.v3.host_cpu_budget import HostCpuBudget, HostCpuConflict, HostCpuUpdate
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore
from tests.v3.test_butler_capabilities import _scheduled_capabilities

HEADERS = {"Authorization": "Bearer synthetic-host-cpu"}


class Executor:
    capacity = 4.0
    limit = 2.0
    fail = False
    ignore = False

    def __init__(self):
        self.applied = []

    async def available(self):
        return self.capacity

    async def apply(self, value):
        self.applied.append(value)
        if self.fail:
            raise RuntimeError("Synthetic executor failure")
        if not self.ignore:
            self.limit = value

    async def observe(self):
        return self.limit


def fixture(root: Path, *, active=True):
    root.mkdir(parents=True, exist_ok=True)
    store = NebulaStore(root / "core.db")
    store.create(Engagement(id="project", name="Synthetic host project"))
    executor = Executor()
    service = HostCpuBudget(
        root / "host-cpu-budget.json",
        executor.available,
        apply=executor.apply if active else None,
        observe=executor.observe if active else None,
    )
    app = create_app(
        store,
        host_cpu_budget=service,
        auth_token="synthetic-host-cpu",
        execution_data_root=root / "execution",
    )
    return store, executor, service, app


def update(client, status, value):
    return client.put(
        "/api/v1/host-cpu-budget",
        headers=HEADERS,
        json={
            "host_id": status["host_id"],
            "expected_revision": status["revision"],
            "cpu_limit": value,
        },
    )


def test_host_policy_is_shared_persistent_and_independent_between_hosts(tmp_path):
    a, _, service, app = fixture(tmp_path / "a", active=False)
    b, _, _, other = fixture(tmp_path / "b", active=False)
    try:
        with TestClient(app) as client, TestClient(other) as second:
            initial = client.get("/api/v1/host-cpu-budget", headers=HEADERS)
            assert initial.headers["cache-control"] == "private, no-store"
            status = initial.json()
            assert status["cpu_limit"] is None and status["effective_cpu_limit"] == 2
            assert (
                status["apply_state"] == "inactive"
                and status["applied_cpu_limit"] is None
            )
            response = update(client, status, 3)
            assert response.status_code == 200
            saved = response.json()
            assert saved["cpu_limit"] == 3 and saved["revision"] == 2
            assert saved["apply_state"] == "inactive"
            a.create(
                Engagement(id="other-project", name="Another project on this host")
            )
            assert (
                client.get("/api/v1/host-cpu-budget", headers=HEADERS).json() == saved
            )
            foreign = second.get("/api/v1/host-cpu-budget", headers=HEADERS).json()
            assert (
                foreign["cpu_limit"] is None and foreign["host_id"] != saved["host_id"]
            )
        restored = HostCpuBudget(service.path, Executor().available)
        value = asyncio.run(restored.status())
        assert (
            value.cpu_limit == 3
            and value.revision == 2
            and value.host_id == saved["host_id"]
        )
        assert service.path.stat().st_mode & 0o777 == 0o600
    finally:
        a.database.dispose()
        b.database.dispose()


def test_custom_and_automatic_are_observed_and_reapplied_on_startup(tmp_path):
    store, executor, service, app = fixture(tmp_path)
    try:
        with TestClient(app) as client:
            status = client.get("/api/v1/host-cpu-budget", headers=HEADERS).json()
            assert status["apply_state"] == "applied" and executor.limit == 2
            saved = update(client, status, 3).json()
            assert saved["apply_state"] == "applied" and saved["applied_cpu_limit"] == 3
        executor.limit = 1
        restored = HostCpuBudget(
            service.path,
            executor.available,
            apply=executor.apply,
            observe=executor.observe,
        )
        asyncio.run(restored.startup())
        assert executor.limit == 3

        async def automatic():
            status = await restored.status()
            return await restored.update(
                HostCpuUpdate(
                    host_id=status.host_id,
                    expected_revision=status.revision,
                    cpu_limit=None,
                )
            )

        observed = asyncio.run(automatic())
        assert observed.cpu_limit is None and observed.applied_cpu_limit == 2
    finally:
        store.database.dispose()


@pytest.mark.parametrize("value", [0, -1, 0.001, 5, True, "3", {"value": 3}])
def test_invalid_limits_do_not_change_policy_or_executor(tmp_path, value):
    store, executor, _, app = fixture(tmp_path)
    try:
        with TestClient(app) as client:
            status = client.get("/api/v1/host-cpu-budget", headers=HEADERS).json()
            assert update(client, status, value).status_code == 422
            assert (
                client.get("/api/v1/host-cpu-budget", headers=HEADERS).json() == status
            )
            assert executor.applied == [2]
    finally:
        store.database.dispose()


def test_auth_host_identity_and_stale_revision_are_enforced(tmp_path):
    store, executor, _, app = fixture(tmp_path)
    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/host-cpu-budget").status_code == 401
            status = client.get("/api/v1/host-cpu-budget", headers=HEADERS).json()
            assert (
                client.put(
                    "/api/v1/host-cpu-budget",
                    json={
                        "host_id": status["host_id"],
                        "expected_revision": 1,
                        "cpu_limit": 3,
                    },
                ).status_code
                == 401
            )
            foreign = {**status, "host_id": "another-core-host"}
            assert update(client, foreign, 3).status_code == 409
            saved = update(client, status, 3).json()
            assert update(client, status, 1).status_code == 409
            assert (
                client.get("/api/v1/host-cpu-budget", headers=HEADERS).json() == saved
            )
            assert executor.applied == [2, 3]
    finally:
        store.database.dispose()


def test_concurrent_writes_have_one_revision_winner(tmp_path):
    executor = Executor()
    service = HostCpuBudget(
        tmp_path / "cpu.json",
        executor.available,
        apply=executor.apply,
        observe=executor.observe,
    )

    async def scenario():
        initial = await service.status()
        values = await asyncio.gather(
            *(
                service.update(
                    HostCpuUpdate(
                        host_id=initial.host_id, expected_revision=1, cpu_limit=value
                    )
                )
                for value in (1, 3)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(value, HostCpuConflict) for value in values) == 1
        saved = await service.status()
        assert saved.revision == 2 and saved.cpu_limit == executor.limit
        assert len(executor.applied) == 1

    asyncio.run(scenario())


def test_failed_or_unobserved_application_is_not_called_applied(tmp_path):
    store, executor, _, app = fixture(tmp_path)
    try:
        with TestClient(app) as client:
            status = client.get("/api/v1/host-cpu-budget", headers=HEADERS).json()
            executor.fail = True
            failed = update(client, status, 3).json()
            assert (
                failed["cpu_limit"] == 3
                and failed["apply_state"] == "failed"
                and failed["applied_cpu_limit"] == 2
            )
            executor.fail = False
            executor.ignore = True
            pending = update(client, failed, 3).json()
            assert (
                pending["apply_state"] == "pending"
                and pending["applied_cpu_limit"] == 2
            )
            executor.ignore = False
            applied = update(client, pending, 3).json()
            assert (
                applied["apply_state"] == "applied"
                and applied["applied_cpu_limit"] == 3
            )
    finally:
        store.database.dispose()


def test_capacity_reduction_preserves_choice_and_clamps_on_restart(tmp_path):
    executor = Executor()
    service = HostCpuBudget(
        tmp_path / "cpu.json",
        executor.available,
        apply=executor.apply,
        observe=executor.observe,
    )

    async def scenario():
        status = await service.status()
        await service.update(
            HostCpuUpdate(host_id=status.host_id, expected_revision=1, cpu_limit=3)
        )
        executor.capacity = 2
        status = await service.status()
        assert (
            status.cpu_limit == 3
            and status.effective_cpu_limit == 2
            and status.capacity_clamped
        )
        assert status.apply_state == "pending"
        await service.startup()
        assert (await service.status()).applied_cpu_limit == 2
        assert (await service.status()).cpu_limit == 3

    asyncio.run(scenario())


def test_corrupt_config_is_visible_without_overwriting_it_or_blocking_core(tmp_path):
    store, _, service, app = fixture(tmp_path)
    service.path.write_text("not valid JSON")
    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/health", headers=HEADERS).status_code == 200
            assert (
                client.get("/api/v1/host-cpu-budget", headers=HEADERS).status_code
                == 503
            )
            assert service.path.read_text() == "not valid JSON"
    finally:
        store.database.dispose()


def test_butler_discovers_reads_and_saves_the_host_policy_with_readback(tmp_path):
    store, _, _, app = fixture(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    butler = ButlerCapabilities(
        store, artifacts, "project", work=app.state.control_assistant_work
    )
    gateway = SettingsAssistantApi(app, "synthetic-host-cpu")

    async def scenario():
        matches = await butler.search({"query": "host CPU budget quota"})
        assert any(
            value["operation"] == "nebula.work.host_cpu_budget"
            for value in matches["matches"]
        )
        status = await butler.invoke(
            {"operation": "nebula.work.host_cpu_budget", "arguments": {}}, change=False
        )
        assert (
            gateway.schema(path="host-cpu-budget", method="PUT")["path"]
            == "/api/v1/host-cpu-budget"
        )
        saved = await gateway.request(
            "project",
            method="PUT",
            path="host-cpu-budget",
            body={
                "host_id": status["host_id"],
                "expected_revision": status["revision"],
                "cpu_limit": 3,
            },
        )
        assert saved["read_back"] and saved["result"]["cpu_limit"] == 3
        assert saved["result"]["apply_state"] == "applied"
        assert saved["client_action"]["kind"] == "host_cpu_budget_refresh"

    try:
        asyncio.run(scenario())
    finally:
        store.database.dispose()


def test_scheduled_butler_reads_host_cpu_policy(tmp_path):
    store, _, _, _, _, _, butler = _scheduled_capabilities(tmp_path)

    async def scenario():
        described = await butler.describe(
            {"operation": "nebula.global.host_cpu_budget"}
        )
        assert described["invoke_with"] == "nebula.capabilities.read"
        value = await butler.invoke(
            {"operation": "nebula.global.host_cpu_budget", "arguments": {}},
            change=False,
        )
        assert value["cpu_limit"] is None and value["apply_state"] == "inactive"

    try:
        asyncio.run(scenario())
    finally:
        store.database.dispose()
