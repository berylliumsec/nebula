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


def _fake_secret_service(monkeypatch, *, locked, reachable=True):
    """Stand in for the host SecretService with a known lock state."""

    import secretstorage
    from types import SimpleNamespace

    def dbus_init():
        if not reachable:
            raise RuntimeError("no session bus")
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(secretstorage, "dbus_init", dbus_init)
    monkeypatch.setattr(
        secretstorage,
        "get_collection_by_alias",
        lambda *_: SimpleNamespace(is_locked=lambda: locked),
    )
    from keyring.backends.SecretService import Keyring

    class LinuxVault(Keyring):
        # keyring's own priority probes the session bus, which no test may
        # depend on; a fixed one keeps the backend check hermetic.
        __module__ = "keyring.backends.SecretService"
        priority = 5

    return CredentialStore(LinuxVault())


def test_locked_linux_vault_is_reported_and_not_offered(monkeypatch):
    store = _fake_secret_service(monkeypatch, locked=True)

    assert store.vault_state == "locked"
    assert store.vault_available is False
    with pytest.raises(CredentialUnavailableError, match="vault is locked"):
        store.create(CredentialCreateRequest(secret=SecretStr("secret")))
    with pytest.raises(CredentialUnavailableError, match="vault is locked"):
        store.delete("vault:" + "a" * 32)

    # Session storage stays open while the host vault is locked.
    session = store.create(
        CredentialCreateRequest(secret=SecretStr("secret"), persistence="session")
    )
    assert session.persistence == "session"
    assert store.resolve(session.reference).get_secret_value() == "secret"


def test_unlocked_linux_vault_is_available_and_unreachable_one_is_not(monkeypatch):
    assert _fake_secret_service(monkeypatch, locked=False).vault_state == "available"
    unreachable = _fake_secret_service(monkeypatch, locked=False, reachable=False)
    assert unreachable.vault_state == "unavailable"
    assert unreachable.vault_available is False


def test_vault_status_endpoint_reports_the_lock_state(tmp_path, monkeypatch):
    credential_store = _fake_secret_service(monkeypatch, locked=True)
    client = TestClient(
        create_app(
            NebulaStore(tmp_path / "vault-status.db"),
            auth_token="test-token",
            credential_store=credential_store,
        )
    )
    auth = {"Authorization": "Bearer test-token"}

    with client:
        status = client.get("/api/v1/credentials/vault", headers=auth)
        assert status.status_code == 200
        assert status.json() == {"state": "locked", "available": False}

        integration = client.get("/api/v1/integrations/typesafe", headers=auth)
        assert integration.status_code == 200
        assert integration.json()["vault_state"] == "locked"
        assert integration.json()["vault_available"] is False

        refused = client.post(
            "/api/v1/credentials",
            headers=auth,
            json={"secret": "never-persist-this", "persistence": "vault"},
        )
        assert refused.status_code == 503
        assert "locked" in refused.json()["detail"]
        assert "never-persist-this" not in refused.text


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
    monkeypatch.setattr(CredentialStore, "_backend_usable", lambda _: True)
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
    monkeypatch.setattr(CredentialStore, "vault_state", property(lambda _: "available"))
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


def test_core_imports_where_secretstorage_is_absent():
    """macOS and Windows have no secretstorage: Core must still import."""

    import subprocess
    import sys
    from pathlib import Path

    import nebula

    script = (
        "import importlib.abc, sys\n"
        "class Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'secretstorage':\n"
        '            raise ImportError(f"No module named {name!r}")\n'
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import nebula.v3.credentials\n"
        "import nebula.v3.vault_probe\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(nebula.__file__).resolve().parents[1]),
        },
    )
    assert completed.returncode == 0, completed.stderr


def test_missing_secretstorage_is_reported_instead_of_raising(monkeypatch):
    import sys

    from keyring.backends.SecretService import Keyring

    from nebula.v3.vault_probe import secret_service_state

    monkeypatch.setitem(sys.modules, "secretstorage", None)
    caught: list[Exception] = []
    assert secret_service_state(Keyring(), on_caught=caught.append) == "unavailable"
    assert isinstance(caught[0], ImportError)


class ChainedKeyring:
    """Stand in for keyring.backends.chainer.ChainerBackend."""

    __module__ = "keyring.backends.chainer"
    priority = 10

    def __init__(self, *backends):
        self.backends = list(backends)


def test_chained_keyring_backend_uses_its_first_trusted_member():
    from nebula.v3.vault_probe import vault_state

    plaintext = PlaintextKeyring()
    vault = MemoryKeyring()
    chain = ChainedKeyring(plaintext, vault)

    assert vault_state(chain) == "available"
    store = CredentialStore(chain)
    assert store.vault_state == "available"
    created = store.create(CredentialCreateRequest(secret=SecretStr("secret")))
    assert created.persistence == "vault"
    assert store.resolve(created.reference).get_secret_value() == "secret"
    assert vault.values and not plaintext.values

    assert CredentialStore(ChainedKeyring(plaintext)).vault_available is False


def test_credential_status_endpoints_run_off_the_event_loop(tmp_path):
    import asyncio
    from nebula.v3.domain import VpnProfile

    store = NebulaStore(tmp_path / "core.db")
    credential_store = CredentialStore(MemoryKeyring())
    session = credential_store.create(
        CredentialCreateRequest(secret=SecretStr("secret"), persistence="session")
    )
    store.create(
        VpnProfile(
            name="Fixture",
            filename="fixture.ovpn",
            remote_host="vpn.example.test",
            remote_port=1194,
            protocol="udp",
            fingerprint="a" * 64,
            secret_ref=session.reference,
        )
    )
    calls = []
    real_status = credential_store.status

    def observed_status(reference):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append("worker")
        else:
            calls.append("event-loop")
        return real_status(reference)

    credential_store.status = observed_status
    client = TestClient(
        create_app(store, auth_token="test-token", credential_store=credential_store)
    )
    auth = {"Authorization": "Bearer test-token"}
    config = (
        "client\ndev tun\nproto udp\nremote vpn.example.test 1194\n"
        "remote-cert-tls server\n<ca>\ncertificate\n</ca>\n"
    )

    with client:
        listed = client.get("/api/v1/vpn-profiles", headers=auth)
        assert listed.status_code == 200
        assert listed.json()[0]["available"] is True
        created = client.post(
            "/api/v1/vpn-profiles",
            headers=auth,
            json={
                "name": "Created",
                "filename": "created.ovpn",
                "config": config,
                "persistence": "session",
            },
        )
        assert created.status_code == 201
        assert created.json()["available"] is True
        status = client.get(
            f"/api/v1/credentials/{session.reference}/status", headers=auth
        )
        assert status.status_code == 200
        assert status.json()["available"] is True

    assert calls == ["worker"] * 3
