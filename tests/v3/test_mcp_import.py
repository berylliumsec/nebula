import copy
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import nebula.v3.cli as cli_module
from nebula.v3.api import create_app
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    McpApprovalMode,
    McpAuthMode,
    McpCapabilitySnapshot,
    McpCwdPolicy,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
)
from nebula.v3.mcp_import import (
    McpImportDefaults,
    McpImportRequest,
    export_mcp_config,
    import_mcp_config,
)
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


class UntrustedKeyring(MemoryKeyring):
    __module__ = "keyrings.alt.file"


def _which(name):
    return {"npx": "/usr/bin/npx", "uvx": "/opt/uv/bin/uvx"}.get(name)


CLAUDE_CONFIG = {
    "mcpServers": {
        "burp": {
            "command": "npx",
            "args": ["-y", "burp-mcp", "--port", 1337],
            "env": {
                "BURP_API_KEY": "${BURP_API_KEY}",
                "LOG_LEVEL": "debug",
                "SHODAN_TOKEN": "literal-shodan-secret",
            },
            "alwaysAllow": ["scan"],
        },
        "remote": {
            "type": "http",
            "url": "https://mcp.example.test/mcp",
            "headers": {
                "Authorization": "Bearer ${REMOTE_TOKEN}",
                "X-Tenant-Key": "literal-tenant-secret",
            },
            "nebula": {
                "default_approval": "ask",
                "disabled_tools": ["delete_all"],
                "tool_timeout_seconds": 120,
            },
        },
    }
}


def _import(store, credentials, config=CLAUDE_CONFIG, **options):
    return import_mcp_config(
        McpImportRequest(config=config, **options),
        store=store,
        credential_store=credentials,
        which=_which,
    )


