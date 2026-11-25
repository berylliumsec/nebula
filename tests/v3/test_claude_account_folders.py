"""Per-profile Claude account folders (CLAUDE_CONFIG_DIR), like Codex/Grok homes.

Fakes only: no network, no SDK process, no vendor account.
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nebula.v3 import harnesses as harness_module
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    HarnessAuthMode,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
)
from nebula.v3.harnesses import (
    ClaudeAgentSdkAdapter,
    HarnessConfigurationError,
    HarnessRuntimeService,
    _claude_cli_logged_in,
    _claude_login_location,
    _claude_not_logged_in_detail,
    _harness_environment,
    _minimal_environment,
    _scrubbed_claude_environment,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_butler_claude_harness import (
    CLAUDE_MODEL,
    _Client,
    _claude_harness,
    _fake_sdk,
)


def _previous_scrubbed_claude_environment(
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The construction before account folders, kept as the reference."""

    minimal = _minimal_environment()
    scrubbed = {key: "" for key in os.environ if key not in minimal}
    scrubbed.update(minimal)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir:
        scrubbed["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        scrubbed.pop("CLAUDE_CONFIG_DIR", None)
    scrubbed.update(extra or {})
    return scrubbed


def _session(profile: HarnessProfile) -> HarnessSession:
    return HarnessSession(
        engagement_id="project",
        harness_profile_id=profile.id,
        model=CLAUDE_MODEL,
    )


async def _deny(_request: Any) -> Any:
    raise AssertionError("no permission request expected")


def _open_request(profile: HarnessProfile, workspace: Path) -> Any:
    return harness_module.AdapterOpenRequest(
        profile=profile,
        session=_session(profile),
        workspace=workspace,
        mcp_profiles=(),
        credential_store=CredentialStore(),
        permission_handler=_deny,
    )


def _fake_cli(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cli = directory / "claude"
    cli.write_text("#!/bin/sh\n" + body)
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


# --- Environment construction ------------------------------------------------


def test_claude_profile_with_account_folder_sets_config_dir(tmp_path, monkeypatch):
    folder = tmp_path / "claude-work"
    folder.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/example/core-claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    profile = _claude_harness(home_directory=str(folder))
    env = _scrubbed_claude_environment(profile=profile)
    assert env["CLAUDE_CONFIG_DIR"] == str(folder)
    assert env["ANTHROPIC_API_KEY"] == ""
    # Only CLAUDE_CONFIG_DIR differs from the profile-less construction.
    baseline = _previous_scrubbed_claude_environment()
    assert {key for key in env if env.get(key) != baseline.get(key)} == {
        "CLAUDE_CONFIG_DIR"
    }
    # Building the env never writes the folder into Core's own environment.
    assert os.environ["CLAUDE_CONFIG_DIR"] == "/opt/example/core-claude"


@pytest.mark.parametrize("core_config_dir", [None, "", "/opt/example/core-claude"])
def test_claude_profile_without_account_folder_is_unchanged(
    monkeypatch, core_config_dir
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    if core_config_dir is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", core_config_dir)
    expected = _previous_scrubbed_claude_environment()
    assert _scrubbed_claude_environment() == expected
    assert _scrubbed_claude_environment(profile=_claude_harness()) == expected
    assert list(_scrubbed_claude_environment(profile=_claude_harness())) == list(
        expected
    )
    assert _claude_login_location(_claude_harness()) == _claude_login_location()
    assert (
        _claude_not_logged_in_detail(_claude_harness())
        == _claude_not_logged_in_detail()
    )
    assert "claude auth login" in _claude_not_logged_in_detail()


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        (HarnessKind.CODEX_APP_SERVER, "CODEX_HOME"),
        (HarnessKind.GROK_ACP, "GROK_HOME"),
    ],
)
def test_codex_and_grok_account_environment_is_unchanged(
    tmp_path, monkeypatch, kind, key
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/example/core-claude")
    home = tmp_path / "account"
    home.mkdir()
    profile = HarnessProfile(
        name="account", kind=kind, executable="/bin/true", home_directory=str(home)
    )
    assert _harness_environment(profile) == {**_minimal_environment(), key: str(home)}
    default = HarnessProfile(name="default", kind=kind, executable="/bin/true")
    assert _harness_environment(default) == _minimal_environment()
    missing = HarnessProfile(
        name="missing",
        kind=kind,
        executable="/bin/true",
        home_directory=str(tmp_path / "missing"),
    )
    with pytest.raises(
        HarnessConfigurationError, match="Account folder is unavailable"
    ):
        _harness_environment(missing)


def test_claude_account_folder_rules_match_other_managed_harnesses():
    profile = _claude_harness(home_directory="/accounts/claude-work/")
    assert profile.home_directory == "/accounts/claude-work"
    with pytest.raises(ValueError, match="account home"):
        _claude_harness(home_directory="relative/claude")
    with pytest.raises(ValueError, match="managed Codex, Grok and Claude"):
        HarnessProfile(
            name="remote",
            kind=HarnessKind.CODEX_APP_SERVER,
            connection_mode="endpoint",
            transport="unix",
            endpoint="unix:///tmp/test.sock",
            home_directory="/accounts/codex",
        )


# --- Sign-in guidance --------------------------------------------------------


def test_signed_out_detail_names_the_profile_folder_and_login_command():
    profile = _claude_harness(home_directory="/accounts/claude work")
    location = _claude_login_location(profile)
    assert "CLAUDE_CONFIG_DIR '/accounts/claude work'" in location
    assert "this profile's account folder" in location
    detail = _claude_not_logged_in_detail(profile)
    assert "CLAUDE_CONFIG_DIR='/accounts/claude work' claude login" in detail
    assert "claude auth login" not in detail
    simple = _claude_not_logged_in_detail(
        _claude_harness(home_directory="/accounts/claude-work")
    )
    assert "`CLAUDE_CONFIG_DIR=/accounts/claude-work claude login`" in simple


async def test_check_names_the_profile_folder_when_signed_out(tmp_path, monkeypatch):
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))
    folder = tmp_path / "claude-work"
    folder.mkdir()
    seen: list[HarnessProfile] = []

    async def logged_in(_self: Any, _sdk: Any, profile: Any) -> bool | None:
        seen.append(profile)
        return False

    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_cli_logged_in", logged_in)
    profile = _claude_harness(
        home_directory=str(folder), auth_mode=HarnessAuthMode.EXISTING_SESSION
    )
    health = await ClaudeAgentSdkAdapter().probe(profile, CredentialStore())
    assert health.healthy is False
    assert f"CLAUDE_CONFIG_DIR={folder} claude login" in (health.detail or "")
    assert seen[0].home_directory == str(folder)


