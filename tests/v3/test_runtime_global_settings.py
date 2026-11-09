"""Global Settings share one SQL outcome and receipt without an execution owner."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.database import Database, ProjectOwnershipRow, RuntimeCommandRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    AutomationProjectPolicy,
    Engagement,
    OperatorProfile,
    ProviderProfile,
    RunnerProfile,
    utc_now,
)
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery
from nebula.v3.runtime_global_api import RuntimeGlobalApi
from nebula.v3.runtime_global_delivery import GlobalSqlDelivery
from nebula.v3.runtime_global_settings import (
    is_global_settings_host_route,
    mount_global_settings,
)
from nebula.v3.runtime_vault import RuntimeVault
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def settings(location, tmp_path, monkeypatch):
    database = Database(location, pool_settings=PoolSettings(5, 12, 5, 2))
    await database.initialize()
    database._bootstrap = False
    app = RuntimeGlobalApi(database)
    # Root integrates this mount before its existing read routes. It is safe to
    # keep the old read routes in this fixture; only one will match a request.
    mount_global_settings(app.app, app.store, app.actor)
    delivery = RuntimeDelivery(
        RuntimeCoordination(database), RuntimeVault(tmp_path / "payloads")
    )
    sql = GlobalSqlDelivery(delivery, app)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://synthetic.test"
    )

    async def forbidden_connection():
        pytest.fail("A global Settings route opened an independent SQL connection")

    monkeypatch.setattr(database, "connect", forbidden_connection)

    async def mutate(method, path, body=None, key=None, headers=None):
        raw = b"" if body is None else json.dumps(body).encode()
        intent = HttpIntent.capture(
            method,
            "/api/v1/" + path,
            b"",
            [(b"content-type", b"application/json")]
            + [
                (name.lower().encode(), str(value).encode())
                for name, value in (headers or {}).items()
            ],
            raw,
        )
        receipt = await sql.execute(
            intent,
            actor_id="device:settings-fixture",
            idempotency_key=key or path + ":" + method + ":" + str(mutate.calls),
        )
        mutate.calls += 1
        page = await delivery.page("device:settings-fixture", receipt.command_id)
        head = json.loads(page.frames[0].content)
        result = b"".join(
            frame.content for frame in page.frames if frame.kind == "body"
        )
        return SimpleNamespace(
            status=head["status"],
            data=json.loads(result) if result else None,
            receipt=receipt,
        )

    mutate.calls = 0
    try:
        yield SimpleNamespace(
            app=app,
            sql=sql,
            delivery=delivery,
            database=database,
            store=app.store,
            client=client,
            mutate=mutate,
        )
    finally:
        await client.aclose()
        await app.close()
        await database.dispose()


async def test_operator_lifecycle_and_revisions_share_the_receipt_transaction(settings):
    create = await settings.mutate(
        "POST", "operator-profiles", {"display_name": "  Jordan   Diaz "}
    )
    assert create.status == 201
    first = create.data
    assert first["display_name"] == "Jordan Diaz" and first["active"]
    second = (
        await settings.mutate("POST", "operator-profiles", {"display_name": "Alex"})
    ).data
    assert not second["active"]
    changed = await settings.mutate(
        "PATCH",
        f"operator-profiles/{second['id']}",
        {"role": "Reviewer", "expected_revision": 1},
    )
    assert changed.status == 200 and changed.data["revision"] == 2
    stale = await settings.mutate(
        "POST",
        f"operator-profiles/{second['id']}/activate",
        {"expected_revision": 1},
    )
    assert stale.status == 409
    activated = await settings.mutate(
        "POST",
        f"operator-profiles/{second['id']}/activate",
        {"expected_revision": 2},
    )
    assert activated.status == 200 and activated.data["active"]
    listed = (await settings.client.get("/api/v1/operator-profiles")).json()
    assert len([item for item in listed if item["active"]]) == 1
    assert listed[0]["id"] == second["id"]
    blocked = await settings.mutate(
        "DELETE", f"operator-profiles/{second['id']}", headers={"If-Match": 3}
    )
    assert blocked.status == 409
    deleted = await settings.mutate(
        "DELETE", f"operator-profiles/{first['id']}", headers={"If-Match": 2}
    )
    assert deleted.status == 204
    assert await settings.store.count(OperatorProfile) == 1
    async with settings.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
        row = await session.get(RuntimeCommandRow, activated.receipt.command_id)
        assert row.actor_id == "device:settings-fixture" and row.state == "succeeded"


async def test_operator_invalid_fields_and_attribution_never_partially_commit(settings):
    invalid = await settings.mutate(
        "POST", "operator-profiles", {"display_name": "Injected", "active": True}
    )
    assert invalid.status == 422 and await settings.store.count(OperatorProfile) == 0
    first = (
        await settings.mutate("POST", "operator-profiles", {"display_name": "Owner"})
    ).data
    second = (
        await settings.mutate("POST", "operator-profiles", {"display_name": "Next"})
    ).data
    await settings.store.create(Engagement(name="Attributed", owner_id=first["id"]))
    await settings.mutate(
        "POST", f"operator-profiles/{second['id']}/activate", {"expected_revision": 1}
    )
    blocked = await settings.mutate("DELETE", f"operator-profiles/{first['id']}")
    assert blocked.status == 409 and "attribution" in blocked.data["detail"]
    rejected = await settings.mutate(
        "PATCH",
        f"operator-profiles/{second['id']}",
        {"metadata": None, "expected_revision": 2},
    )
    assert rejected.status == 422
    assert (await settings.store.get(OperatorProfile, second["id"])).revision == 2


async def test_concurrent_create_and_retry_preserve_one_profile_per_request(settings):
    async def create(key):
        return await settings.mutate(
            "POST", "operator-profiles", {"display_name": key}, key=key
        )

    first, duplicate, second = await asyncio.gather(
        create("first"), create("first"), create("second")
    )
    assert first.data == duplicate.data
    assert first.receipt.command_id == duplicate.receipt.command_id
    assert second.data["id"] != first.data["id"]
    profiles = (await settings.client.get("/api/v1/operator-profiles")).json()
    assert len(profiles) == 2 and sum(profile["active"] for profile in profiles) == 1


async def test_failure_before_receipt_rolls_back_operator_creation_and_activation(
    settings, monkeypatch
):
    first = (
        await settings.mutate("POST", "operator-profiles", {"display_name": "First"})
    ).data
    second = (
        await settings.mutate("POST", "operator-profiles", {"display_name": "Second"})
    ).data
    original = settings.sql._response

    async def fail_after_effect(intent, actor):
        response = await original(intent, actor)
        assert response[0] < 400
        raise RuntimeError("Interrupted before recording the response")

    monkeypatch.setattr(settings.sql, "_response", fail_after_effect)
    with pytest.raises(RuntimeError, match="Interrupted before"):
        await settings.mutate(
            "POST", "operator-profiles", {"display_name": "Uncommitted"}
        )
    with pytest.raises(RuntimeError, match="Interrupted before"):
        await settings.mutate(
            "POST",
            f"operator-profiles/{second['id']}/activate",
            {"expected_revision": 1},
        )
    assert await settings.store.count(OperatorProfile) == 2
    assert (await settings.store.get(OperatorProfile, first["id"])).active
    assert (await settings.store.get(OperatorProfile, first["id"])).revision == 1
    assert not (await settings.store.get(OperatorProfile, second["id"])).active
    async with settings.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 2
        )


def runner_request(**changes):
    return {
        "name": "Synthetic runner",
        "runtime": "podman",
        "executable": "/inert/podman",
        "platform": "linux/amd64",
        "isolation": "rootless",
        **changes,
    }


async def test_runner_crud_preserves_validation_and_does_not_claim_verification(
    settings,
):
    invalid = await settings.mutate(
        "PUT", "runner-profiles/local", runner_request(healthy=True)
    )
    assert invalid.status == 422
    invalid = await settings.mutate(
        "PUT", "runner-profiles/local", runner_request(executable="podman")
    )
    assert invalid.status == 422
    created = await settings.mutate(
        "PUT", "runner-profiles/local", runner_request(), key="runner"
    )
    assert created.status == 200 and not created.data["healthy"]
    assert created.data["last_health_at"] is None
    assert "verification" in created.data["last_health_detail"]
    duplicate = await settings.mutate(
        "PUT", "runner-profiles/local", runner_request(), key="runner"
    )
    assert duplicate.data == created.data
    observed = await settings.store.update(
        RunnerProfile,
        "local",
        {
            "healthy": True,
            "last_health_at": utc_now(),
            "last_health_detail": "Earlier host observation",
        },
        expected_revision=1,
    )
    rename = await settings.mutate(
        "PUT",
        "runner-profiles/local",
        runner_request(name="Renamed", expected_revision=observed.revision),
    )
    assert (
        rename.data["healthy"]
        and rename.data["last_health_detail"] == "Earlier host observation"
    )
    changed = await settings.mutate(
        "PUT",
        "runner-profiles/local",
        runner_request(context="different", expected_revision=rename.data["revision"]),
    )
    assert changed.status == 200 and not changed.data["healthy"]
    assert changed.data["last_health_at"] is None
    stale = await settings.mutate(
        "PUT", "runner-profiles/local", runner_request(expected_revision=1)
    )
    assert stale.status == 409
    assert (await settings.client.get("/api/v1/runner-profiles/local")).json()[
        "context"
    ] == "different"
    removed = await settings.mutate(
        "DELETE",
        "runner-profiles/local",
        headers={"If-Match": changed.data["revision"]},
    )
    assert removed.status == 204 and await settings.store.count(RunnerProfile) == 0


async def test_runner_delete_preserves_references_and_new_profile_creation(settings):
    created = await settings.mutate("POST", "runner-profiles", runner_request())
    assert created.status == 201
    identity = created.data["id"]
    await settings.store.create(
        AutomationProjectPolicy(engagement_id="project", runner_profile_id=identity)
    )
    deleted = await settings.mutate(
        "DELETE", f"runner-profiles/{identity}", headers={"If-Match": 1}
    )
    assert deleted.status == 409
    assert await settings.store.count(RunnerProfile) == 1
    assert not settings.app.supports(
        {
            "type": "http",
            "path": f"/api/v1/runner-profiles/{identity}/probe",
            "method": "POST",
        }
    )


async def test_stored_model_metadata_is_bounded_and_requires_no_credentials(settings):
    profile = await settings.store.create(
        ProviderProfile(
            name="Metadata only",
            provider_type="openai",
            secret_ref="env:ABSENT_FIXTURE_SECRET",
            model_allowlist=["allowed"],
            metadata={
                "model_descriptors": [
                    {"id": f"model-{index:04}", "name": f"Model {index}"}
                    for index in range(1010)
                ]
            },
        )
    )
    first = await settings.client.get(f"/api/v1/providers/{profile.id}/models?limit=2")
    assert first.status_code == 200
    data = first.json()
    assert data["source"] == "stored_profile" and not data["live_probe"]
    assert data["partial"] and data["next_offset"] == 2
    assert len(data["items"]) == 2
    assert not (
        await settings.store.get(ProviderProfile, profile.id)
    ).capabilities.tool_calling
    second = (
        await settings.client.get(
            f"/api/v1/providers/{profile.id}/models?limit=2&offset=2"
        )
    ).json()
    assert second["items"][0]["id"] != data["items"][0]["id"]


async def test_external_route_classifier_never_selects_sql_metadata_or_project_work():
    for method, path in [
        ("POST", "/api/v1/providers/provider/health"),
        ("GET", "/api/v1/providers/discover-local"),
        ("POST", "/api/v1/runner-profiles/local/probe"),
        ("POST", "/api/v1/harnesses/harness/health"),
    ]:
        assert is_global_settings_host_route(method, path)
    for method, path in [
        ("PUT", "/api/v1/runner-profiles/local"),
        ("GET", "/api/v1/providers/provider/models"),
        ("POST", "/api/v1/engagements/project/chat/completions"),
        ("POST", "/api/v1/providers/provider/health/extra"),
    ]:
        assert not is_global_settings_host_route(method, path)