def test_preview_describes_servers_without_saving_profiles_or_secrets(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    keyring = MemoryKeyring()

    report = _import(store, CredentialStore(keyring))

    assert report.dry_run is True
    assert (report.created, report.invalid) == (2, 0)
    burp, remote = report.entries
    assert burp.command == "/usr/bin/npx"
    assert burp.arguments == ["-y", "burp-mcp", "--port", "1337"]
    assert any("approvals were not imported" in item for item in burp.warnings)
    assert {(item.target, item.source) for item in burp.secrets} == {
        ("env BURP_API_KEY", "environment"),
        ("env SHODAN_TOKEN", "vault"),
    }
    assert remote.transport == McpTransport.STREAMABLE_HTTP
    assert store.list_entities(McpServerProfile) == []
    assert keyring.values == {}
    assert "literal" not in report.model_dump_json()


def test_apply_saves_disabled_profiles_with_references_only(tmp_path):
    database = tmp_path / "nebula.db"
    store = NebulaStore(database)
    keyring = MemoryKeyring()
    credentials = CredentialStore(keyring)

    report = _import(store, credentials, dry_run=False, source_name="claude.json")

    assert (report.created, report.invalid) == (2, 0)
    profiles = {item.name: item for item in store.list_entities(McpServerProfile)}
    burp = profiles["burp"]
    assert burp.enabled is False and burp.trusted_stdio is False
    assert burp.command == "/usr/bin/npx"
    assert burp.cwd_policy == McpCwdPolicy.WORKSPACE
    assert burp.environment == {"LOG_LEVEL": "debug"}
    assert burp.environment_secret_refs["BURP_API_KEY"] == "env:BURP_API_KEY"
    vaulted = burp.environment_secret_refs["SHODAN_TOKEN"]
    assert vaulted.startswith("vault:")
    assert credentials.resolve(vaulted).get_secret_value() == "literal-shodan-secret"
    assert burp.metadata["imported"]["source_name"] == "claude.json"

    remote = profiles["remote"]
    assert remote.enabled is False
    assert remote.auth_mode == McpAuthMode.BEARER
    assert remote.bearer_secret_ref == "env:REMOTE_TOKEN"
    tenant = remote.header_secret_refs["X-Tenant-Key"]
    assert credentials.resolve(tenant).get_secret_value() == "literal-tenant-secret"
    assert remote.default_approval == McpApprovalMode.ASK
    assert remote.disabled_tools == ["delete_all"]
    assert remote.tool_timeout_seconds == 120
    assert report.entries[0].profile_id == burp.id
    assert {item.reference for item in report.entries[0].secrets} == {
        "env:BURP_API_KEY",
        vaulted,
    }
    assert b"literal-" not in database.read_bytes()


def test_literal_secrets_can_be_rejected_or_require_a_vault(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")

    rejected = _import(
        store, CredentialStore(MemoryKeyring()), literal_secrets="reject"
    )
    assert rejected.invalid == 2
    assert "env SHODAN_TOKEN contains a literal credential" in rejected.entries[0].error

    no_vault = _import(store, CredentialStore(UntrustedKeyring()), dry_run=False)
    assert no_vault.invalid == 2
    assert "vault is unavailable" in no_vault.entries[0].error
    assert store.list_entities(McpServerProfile) == []

    session = CredentialStore(UntrustedKeyring())
    stored = _import(store, session, dry_run=False, literal_secrets="session")
    assert stored.created == 2
    burp = next(
        item for item in store.list_entities(McpServerProfile) if item.name == "burp"
    )
    assert burp.environment_secret_refs["SHODAN_TOKEN"].startswith("session:")


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ({"command": "not-installed"}, "not installed on the Nebula host PATH"),
        ({"command": "./server.js"}, "absolute path on the Nebula host"),
        ({"type": "sse", "url": "https://x.test/sse"}, "legacy SSE transport"),
        ({"type": "websocket", "url": "wss://x.test"}, "unsupported MCP transport"),
        ({"url": "http://mcp.example.test/mcp"}, "require HTTPS"),
        ({"command": "npx", "env": {"KEY": "${input:key}"}}, "prompted ${input"),
        ({"command": "npx", "env": {"HOME_DIR": "${HOME}/x"}}, "mixes text"),
        (
            {"command": "npx", "args": ["--token", "${TOKEN}"]},
            "move the value into env",
        ),
        ({"command": "npx", "cwd": "relative"}, "cwd must be an absolute path"),
        ({"command": "npx", "url": "https://x.test"}, "cannot also define url"),
        ({"command": "npx", "nebula": {"trusted_stdio": True}}, "trusted_stdio"),
        ({"description": "nothing to run"}, "either a command or a url"),
        ("npx", "must be an object"),
    ],
)
def test_invalid_servers_explain_the_fix(tmp_path, server, message):
    store = NebulaStore(tmp_path / "nebula.db")
    report = _import(
        store, CredentialStore(MemoryKeyring()), {"servers": {"bad": server}}
    )
    assert report.invalid == 1
    assert message in report.entries[0].error


