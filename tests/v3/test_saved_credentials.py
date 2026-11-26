"""Saved passwords: encrypted at rest, entered through any tool, never shown to models."""

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
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credential_placeholders import (
    environment_name,
    find_placeholders,
    scrub,
    scrub_bytes,
    substitute,
    to_environment_references,
)
from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.domain import (
    McpApprovalMode,
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
from nebula.v3.policy import PolicyEngine
from nebula.v3.sandbox import AnalysisOnlyRunner
from nebula.v3.domain import ScopePolicy, ToolCall as StoredCall
from nebula.v3.tools import (
    AnalysisTool,
    GuidedToolError,
    IdempotencyBehavior,
    RiskClass,
    StoreToolEvidenceRecorder,
    StoreToolLedger,
    ToolBroker,
    ToolBrokerError,
    ToolInvocation,
    ToolRegistry,
    ToolSpec,
)

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


@pytest.fixture
def saved_root(tmp_path, monkeypatch):
    """Point this process's encrypted credential files at a temp data root."""

    import nebula.v3.credential_file_store as files

    monkeypatch.setattr(files, "_configured_root", tmp_path / "data" / "credentials")
    return CredentialStore(NoKeyring())


async def _save(store, credentials, name="mac-login", secret=SECRET):
    await SavedCredentialService(store, credentials).create(
        SavedCredentialCreate(name=name, secret=SecretStr(secret))
    )


def _broker(store, tmp_path, *plugins):
    registry = ToolRegistry()
    for plugin in plugins:
        registry.register(plugin)
    return ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=AnalysisOnlyRunner(),
        ledger=StoreToolLedger(store, enforce_run_budget=False),
        workspace_resolver=lambda _: tmp_path,
        evidence_recorder=StoreToolEvidenceRecorder(
            store, ArtifactStore(tmp_path / "artifacts")
        ),
    )


def _invocation(tmp_path, tool, arguments, call_id="call-1"):
    return ToolInvocation(
        id=call_id,
        engagement_id="project",
        run_id="run-1",
        tool_name=tool,
        arguments=arguments,
        workspace=tmp_path,
    )


def _echo_tool(received):
    async def run(arguments):
        received.append(arguments)
        return {"typed": arguments["text"], "note": f"echo {arguments['text']}"}

    return AnalysisTool(
        ToolSpec(
            name="echo.type",
            description="Type text.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.LOCAL_READ,
            idempotency=IdempotencyBehavior.SAFE,
        ),
        run,
    )


async def test_any_broker_tool_receives_value_while_records_keep_placeholder(
    tmp_path, saved_root
):
    store = NebulaStore(tmp_path / "saved.db")
    await _save(store, saved_root)
    received: list[dict] = []
    broker = _broker(store, tmp_path, _echo_tool(received))
    invocation = _invocation(
        tmp_path, "echo.type", {"text": "{{credential:mac-login}}"}
    )

    result = await broker.execute(invocation, ScopePolicy(engagement_id="project"))

    assert received == [{"text": SECRET}]
    assert invocation.arguments == {"text": "{{credential:mac-login}}"}
    assert result.output == {
        "typed": "{{credential:mac-login}}",
        "note": "echo {{credential:mac-login}}",
    }
    call = await store.get(StoredCall, "call-1")
    assert SECRET not in json.dumps(call.model_dump(mode="json"))
    assert "{{credential:mac-login}}" in json.dumps(call.arguments)

    # An unknown name refuses the call before the tool runs.
    with pytest.raises(GuidedToolError) as refused:
        await broker.execute(
            _invocation(tmp_path, "echo.type", {"text": "{{credential:nope}}"}, "c2"),
            ScopePolicy(engagement_id="project"),
        )
    assert refused.value.code == "saved_credential_refused"
    assert len(received) == 1


