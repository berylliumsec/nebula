"""Saved passwords: encrypted at rest, entered into MCP tools, never shown to models."""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
import os
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.credential_file_store import (
    CredentialFileError,
    EncryptedCredentialFiles,
)
from nebula.v3.credential_placeholders import find_placeholders, scrub, substitute
from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.domain import (
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    utc_now,
)
from nebula.v3.mcp import McpProbeService, build_mcp_tool_plugins
from nebula.v3.saved_credentials import (
    SavedCredentialCreate,
    SavedCredentialService,
    saved_credential_list_tool,
)
from nebula.v3.settings_assistant_api import _browser_owned_destination
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import GuidedToolError

SECRET = "correct horse battery staple"


class NoKeyring:
    __module__ = "keyring.backends.fail"
    priority = 0

    def get_password(self, *_args):
        return None

    def set_password(self, *_args):
        raise RuntimeError("no keyring")

    def delete_password(self, *_args):
        return None


def _credentials(tmp_path) -> CredentialStore:
    return CredentialStore(NoKeyring(), file_root=tmp_path / "credentials")


def test_file_store_round_trips_private_ciphertext(tmp_path):
    files = EncryptedCredentialFiles(tmp_path / "credentials")
    identifier = files.write(SECRET)

    assert files.read(identifier) == SECRET
    for path in (files.root / "key", files.root / identifier):
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(files.root).st_mode & 0o777 == 0o700
    assert SECRET.encode() not in (files.root / identifier).read_bytes()

    files.delete(identifier)
    assert files.read(identifier) is None


def test_file_store_rejects_tampering_and_unsafe_directories(tmp_path):
    files = EncryptedCredentialFiles(tmp_path / "credentials")
    identifier = files.write(SECRET)
    path = files.root / identifier
    sealed = bytearray(path.read_bytes())
    sealed[-1] ^= 1
    path.chmod(0o600)
    path.unlink()
    path.write_bytes(bytes(sealed))
    path.chmod(0o600)
    with pytest.raises(CredentialFileError):
        files.read(identifier)

    # A ciphertext moved to another identifier fails its bound name.
    other = files.write("other")
    (files.root / other).unlink()
    os.link(files.root / files.write(SECRET), files.root / other)
    with pytest.raises(CredentialFileError):
        files.read(other)

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(CredentialFileError):
        EncryptedCredentialFiles(shared)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "credentials")
    with pytest.raises(CredentialFileError):
        EncryptedCredentialFiles(link)


def test_store_references_work_without_an_os_vault(tmp_path):
    credentials = _credentials(tmp_path)
    assert credentials.vault_state != "available"

    status = credentials.create(
        CredentialCreateRequest(secret=SecretStr(SECRET), persistence="store")
    )
    assert status.reference.startswith("store:")
    assert status.persistence == "store"
    assert credentials.status(status.reference).available
    assert credentials.resolve(status.reference).get_secret_value() == SECRET

    credentials.delete(status.reference)
    assert credentials.status(status.reference).state == "missing"


def test_placeholders_substitute_and_scrub_nested_values():
    arguments = {
        "text": "{{credential:mac-login}}",
        "steps": [{"type": "{{ credential:mac-login }}\n"}, "plain"],
        "note": "{{credential:Not-Valid}}",
    }
    assert find_placeholders(arguments) == {"mac-login"}

    secrets = {"mac-login": SECRET}
    sent = substitute(arguments, secrets)
    assert sent["text"] == SECRET
    assert sent["steps"][0]["type"] == SECRET + "\n"
    assert sent["note"] == "{{credential:Not-Valid}}"
    assert arguments["text"] == "{{credential:mac-login}}"

    echoed = {"content": [{"type": "text", "text": f"typed {SECRET}!"}]}
    assert scrub(echoed, secrets) == {
        "content": [{"type": "text", "text": "typed {{credential:mac-login}}!"}]
    }