# --- Missing folder ----------------------------------------------------------


async def test_missing_folder_fails_check_and_turn_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))
    _Client.opened.clear()
    profile = _claude_harness(home_directory=str(tmp_path / "missing"))

    health = await ClaudeAgentSdkAdapter().probe(profile, CredentialStore())
    assert health.healthy is False
    assert "Account folder is unavailable on the Nebula host" in (health.detail or "")

    with pytest.raises(
        HarnessConfigurationError, match="Account folder is unavailable"
    ):
        await ClaudeAgentSdkAdapter().open(_open_request(profile, tmp_path))
    assert _Client.opened == []

    with pytest.raises(
        HarnessConfigurationError, match="Account folder is unavailable"
    ):
        await _claude_cli_logged_in("/bin/true", profile)

    store = NebulaStore(tmp_path / "missing.db")
    saved = await store.create(profile)
    runtime = HarnessRuntimeService(
        store, credential_store=CredentialStore(), workspace_resolver=lambda _: tmp_path
    )
    checked = await runtime.health(saved.id)
    assert checked.healthy is False
    assert "Account folder is unavailable" in (checked.detail or "")
    turn = await runtime.test_turn(saved.id)
    assert turn.capabilities.turn_state == "failed"
    assert _Client.opened == []


# --- Turns, auth status and concurrency ---------------------------------------


async def test_turn_and_auth_status_use_the_profile_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/example/core-claude")
    _Client.opened.clear()
    folder = tmp_path / "claude-work"
    folder.mkdir()
    profile = _claude_harness(home_directory=str(folder))
    connection = await ClaudeAgentSdkAdapter().open(_open_request(profile, tmp_path))
    await connection.close()
    assert _Client.opened[0].options.kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(folder)

    seen = tmp_path / "seen.txt"
    cli = _fake_cli(
        tmp_path / "bin",
        f'echo "$CLAUDE_CONFIG_DIR" > {seen}\necho \'{{"loggedIn": true}}\'\n',
    )
    assert await _claude_cli_logged_in(str(cli), profile) is True
    assert seen.read_text().strip() == str(folder)
    assert await _claude_cli_logged_in(str(cli)) is True
    assert seen.read_text().strip() == "/opt/example/core-claude"


async def test_two_profiles_run_concurrently_with_their_own_folders(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    both_connecting = asyncio.Barrier(3)

    class _BarrierClient(_Client):
        async def connect(self) -> None:
            # Both connections are open at once before either continues.
            async with asyncio.timeout(5):
                await both_connecting.wait()

    def sdk() -> Any:
        fake = _fake_sdk()
        fake.ClaudeSDKClient = _BarrierClient
        return fake

    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(sdk))
    _Client.opened.clear()
    folders = [tmp_path / "claude-one", tmp_path / "claude-two"]
    for folder in folders:
        folder.mkdir()
    profiles = [
        _claude_harness(name=f"Claude {index}", home_directory=str(folder))
        for index, folder in enumerate(folders)
    ]
    default = _claude_harness(name="Claude default")

    async def open_profile(profile: HarnessProfile) -> None:
        connection = await ClaudeAgentSdkAdapter().open(
            _open_request(profile, tmp_path)
        )
        await connection.close()

    async def release() -> None:
        await both_connecting.wait()

    await asyncio.gather(*(open_profile(profile) for profile in profiles), release())
    observed = sorted(
        client.options.kwargs["env"].get("CLAUDE_CONFIG_DIR")
        for client in _Client.opened
    )
    assert observed == sorted(str(folder) for folder in folders)

    # A later profile without a folder (for example a subagent profile) gets
    # neither folder: its environment is the unchanged default construction.
    both_connecting = asyncio.Barrier(1)
    _Client.opened.clear()
    connection = await ClaudeAgentSdkAdapter().open(_open_request(default, tmp_path))
    await connection.close()
    env = _Client.opened[0].options.kwargs["env"]
    assert "CLAUDE_CONFIG_DIR" not in env
    assert env == _previous_scrubbed_claude_environment()
    assert "CLAUDE_CONFIG_DIR" not in os.environ

    # Concurrent login-status reads see their own folders as well.
    cli_dir = tmp_path / "bin"
    cli = _fake_cli(
        cli_dir,
        'sleep 0.2\necho "{\\"loggedIn\\": true, \\"dir\\": \\"$CLAUDE_CONFIG_DIR\\"}" '
        '> "$CLAUDE_CONFIG_DIR/status.json"\necho \'{"loggedIn": true}\'\n',
    )
    results = await asyncio.gather(
        *(_claude_cli_logged_in(str(cli), profile) for profile in profiles)
    )
    assert results == [True, True]
    for folder in folders:
        assert f'"dir": "{folder}"' in (folder / "status.json").read_text()