async def test_mcp_tools_get_the_value_through_the_broker(tmp_path, saved_root):
    store = NebulaStore(tmp_path / "saved.db")
    await _save(store, saved_root)
    profile = await store.create(
        McpServerProfile(
            name="mac-control",
            transport=McpTransport.STREAMABLE_HTTP,
            url="http://127.0.0.1:1/mcp",
            enabled=True,
            default_approval=McpApprovalMode.ALLOW,
            capabilities=McpCapabilitySnapshot(
                tools=[
                    McpToolSnapshot(
                        name="type_text",
                        open_world=False,
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
    service = McpProbeService(
        store, credential_store=saved_root, workspace_resolver=lambda _: tmp_path
    )
    sent: list[dict] = []

    async def echo(profile, *, engagement_id, tool_name, arguments):
        sent.append(arguments)
        if arguments["text"] == "fail":
            raise RuntimeError("boom")
        return {
            "content": [{"type": "text", "text": f"typed {arguments['text']}"}],
            "structuredContent": {"typed": arguments["text"]},
        }

    service.call_tool = echo
    [plugin] = await build_mcp_tool_plugins(service, (profile,))
    broker = _broker(store, tmp_path, plugin)

    result = await broker.execute(
        _invocation(tmp_path, plugin.spec.name, {"text": "{{credential:mac-login}}"}),
        ScopePolicy(engagement_id="project"),
    )

    assert sent == [{"text": SECRET}]
    rendered = json.dumps(result.model_result()) + json.dumps(result.mcp_content_blocks)
    assert SECRET not in rendered
    assert "{{credential:mac-login}}" in rendered


async def test_errors_never_carry_the_substituted_value(tmp_path, saved_root):
    store = NebulaStore(tmp_path / "saved.db")
    await _save(store, saved_root)

    async def leak(arguments):
        raise ToolBrokerError(f"{arguments['text']!r} does not match pattern")

    tool = AnalysisTool(_echo_tool([]).spec, leak)
    broker = _broker(store, tmp_path, tool)
    with pytest.raises(ToolBrokerError) as caught:
        await broker.execute(
            _invocation(tmp_path, "echo.type", {"text": "{{credential:mac-login}}"}),
            ScopePolicy(engagement_id="project"),
        )
    assert SECRET not in str(caught.value)
    assert "{{credential:mac-login}}" in str(caught.value)
    call = await store.get(StoredCall, "call-1")
    assert SECRET not in (call.error or "")


def test_commands_read_saved_passwords_from_environment_variables():
    assert environment_name("mac-login") == "NEBULA_CREDENTIAL_MAC_LOGIN"
    assert (
        to_environment_references(
            'printf "%s" "{{credential:mac-login}}" | sudo -S true'
        )
        == 'printf "%s" "${NEBULA_CREDENTIAL_MAC_LOGIN}" | sudo -S true'
    )
    assert (
        scrub_bytes(b"pw=" + SECRET.encode(), {"mac-login": SECRET})
        == b"pw={{credential:mac-login}}"
    )


async def test_list_tool_names_every_saved_password(tmp_path, saved_root):
    store = NebulaStore(tmp_path / "saved.db")
    for name in ("mac-login", "sudo-admin"):
        await _save(store, saved_root, name)

    tool = saved_credential_list_tool(store)
    assert tool.spec.name == "credentials.list"
    listed = await tool.execute(SimpleNamespace(arguments={}), None)

    assert [item["name"] for item in listed.output["credentials"]] == [
        "mac-login",
        "sudo-admin",
    ]
    assert listed.output["credentials"][0]["placeholder"] == (
        "{{credential:mac-login}}"
    )
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

        reference = (
            await SavedCredentialService(store, credentials).get("mac-login")
        ).reference
        updated = await client.patch(
            "/api/v1/saved-credentials/mac-login",
            headers=auth,
            json={"secret": "rotated", "description": "Rotated"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["description"] == "Rotated"
        assert credentials.status(reference).state == "missing"

        current = (
            await SavedCredentialService(store, credentials).get("mac-login")
        ).reference
        removed = await client.delete(
            "/api/v1/saved-credentials/mac-login", headers=auth
        )
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