async def _mac_profile(store, name="mac-control") -> McpServerProfile:
    return await store.create(
        McpServerProfile(
            name=name,
            transport=McpTransport.STREAMABLE_HTTP,
            url="http://127.0.0.1:1/mcp",
            enabled=True,
            capabilities=McpCapabilitySnapshot(
                tools=[
                    McpToolSnapshot(
                        name="type_text",
                        input_schema={
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    )
                ],
                checked_at=utc_now(),
            ),
        )
    )


async def test_mcp_call_receives_value_while_model_sees_only_placeholder(tmp_path):
    store = NebulaStore(tmp_path / "saved.db")
    credentials = _credentials(tmp_path)
    mac = await _mac_profile(store)
    web = await _mac_profile(store, "web-fetch")
    saved = SavedCredentialService(store, credentials)
    await saved.create(
        SavedCredentialCreate(
            name="mac-login",
            description="Mac login password",
            secret=SecretStr(SECRET),
            allowed_mcp_server_ids=[mac.id],
        )
    )
    service = McpProbeService(
        store, credential_store=credentials, workspace_resolver=lambda _: tmp_path
    )
    sent: list[dict] = []

    async def echo(profile, *, engagement_id, tool_name, arguments):
        sent.append(arguments)
        return {
            "content": [{"type": "text", "text": f"typed {arguments['text']}"}],
            "structuredContent": {"typed": arguments["text"]},
        }

    service.call_tool = echo
    [mac_tool] = await build_mcp_tool_plugins(service, (mac,))
    [web_tool] = await build_mcp_tool_plugins(service, (web,))
    invocation = SimpleNamespace(
        engagement_id="project", arguments={"text": "{{credential:mac-login}}"}
    )

    result = await mac_tool.execute(invocation, None)

    assert sent == [{"text": SECRET}]
    assert invocation.arguments == {"text": "{{credential:mac-login}}"}
    assert result.exit_code == 0
    rendered = json.dumps(result.mcp_content_blocks)
    assert SECRET not in rendered
    assert "{{credential:mac-login}}" in rendered

    # A server the operator did not allow never receives the value.
    refused = await web_tool.execute(invocation, None)
    assert len(sent) == 1
    assert isinstance(refused.failure_error, GuidedToolError)
    assert refused.failure_error.code == "saved_credential_refused"

    unknown = SimpleNamespace(
        engagement_id="project", arguments={"text": "{{credential:nope}}"}
    )
    missing = await mac_tool.execute(unknown, None)
    assert missing.failure_error.code == "saved_credential_refused"
    assert len(sent) == 1


async def test_upstream_errors_never_carry_the_substituted_value(tmp_path):
    store = NebulaStore(tmp_path / "saved.db")
    credentials = _credentials(tmp_path)
    mac = await _mac_profile(store)
    await SavedCredentialService(store, credentials).create(
        SavedCredentialCreate(
            name="mac-login", secret=SecretStr(SECRET), allowed_mcp_server_ids=[mac.id]
        )
    )
    service = McpProbeService(
        store, credential_store=credentials, workspace_resolver=lambda _: tmp_path
    )

    async def fail(profile, *, engagement_id, tool_name, arguments):
        raise RuntimeError(f"{arguments['text']!r} does not match pattern")

    service.call_tool = fail
    [tool] = await build_mcp_tool_plugins(service, (mac,))
    result = await tool.execute(
        SimpleNamespace(
            engagement_id="project", arguments={"text": "{{credential:mac-login}}"}
        ),
        None,
    )
    assert result.exit_code == 1
    assert SECRET not in str(result.failure_error)
    assert "{{credential:mac-login}}" in str(result.failure_error)


async def test_list_tool_names_only_passwords_for_selected_servers(tmp_path):
    store = NebulaStore(tmp_path / "saved.db")
    credentials = _credentials(tmp_path)
    mac = await _mac_profile(store)
    other = await _mac_profile(store, "other")
    saved = SavedCredentialService(store, credentials)
    for name, server in (("mac-login", mac.id), ("other-login", other.id)):
        await saved.create(
            SavedCredentialCreate(
                name=name, secret=SecretStr(SECRET), allowed_mcp_server_ids=[server]
            )
        )

    assert await saved_credential_list_tool(store, ()) is None
    tool = await saved_credential_list_tool(store, (mac,))
    assert tool is not None and tool.spec.name == "credentials.list"
    listed = await tool.execute(SimpleNamespace(arguments={}), None)

    assert listed.output == {
        "credentials": [
            {
                "name": "mac-login",
                "placeholder": "{{credential:mac-login}}",
                "description": "",
                "allowed_servers": ["mac-control"],
            }
        ]
    }
    assert SECRET not in json.dumps(listed.output)


async def test_saved_credential_api_never_returns_or_persists_values(tmp_path):
    database_path = tmp_path / "saved.db"
    store = NebulaStore(database_path)
    credentials = _credentials(tmp_path)
    client = TestClient(
        create_app(store, auth_token="test-token", credential_store=credentials)
    )
    auth = {"Authorization": "Bearer test-token"}

    async with client:
        created = await client.post(
            "/api/v1/saved-credentials",
            headers=auth,
            json={
                "name": "mac-login",
                "description": "Mac login",
                "secret": SECRET,
                "allowed_mcp_server_ids": ["server-a"],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["placeholder"] == "{{credential:mac-login}}"
        assert created.json()["available"] is True
        duplicate = await client.post(
            "/api/v1/saved-credentials",
            headers=auth,
            json={"name": "mac-login", "secret": "x"},
        )
        assert duplicate.status_code == 409

        listed = await client.get("/api/v1/saved-credentials", headers=auth)
        assert [item["name"] for item in listed.json()] == ["mac-login"]

        reference = (await SavedCredentialService(store, credentials).get("mac-login")).reference
        updated = await client.patch(
            "/api/v1/saved-credentials/mac-login",
            headers=auth,
            json={"secret": "rotated", "allowed_mcp_server_ids": ["server-b"]},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["allowed_mcp_server_ids"] == ["server-b"]
        assert credentials.status(reference).state == "missing"

        current = (await SavedCredentialService(store, credentials).get("mac-login")).reference
        removed = await client.delete("/api/v1/saved-credentials/mac-login", headers=auth)
        assert removed.status_code == 204
        assert credentials.status(current).state == "missing"
        gone = await client.delete("/api/v1/saved-credentials/mac-login", headers=auth)
        assert gone.status_code == 404

        for body in (created.text, listed.text, updated.text):
            assert SECRET not in body and "rotated" not in body

    assert SECRET.encode() not in database_path.read_bytes()


def test_butler_cannot_write_saved_passwords_through_the_settings_api():
    for method, path in (
        ("POST", "/api/v1/saved-credentials"),
        ("PATCH", "/api/v1/saved-credentials/mac-login"),
        ("DELETE", "/api/v1/saved-credentials/mac-login"),
    ):
        assert _browser_owned_destination(method, path) == "saved-credentials"
    assert _browser_owned_destination("GET", "/api/v1/saved-credentials") is None
