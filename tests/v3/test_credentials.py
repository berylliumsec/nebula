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
