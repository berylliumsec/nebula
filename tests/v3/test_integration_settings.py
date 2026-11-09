"""Shared Settings projections use SQL plus supplied, read-only host observations."""

import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from nebula.v3.credentials import CredentialStatus
from nebula.v3.database import Database, EntityRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    ScopePolicy,
    ToolSuggestionSettings,
    ToolSuggestionTest,
    VpnProfile,
    WebSearchEngine,
    WebSearchRuntimeState,
    WebSearchSettings,
    WebSearchTest,
    utc_now,
)
from nebula.v3.integration_settings import (
    TypeSafeKeyRequest,
    VpnProfileCreateRequest,
    VpnProfileDeleteRequest,
    WebSearchEngineRequest,
    public_vpn_profiles,
    typesafe_integration_status,
    web_search_integration_status,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
REFERENCE = "vault:" + "a" * 32


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def store(location):
    database = Database(location, pool_settings=PoolSettings(5, 12, 5, 2))
    await database.initialize()
    database._bootstrap = False
    value = NebulaStore(database)
    try:
        yield value
    finally:
        await value.event_history.close()
        await database.dispose()


class Credentials:
    """Expose observation methods only; no secret or mutation operation exists."""

    def __init__(self, *, available=False, vault_state="locked"):
        self.available = available
        self.saved_vault_state = vault_state
        self.calls = []
        self.threads = []

    def status(self, reference):
        self.calls.append(reference)
        self.threads.append(threading.get_ident())
        return CredentialStatus(
            reference=reference,
            persistence=reference.split(":", 1)[0],
            available=self.available,
            state="available" if self.available else "locked",
        )

    @property
    def vault_state(self):
        self.threads.append(threading.get_ident())
        return self.saved_vault_state


async def seed_policies(store):
    for index, changes in enumerate(
        (
            {"tool_suggestions": True, "web_search": True},
            {"tool_suggestions": True, "web_search": True, "local_only": True},
            {"tool_suggestions": False, "web_search": False},
        )
    ):
        await store.create(
            ScopePolicy(
                id=f"policy-{index}", engagement_id=f"project-{index}", **changes
            )
        )


async def test_typesafe_reports_saved_key_precedence_and_redacted_test(
    store, monkeypatch
):
    test = ToolSuggestionTest(
        tested_at=utc_now(), ok=False, error="Synthetic rejection"
    )
    await store.create(
        ToolSuggestionSettings(id="typesafe", secret_ref=REFERENCE, last_test=test)
    )
    await seed_policies(store)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-environment-key")
    credentials = Credentials()
    result = await typesafe_integration_status(store, credentials)
    assert result.source == "vault" and not result.available
    assert result.vault_state == "locked" and not result.vault_available
    assert result.projects_using == 1 and result.last_test == test
    assert credentials.calls == [REFERENCE]
    assert all(value != threading.get_ident() for value in credentials.threads)
    assert REFERENCE not in result.model_dump_json()
    assert "synthetic-environment-key" not in result.model_dump_json()


async def test_typesafe_environment_and_empty_default_need_no_saved_secret(
    store, monkeypatch
):
    credentials = Credentials(vault_state="unavailable")
    monkeypatch.setenv("TYPESAFE_API_KEY", " synthetic-environment-key ")
    configured = await typesafe_integration_status(store, credentials)
    assert configured.source == "environment" and configured.available
    monkeypatch.delenv("TYPESAFE_API_KEY")
    empty = await typesafe_integration_status(store, credentials)
    assert empty.model_dump(mode="json") == {
        "source": None,
        "available": False,
        "vault_available": False,
        "vault_state": "unavailable",
        "last_test": None,
        "projects_using": 0,
    }
    assert credentials.calls == []
    assert await store.count(ToolSuggestionSettings) == 0


async def test_typesafe_policy_count_uses_legacy_defaults_without_catalog_hydration(
    store, monkeypatch
):
    await seed_policies(store)
    async with store.database.session() as session:
        row = await session.scalar(select(EntityRow).where(EntityRow.id == "policy-0"))
        row.payload = {
            key: value for key, value in row.payload.items() if key != "local_only"
        }
        await session.commit()

    async def forbidden_catalog(*args, **kwargs):
        pytest.fail("Integration count hydrated an unbounded policy catalog")

    monkeypatch.setattr(store, "list_entities", forbidden_catalog)
    result = await typesafe_integration_status(store, Credentials())
    assert result.projects_using == 1


async def test_web_search_uses_actual_host_observation_with_saved_metadata(store):
    await seed_policies(store)
    test = WebSearchTest(tested_at=utc_now(), ok=False, error="Synthetic search error")
    settings = await store.create(
        WebSearchSettings(
            id="web-search",
            container_state=WebSearchRuntimeState.READY,
            image_digest="sha256:" + "b" * 64,
            port=18181,
            engines=[WebSearchEngine.WIKIPEDIA],
            last_test=test,
            last_detail="Saved metadata",
        )
    )
    calls = []

    async def state():
        calls.append("state")
        return WebSearchRuntimeState.STOPPED, "Synthetic runtime is stopped"

    result = await web_search_integration_status(store, SimpleNamespace(state=state))
    assert (
        result.runtime_available
        and result.container_state == WebSearchRuntimeState.STOPPED
    )
    assert result.runtime_detail == "Synthetic runtime is stopped"
    assert result.image_digest == settings.image_digest and result.port == 18181
    assert result.engines == [WebSearchEngine.WIKIPEDIA]
    assert result.last_test == test and result.last_detail == "Saved metadata"
    assert result.projects_using == 1 and calls == ["state"]
    assert (
        await store.get(WebSearchSettings, settings.id)
    ).revision == settings.revision


async def test_web_search_distinguishes_missing_runtime_from_not_yet_installed(store):
    absent = await web_search_integration_status(store, None)
    assert absent.model_dump(mode="json") == {
        "runtime_available": False,
        "runtime_detail": "Core has no container runtime configured",
        "container_state": "absent",
        "image_digest": None,
        "port": None,
        "engines": [],
        "last_test": None,
        "last_detail": None,
        "projects_using": 0,
    }

    async def state():
        return WebSearchRuntimeState.ABSENT, "the search runtime is not created"

    not_installed = await web_search_integration_status(
        store, SimpleNamespace(state=state)
    )
    assert not_installed.runtime_available
    assert not_installed.container_state == WebSearchRuntimeState.ABSENT
    assert await store.count(WebSearchSettings) == 0


async def seed_vpn(store, identity):
    return await store.create(
        VpnProfile(
            id=identity,
            name="Synthetic VPN " + identity,
            filename=identity + ".ovpn",
            remote_host="vpn.example.invalid",
            remote_port=1194,
            protocol="udp",
            fingerprint="c" * 64,
            secret_ref=REFERENCE,
        )
    )


async def test_vpn_catalog_is_redacted_paged_and_observes_only_returned_profiles(store):
    for identity in ("first", "second", "third"):
        await seed_vpn(store, identity)
    credentials = Credentials(available=True)
    values = []
    for offset in range(3):
        page, next_offset = await public_vpn_profiles(
            store, credentials, offset=offset, limit=1
        )
        assert len(page) == 1 and page[0]["available"]
        assert "secret_ref" not in page[0] and REFERENCE not in str(page)
        assert next_offset == (offset + 1 if offset < 2 else None)
        values.extend(page)
    assert len({value["id"] for value in values}) == 3
    assert credentials.calls == [REFERENCE] * 3
    assert all(value != threading.get_ident() for value in credentials.threads)
    assert await public_vpn_profiles(store, credentials, offset=3) == ([], None)


async def test_vpn_catalog_rejects_oversized_or_invalid_pages_explicitly(
    store, monkeypatch
):
    await seed_vpn(store, "profile")
    credentials = Credentials()
    for options in ({"offset": -1}, {"limit": 0}, {"limit": 1001}):
        with pytest.raises(ValueError, match="bounds"):
            await public_vpn_profiles(store, credentials, **options)
    monkeypatch.setattr("nebula.v3.integration_settings.MAX_VPN_CATALOG_BYTES", 32)
    with pytest.raises(HTTPException) as failure:
        await public_vpn_profiles(store, credentials)
    assert failure.value.status_code == 413 and "smaller page" in failure.value.detail
    assert credentials.calls == []


def test_integration_request_schemas_keep_existing_validation():
    assert TypeSafeKeyRequest(secret="synthetic").persistence == "vault"
    assert WebSearchEngineRequest(engines=["wikipedia"]).engines == [
        WebSearchEngine.WIKIPEDIA
    ]
    assert (
        VpnProfileCreateRequest(
            name="VPN", filename="profile.ovpn", config="client"
        ).persistence
        == "vault"
    )
    for schema, value in (
        (TypeSafeKeyRequest, {"secret": "synthetic", "persistence": "plain"}),
        (WebSearchEngineRequest, {"engines": []}),
        (
            VpnProfileCreateRequest,
            {"name": "VPN", "filename": "../profile.ovpn", "config": "client"},
        ),
        (VpnProfileDeleteRequest, {"expected_revision": 0}),
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(value)
