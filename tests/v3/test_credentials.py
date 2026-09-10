import os

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import nebula.v3.credentials as credentials_module
from nebula.v3.api import create_app
from nebula.v3.credentials import (
    CredentialCreateRequest,
    CredentialError,
    CredentialStore,
    CredentialUnavailableError,
)
from nebula.v3.domain import ProviderProfile
from nebula.v3.providers import ProviderError, provider_from_profile
from nebula.v3.storage import NebulaStore


class MemoryKeyring:
    __module__ = "keyring.backends.SecretService"
    priority = 1

    def __init__(self):
        self.values = {}

    def get_keyring(self):
        return self

    def set_password(self, service_name, username, password):
        self.values[(service_name, username)] = password

    def get_password(self, service_name, username):
        return self.values.get((service_name, username))

    def delete_password(self, service_name, username):
        self.values.pop((service_name, username), None)


class PlaintextKeyring(MemoryKeyring):
    __module__ = "keyrings.alt.file"


def test_vault_and_session_credentials_are_write_only_references():
    backend = MemoryKeyring()
    store = CredentialStore(backend)
    vault = store.create(
        CredentialCreateRequest(secret=SecretStr("vault-secret"), persistence="vault")
    )
    session = store.create(
        CredentialCreateRequest(
            secret=SecretStr("session-secret"), persistence="session"
        )
    )

    assert vault.reference.startswith("vault:")
    assert session.reference.startswith("session:")
    assert "vault-secret" not in vault.model_dump_json()
    assert store.resolve(vault.reference).get_secret_value() == "vault-secret"
    assert store.resolve(session.reference).get_secret_value() == "session-secret"

    store.delete(vault.reference)
    store.delete(session.reference)
    assert store.status(vault.reference).available is False
    assert store.status(session.reference).available is False


def test_unavailable_vault_fails_closed_and_environment_is_external(monkeypatch):
    store = CredentialStore(None)
    store.keyring_backend = None
    with pytest.raises(CredentialUnavailableError):
        store.create(CredentialCreateRequest(secret=SecretStr("secret")))

    monkeypatch.setenv("NEBULA_TEST_KEY", "environment-secret")
    assert (
        store.resolve("env:NEBULA_TEST_KEY").get_secret_value() == "environment-secret"
    )
    with pytest.raises(CredentialError, match="outside Nebula"):
        store.delete("env:NEBULA_TEST_KEY")
    assert os.environ["NEBULA_TEST_KEY"] == "environment-secret"


def test_system_vault_backend_load_failure_fails_closed(monkeypatch):
    def unavailable_backend():
        raise RuntimeError("OS vault service is unavailable")

    monkeypatch.setattr(credentials_module.keyring, "get_keyring", unavailable_backend)
    store = CredentialStore()

    assert store.keyring_backend is None
    assert store.vault_available is False
    with pytest.raises(CredentialUnavailableError, match="vault is unavailable"):
        store.create(CredentialCreateRequest(secret=SecretStr("secret")))


def test_plaintext_keyring_backend_is_never_treated_as_a_vault():
    store = CredentialStore(PlaintextKeyring())

    assert store.vault_available is False
    with pytest.raises(CredentialUnavailableError, match="vault is unavailable"):
        store.create(CredentialCreateRequest(secret=SecretStr("secret")))
    with pytest.raises(CredentialUnavailableError, match="vault is unavailable"):
        store.delete("vault:" + "a" * 32)


def test_provider_vault_reference_requires_and_uses_resolver():
    profile = ProviderProfile(
        name="Cloud",
        provider_type="openai",
        secret_ref="vault:" + "a" * 32,
        model_allowlist=["gpt-test"],
    )
    unresolved = provider_from_profile(profile)
    with pytest.raises(ProviderError, match="unavailable"):
        unresolved.config.resolve_api_key()

    resolved = provider_from_profile(profile, lambda _ref: SecretStr("resolved-secret"))
    assert resolved.config.resolve_api_key().get_secret_value() == "resolved-secret"