def test_document_shape_errors_reject_the_whole_import(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    credentials = CredentialStore(MemoryKeyring())
    for config, message in [
        ({"tools": []}, '"mcpServers"'),
        ({"mcpServers": []}, "object keyed by name"),
        ({"mcpServers": {}}, "does not define any"),
    ]:
        with pytest.raises(ValueError, match=message):
            _import(store, credentials, config)


def test_vscode_documents_names_and_absolute_commands(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    missing = str(tmp_path / "bin" / "server")
    report = _import(
        store,
        CredentialStore(MemoryKeyring()),
        {
            "mcp": {
                "servers": {
                    "@acme/Recon Tool": {
                        "type": "stdio",
                        "command": missing,
                        "env": {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"},
                        "cwd": str(tmp_path),
                    },
                    "acme-Recon-Tool": {"command": "uvx"},
                }
            }
        },
        dry_run=False,
    )

    first, second = report.entries
    assert first.name == "acme-Recon-Tool"
    assert any("renamed" in item for item in first.warnings)
    assert any("does not exist on the Nebula host" in item for item in first.warnings)
    assert second.action == "invalid" and "also named" in second.error
    profile = store.get(McpServerProfile, first.profile_id)
    assert profile.command == missing
    assert profile.cwd_policy == McpCwdPolicy.FIXED and profile.cwd == str(tmp_path)
    assert profile.environment_secret_refs == {"GITHUB_TOKEN": "env:GITHUB_TOKEN"}


def test_existing_names_are_skipped_or_replaced_for_review(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    keyring = MemoryKeyring()
    credentials = CredentialStore(keyring)
    _import(store, credentials, dry_run=False)
    burp = next(
        item for item in store.list_entities(McpServerProfile) if item.name == "burp"
    )
    old_secret = burp.environment_secret_refs["SHODAN_TOKEN"]
    store.update(
        McpServerProfile,
        burp.id,
        {
            "enabled": True,
            "trusted_stdio": True,
            "capabilities": McpCapabilitySnapshot(tools=[McpToolSnapshot(name="scan")]),
        },
        expected_revision=burp.revision,
    )

    skipped = _import(store, credentials, dry_run=False, on_conflict="skip")
    assert (skipped.skipped, skipped.created) == (2, 0)
    assert store.get(McpServerProfile, burp.id).enabled is True

    replacement = {"mcpServers": {"burp": {"command": "uvx", "args": ["burp-mcp"]}}}
    replaced = _import(
        store, credentials, replacement, dry_run=False, on_conflict="replace"
    )
    assert replaced.replaced == 1
    current = store.get(McpServerProfile, burp.id)
    assert current.command == "/opt/uv/bin/uvx"
    assert current.arguments == ["burp-mcp"]
    assert current.enabled is False and current.trusted_stdio is False
    assert current.environment_secret_refs == {} and current.environment == {}
    assert current.capabilities.tools == []
    assert current.created_at == burp.created_at
    assert credentials.status(old_secret).available is False
    assert len(store.list_entities(McpServerProfile)) == 2


def test_new_servers_follow_the_import_choices_and_the_file_wins(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    credentials = CredentialStore(MemoryKeyring())
    config = {
        "mcpServers": {
            "local": {"command": "npx", "args": ["-y", "local-mcp"]},
            "plain": {"url": "https://plain.example.test/mcp"},
            "strict": {
                "url": "https://strict.example.test/mcp",
                "nebula": {"default_approval": "deny"},
            },
        }
    }
    defaults = McpImportDefaults(enabled=True, default_approval=McpApprovalMode.ASK)

    untrusted = _import(store, credentials, config, defaults=defaults)
    local, plain, strict = untrusted.entries
    assert (local.enabled, local.needs_trust, local.needs_probe) == (False, True, False)
    assert local.default_approval == McpApprovalMode.ASK
    assert (plain.enabled, plain.needs_trust, plain.needs_probe) == (True, False, True)
    assert plain.default_approval == McpApprovalMode.ASK
    assert strict.default_approval == McpApprovalMode.DENY

    applied = _import(
        store,
        credentials,
        config,
        dry_run=False,
        defaults=defaults,
        trust_local_programs=True,
    )
    assert applied.created == 3
    assert applied.entries[0].needs_trust is True
    assert applied.entries[0].enabled is True
    assert applied.entries[0].needs_probe is True
    profiles = {item.name: item for item in store.list_entities(McpServerProfile)}
    assert profiles["local"].enabled and profiles["local"].trusted_stdio
    assert profiles["plain"].enabled and not profiles["plain"].trusted_stdio
    assert profiles["plain"].default_approval == McpApprovalMode.ASK
    assert profiles["strict"].default_approval == McpApprovalMode.DENY


def test_reimport_updates_only_what_changed(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    keyring = MemoryKeyring()
    credentials = CredentialStore(keyring)
    _import(
        store,
        credentials,
        dry_run=False,
        defaults=McpImportDefaults(enabled=True),
        trust_local_programs=True,
    )
    profiles = {item.name: item for item in store.list_entities(McpServerProfile)}
    probed = McpCapabilitySnapshot(tools=[McpToolSnapshot(name="scan")])
    for profile in profiles.values():
        store.update(
            McpServerProfile,
            profile.id,
            {
                "capabilities": probed,
                "tool_overrides": {"scan": McpApprovalMode.ALLOW},
            },
            expected_revision=profile.revision,
        )
    saved = {item.name: item for item in store.list_entities(McpServerProfile)}
    vault_entries = dict(keyring.values)

    same = _import(store, credentials, dry_run=False)
    assert (same.unchanged, same.updated, same.created) == (2, 0, 0)
    assert all(entry.changes == [] for entry in same.entries)
    assert keyring.values == vault_entries
    for profile in saved.values():
        assert store.get(McpServerProfile, profile.id).revision == profile.revision

    edited = copy.deepcopy(CLAUDE_CONFIG)
    burp = edited["mcpServers"]["burp"]
    burp["env"]["LOG_LEVEL"] = "info"
    del burp["env"]["BURP_API_KEY"]
    remote = edited["mcpServers"]["remote"]
    remote["headers"]["X-Tenant-Key"] = "rotated-tenant-secret"
    remote["nebula"]["default_approval"] = "allow"

    preview = _import(store, credentials, edited)
    burp_entry, remote_entry = preview.entries
    assert preview.updated == 2
    assert {(c.field, c.before, c.after) for c in burp_entry.changes} == {
        ("env LOG_LEVEL", "debug", "info"),
        ("env BURP_API_KEY", "${BURP_API_KEY}", None),
    }
    assert (burp_entry.enabled, burp_entry.needs_trust) == (False, True)
    assert {(c.field, c.before, c.after) for c in remote_entry.changes} == {
        ("header X-Tenant-Key", "stored credential", "new credential"),
        ("nebula.default_approval", "ask", "allow"),
    }
    assert (remote_entry.enabled, remote_entry.needs_probe) == (True, True)
    assert "rotated" not in preview.model_dump_json()

    applied = _import(store, credentials, edited, dry_run=False)
    assert applied.updated == 2
    burp_now = store.get(McpServerProfile, saved["burp"].id)
    assert burp_now.environment == {"LOG_LEVEL": "info"}
    assert set(burp_now.environment_secret_refs) == {"SHODAN_TOKEN"}
    assert (
        burp_now.environment_secret_refs["SHODAN_TOKEN"]
        == (saved["burp"].environment_secret_refs["SHODAN_TOKEN"])
    )
    assert burp_now.enabled is False and burp_now.trusted_stdio is False
    assert burp_now.capabilities.tools == []
    assert burp_now.tool_overrides == {"scan": McpApprovalMode.ALLOW}
    remote_now = store.get(McpServerProfile, saved["remote"].id)
    assert remote_now.enabled is True
    assert remote_now.default_approval == McpApprovalMode.ALLOW
    assert remote_now.tool_overrides == {"scan": McpApprovalMode.ALLOW}
    assert remote_now.disabled_tools == ["delete_all"]
    assert remote_now.capabilities.tools == []
    old_tenant = saved["remote"].header_secret_refs["X-Tenant-Key"]
    assert credentials.status(old_tenant).available is False
    tenant = remote_now.header_secret_refs["X-Tenant-Key"]
    assert credentials.resolve(tenant).get_secret_value() == "rotated-tenant-secret"


def test_reimport_keeps_a_changed_program_enabled_only_when_trusted(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    credentials = CredentialStore(MemoryKeyring())
    config = {"mcpServers": {"local": {"command": "npx", "args": ["local-mcp"]}}}
    _import(
        store,
        credentials,
        config,
        dry_run=False,
        defaults=McpImportDefaults(enabled=True),
        trust_local_programs=True,
    )
    config["mcpServers"]["local"]["args"] = ["local-mcp@2"]

    report = _import(
        store, credentials, config, dry_run=False, trust_local_programs=True
    )

    entry = report.entries[0]
    assert [(c.field, c.before, c.after) for c in entry.changes] == [
        ("args", "local-mcp", "local-mcp@2")
    ]
    assert (entry.enabled, entry.needs_trust, entry.needs_probe) == (True, True, True)
    profile = store.get(McpServerProfile, entry.profile_id)
    assert profile.enabled and profile.trusted_stdio
    assert profile.arguments == ["local-mcp@2"]


def test_export_round_trips_without_leaking_stored_credentials(tmp_path):
    store = NebulaStore(tmp_path / "source.db")
    credentials = CredentialStore(MemoryKeyring())
    _import(store, credentials, dry_run=False)

    exported = export_mcp_config(store.list_entities(McpServerProfile))
    text = json.dumps(exported.config)

    assert "literal-" not in text and "vault:" not in text
    burp = exported.config["mcpServers"]["burp"]
    assert burp["env"] == {
        "BURP_API_KEY": "${BURP_API_KEY}",
        "LOG_LEVEL": "debug",
        "SHODAN_TOKEN": "${SHODAN_TOKEN}",
    }
    remote = exported.config["mcpServers"]["remote"]
    assert remote["headers"] == {
        "Authorization": "Bearer ${REMOTE_TOKEN}",
        "X-Tenant-Key": "${REMOTE_X_TENANT_KEY}",
    }
    assert remote["nebula"] == {
        "default_approval": "ask",
        "disabled_tools": ["delete_all"],
        "tool_timeout_seconds": 120,
    }
    assert len(exported.warnings) == 2

    target = NebulaStore(tmp_path / "target.db")
    report = _import(
        target, CredentialStore(UntrustedKeyring()), exported.config, dry_run=False
    )
    assert (report.created, report.invalid) == (2, 0)
    reimported = {item.name: item for item in target.list_entities(McpServerProfile)}
    assert reimported["burp"].environment_secret_refs == {
        "BURP_API_KEY": "env:BURP_API_KEY",
        "SHODAN_TOKEN": "env:SHODAN_TOKEN",
    }
    assert reimported["remote"].header_secret_refs == {
        "X-Tenant-Key": "env:REMOTE_X_TENANT_KEY"
    }


def test_api_previews_applies_and_exports(tmp_path, monkeypatch):
    monkeypatch.setattr("nebula.v3.mcp_import.shutil.which", _which)
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(
        create_app(
            store,
            auth_token="test-token",
            credential_store=CredentialStore(MemoryKeyring()),
        )
    )
    auth = {"Authorization": "Bearer test-token"}

    with client:
        assert (
            client.post(
                "/api/v1/mcp-servers/import", json={"config": CLAUDE_CONFIG}
            ).status_code
            == 401
        )
        preview = client.post(
            "/api/v1/mcp-servers/import",
            headers=auth,
            json={"config": CLAUDE_CONFIG},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["created"] == 2
        assert client.get("/api/v1/mcp-servers", headers=auth).json() == []

        applied = client.post(
            "/api/v1/mcp-servers/import",
            headers=auth,
            json={"config": CLAUDE_CONFIG, "dry_run": False},
        )
        assert applied.status_code == 200, applied.text
        listed = client.get("/api/v1/mcp-servers", headers=auth).json()
        assert sorted(item["name"] for item in listed) == ["burp", "remote"]
        assert all(item["enabled"] is False for item in listed)

        remote_id = next(item["id"] for item in listed if item["name"] == "remote")
        exported = client.get(
            "/api/v1/mcp-servers/export",
            headers=auth,
            params={"profile_id": remote_id},
        )
        assert exported.status_code == 200, exported.text
        assert list(exported.json()["config"]["mcpServers"]) == ["remote"]

        malformed = client.post(
            "/api/v1/mcp-servers/import",
            headers=auth,
            json={"config": {"servers": []}},
        )
        assert malformed.status_code == 422
        assert "object keyed by name" in malformed.text


def test_cli_previews_then_applies_and_exports(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_V3_DATABASE_URL", raising=False)
    monkeypatch.setattr("nebula.v3.mcp_import.shutil.which", _which)
    monkeypatch.setattr(
        cli_module, "CredentialStore", lambda: CredentialStore(MemoryKeyring())
    )
    source = tmp_path / "claude_desktop_config.json"
    source.write_text(json.dumps(CLAUDE_CONFIG), encoding="utf-8")
    data_dir = tmp_path / "data"
    runner = CliRunner()

    preview = runner.invoke(
        cli_module.app, ["mcp", "import", str(source), "--data-dir", str(data_dir)]
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["dry_run"] is True
    store = NebulaStore(data_dir / "nebula.db")
    assert store.list_entities(McpServerProfile) == []

    applied = runner.invoke(
        cli_module.app,
        ["mcp", "import", str(source), "--apply", "--data-dir", str(data_dir)],
    )
    assert applied.exit_code == 0, applied.output
    assert len(store.list_entities(McpServerProfile)) == 2

    rejected = runner.invoke(
        cli_module.app,
        [
            "mcp",
            "import",
            str(source),
            "--replace",
            "--reject-literal-secrets",
            "--data-dir",
            str(data_dir),
        ],
    )
    assert rejected.exit_code == 1

    destination = tmp_path / "export.json"
    exported = runner.invoke(
        cli_module.app,
        ["mcp", "export", str(destination), "--data-dir", str(data_dir)],
    )
    assert exported.exit_code == 0, exported.output
    assert set(json.loads(destination.read_text())["mcpServers"]) == {
        "burp",
        "remote",
    }
    refused = runner.invoke(
        cli_module.app,
        ["mcp", "export", str(destination), "--data-dir", str(data_dir)],
    )
    assert refused.exit_code != 0


def test_api_and_cli_pass_the_import_choices(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_V3_DATABASE_URL", raising=False)
    monkeypatch.setattr("nebula.v3.mcp_import.shutil.which", _which)
    monkeypatch.setattr(
        cli_module, "CredentialStore", lambda: CredentialStore(MemoryKeyring())
    )
    config = {"mcpServers": {"local": {"command": "npx", "args": ["local-mcp"]}}}
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(
        create_app(
            store,
            auth_token="test-token",
            credential_store=CredentialStore(MemoryKeyring()),
        )
    )
    with client:
        response = client.post(
            "/api/v1/mcp-servers/import",
            headers={"Authorization": "Bearer test-token"},
            json={
                "config": config,
                "defaults": {"enabled": True, "default_approval": "ask"},
                "trust_local_programs": True,
            },
        )
    assert response.status_code == 200, response.text
    entry = response.json()["entries"][0]
    assert (entry["enabled"], entry["default_approval"]) == (True, "ask")
    assert entry["needs_probe"] is True

    source = tmp_path / "mcp.json"
    source.write_text(json.dumps(config), encoding="utf-8")
    data_dir = tmp_path / "data"
    runner = CliRunner()
    base = ["mcp", "import", str(source), "--data-dir", str(data_dir)]
    applied = runner.invoke(
        cli_module.app,
        [*base, "--apply", "--enable", "--approval", "allow", "--trust-local-programs"],
    )
    assert applied.exit_code == 0, applied.output
    (profile,) = NebulaStore(data_dir / "nebula.db").list_entities(McpServerProfile)
    assert profile.enabled and profile.trusted_stdio
    assert profile.default_approval == McpApprovalMode.ALLOW
    again = runner.invoke(cli_module.app, base)
    assert json.loads(again.output)["unchanged"] == 1
    skipped = runner.invoke(cli_module.app, [*base, "--skip-existing"])
    assert json.loads(skipped.output)["skipped"] == 1
    conflicting = runner.invoke(cli_module.app, [*base, "--replace", "--skip-existing"])
    assert conflicting.exit_code != 0


def test_failed_apply_rolls_back_secrets_it_already_stored(tmp_path):
    class FailingKeyring(MemoryKeyring):
        __module__ = MemoryKeyring.__module__

        def set_password(self, service_name, username, password):
            if self.values:
                raise RuntimeError("vault locked")
            super().set_password(service_name, username, password)

    store = NebulaStore(tmp_path / "nebula.db")
    keyring = FailingKeyring()
    config = {
        "mcpServers": {
            "two": {
                "command": "npx",
                "env": {"FIRST_TOKEN": "first-value", "SECOND_TOKEN": "second"},
            }
        }
    }

    report = _import(store, CredentialStore(keyring), config, dry_run=False)

    assert report.invalid == 1
    assert "could not save" in report.entries[0].error
    assert keyring.values == {}
    assert store.list_entities(McpServerProfile) == []


def test_checked_in_schema_matches_the_importer():
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from nebula.v3.mcp_import import mcp_config_json_schema

    schema = mcp_config_json_schema()
    Draft202012Validator.check_schema(schema)
    checked_in = Path(__file__).resolve().parents[2] / "docs/mcp-servers.schema.json"
    assert json.loads(checked_in.read_text(encoding="utf-8")) == schema, (
        "regenerate with: nebula-core mcp schema > docs/mcp-servers.schema.json"
    )


def test_schema_accepts_documented_examples_and_rejects_what_import_rejects():
    import re
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from nebula.v3.mcp_import import mcp_config_json_schema

    validator = Draft202012Validator(mcp_config_json_schema())
    guide = (Path(__file__).resolve().parents[2] / "docs/MCP-SERVERS.md").read_text(
        encoding="utf-8"
    )
    examples = [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)```", guide, flags=re.DOTALL)
    ]
    assert len(examples) >= 2
    for example in [*examples, CLAUDE_CONFIG]:
        assert validator.is_valid(example), list(validator.iter_errors(example))

    for server in [
        {"command": "./server.js"},
        {"command": "~/bin/server"},
        {"type": "sse", "url": "https://x.test/sse"},
        {"command": "npx", "url": "https://x.test"},
        {"type": "stdio", "url": "https://x.test"},
        {"url": "https://x.test", "env": {"A": "b"}},
        {"command": "npx", "cwd": "relative"},
        {"command": "npx", "nebula": {"trusted_stdio": True}},
        {"command": "npx", "nebula": {"enabled": True}},
        {"command": "npx", "nebula": {"default_approval": "always"}},
        {"command": "npx", "nebula": {"tool_timeout_seconds": 901}},
        {"description": "nothing to run"},
    ]:
        document = {"mcpServers": {"bad": server}}
        assert not validator.is_valid(document), server
    assert not validator.is_valid({"tools": {}})
    assert not validator.is_valid({"mcpServers": {}})


def test_schema_is_served_by_api_and_cli(tmp_path):
    from nebula.v3.mcp_import import mcp_config_json_schema

    client = TestClient(create_app(NebulaStore(tmp_path / "n.db"), auth_token="t"))
    with client:
        assert client.get("/api/v1/mcp-servers/schema").status_code == 401
        served = client.get(
            "/api/v1/mcp-servers/schema", headers={"Authorization": "Bearer t"}
        )
    assert served.status_code == 200
    assert served.json() == mcp_config_json_schema()

    printed = CliRunner().invoke(cli_module.app, ["mcp", "schema"])
    assert printed.exit_code == 0
    assert json.loads(printed.output) == mcp_config_json_schema()