def test_credential_api_is_write_only_and_persists_only_opaque_reference(tmp_path):
    database_path = tmp_path / "credentials.db"
    store = NebulaStore(database_path)
    credential_store = CredentialStore(MemoryKeyring())
    client = TestClient(
        create_app(
            store,
            auth_token="test-token",
            credential_store=credential_store,
        )
    )
    auth = {"Authorization": "Bearer test-token"}

    with client:
        created = client.post(
            "/api/v1/credentials",
            headers=auth,
            json={"secret": "never-persist-this", "persistence": "vault"},
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["reference"].startswith("vault:")
        assert "never-persist-this" not in created.text

        profile = client.post(
            "/api/v1/providers",
            headers=auth,
            json={
                "name": "Write-only cloud profile",
                "provider_type": "openai",
                "secret_ref": payload["reference"],
                "model_allowlist": ["gpt-test"],
            },
        )
        assert profile.status_code == 201
        assert profile.json()["secret_ref"] == payload["reference"]
        assert "never-persist-this" not in profile.text

        status = client.get(
            f"/api/v1/credentials/{payload['reference']}/status", headers=auth
        )
        assert status.status_code == 200
        assert status.json()["available"] is True

        removed = client.delete(
            f"/api/v1/credentials/{payload['reference']}", headers=auth
        )
        assert removed.status_code == 204

    assert b"never-persist-this" not in database_path.read_bytes()


@pytest.mark.parametrize(
    "collection_locked,item_locked,expected",
    [
        (True, False, None),
        (False, True, None),
        (False, False, "test-secret"),
    ],
)
def test_linux_vault_reads_never_prompt(
    monkeypatch, collection_locked, item_locked, expected
):
    import secretstorage
    from keyring.backends.SecretService import Keyring

    class Connection:
        closed = False

        def close(self):
            self.closed = True

    class Item:
        def is_locked(self):
            return item_locked

        def unlock(self):
            pytest.fail("must not prompt to unlock an item")

        def get_secret(self):
            assert not item_locked
            return b"test-secret"

    class Collection:
        def is_locked(self):
            return collection_locked

        def unlock(self):
            pytest.fail("must not prompt to unlock a collection")

        def search_items(self, query):
            assert not collection_locked
            return [Item()]

    connection = Connection()
    monkeypatch.setattr(secretstorage, "dbus_init", lambda: connection)
    monkeypatch.setattr(
        secretstorage, "get_collection_by_alias", lambda *_: Collection()
    )
    monkeypatch.setattr(
        Keyring, "get_password", lambda *_: pytest.fail("interactive read used")
    )
    monkeypatch.setattr(CredentialStore, "vault_available", property(lambda _: True))
    store = CredentialStore(Keyring())
    assert store._vault_value("vault:" + "a" * 32) == expected
    assert connection.closed


@pytest.mark.parametrize("operation", ["write", "delete"])
@pytest.mark.parametrize("state", ["locked", "prompt", "ready"])
def test_linux_vault_mutations_never_prompt(monkeypatch, operation, state):
    from types import SimpleNamespace
    import secretstorage.collection
    from keyring.backends.SecretService import Keyring

    calls = []
    connection = SimpleNamespace(close=lambda: calls.append("close"))

    def call(method, *args):
        calls.append(method)
        if method == "CreateItem":
            return ("/item" if state == "ready" else "/", "/prompt")
        return ("/" if state == "ready" else "/prompt",)

    item = SimpleNamespace(
        ensure_not_locked=lambda: None, _item=SimpleNamespace(call=call)
    )
    collection = SimpleNamespace(
        is_locked=lambda: state == "locked",
        session=object(),
        search_items=lambda _: [item],
        _collection=SimpleNamespace(call=call),
    )
    monkeypatch.setattr(secretstorage, "dbus_init", lambda: connection)
    monkeypatch.setattr(secretstorage, "get_collection_by_alias", lambda *_: collection)
    monkeypatch.setattr(
        secretstorage.collection, "format_secret", lambda *_: "encoded-secret"
    )
    monkeypatch.setattr(
        Keyring, "set_password", lambda *_: pytest.fail("interactive write")
    )
    monkeypatch.setattr(
        Keyring, "delete_password", lambda *_: pytest.fail("interactive delete")
    )
    monkeypatch.setattr(CredentialStore, "vault_available", property(lambda _: True))
    store = CredentialStore(Keyring())

    def mutate():
        if operation == "write":
            return store.create(CredentialCreateRequest(secret=SecretStr("fixture")))
        return store.delete("vault:" + "a" * 32)

    if state == "ready":
        mutate()
    else:
        with pytest.raises(CredentialUnavailableError, match="host"):
            mutate()
    assert calls[-1] == "close"
    assert (len(calls) == 1) == (state == "locked")


def test_failed_vpn_vault_delete_keeps_profile_and_core_responsive(tmp_path):
    import asyncio
    import threading
    import httpx
    from nebula.v3.domain import VpnProfile

    store = NebulaStore(tmp_path / "core.db")
    credentials = CredentialStore(MemoryKeyring())
    profile = store.create(
        VpnProfile(
            name="Fixture",
            filename="fixture.ovpn",
            remote_host="vpn.example.test",
            remote_port=1194,
            protocol="udp",
            fingerprint="a" * 64,
            secret_ref="vault:" + "a" * 32,
        )
    )
    entered, release = threading.Event(), threading.Event()

    def blocked_delete(_):
        entered.set()
        release.wait(3)
        raise CredentialUnavailableError(
            "The host credential vault is locked. Unlock and retry."
        )

    credentials.delete = blocked_delete
    app = create_app(store, credential_store=credentials, auth_token="fixture")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": "Bearer fixture"},
        ) as client:
            path = f"/api/v1/vpn-profiles/{profile.id}"
            stale = await client.request(
                "DELETE", path, json={"expected_revision": profile.revision + 1}
            )
            assert stale.status_code == 409
            assert not entered.is_set()
            deletion = asyncio.create_task(
                client.request(
                    "DELETE", path, json={"expected_revision": profile.revision}
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                response = await asyncio.wait_for(client.get("/api/v1/engagements"), 1)
                assert response.status_code == 200
                assert not deletion.done()
            finally:
                release.set()
            response = await deletion
            assert response.status_code == 409
            assert "not removed" in response.json()["detail"]
            assert store.get(VpnProfile, profile.id).secret_ref == profile.secret_ref
            credentials.delete = lambda _: None
            retry = await client.request(
                "DELETE", path, json={"expected_revision": profile.revision}
            )
            assert retry.status_code == 204

    asyncio.run(scenario())
