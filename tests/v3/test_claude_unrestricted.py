"""Claude Agent SDK harness profiles in unrestricted permission mode.

Fakes only: no network, no SDK process, no vendor account.
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from types import SimpleNamespace
from typing import Any

import pytest

from nebula.v3 import harnesses as harness_module
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessWorkspaceAccess,
)
from nebula.v3.harnesses import ClaudeAgentSdkAdapter
from tests.v3.test_butler_claude_harness import (
    CLAUDE_MODEL,
    _Client,
    _Options,
    _claude_harness,
)

SHELL = HarnessNativeCapabilities(
    workspace_access=HarnessWorkspaceAccess.WRITE, shell=True
)


class _HookMatcher:
    def __init__(self, *, matcher: Any, hooks: list[Any]) -> None:
        self.matcher = matcher
        self.hooks = hooks


def _sdk() -> SimpleNamespace:
    return SimpleNamespace(
        __version__="0.2.118",
        ClaudeAgentOptions=_Options,
        ClaudeSDKClient=_Client,
        HookMatcher=_HookMatcher,
    )


def _session(profile: HarnessProfile, **metadata: Any) -> HarnessSession:
    values: dict[str, Any] = {
        "execution_mode": "host",
        "harness_permission_mode": profile.permission_mode,
        "scope_snapshot": {"bypass_permissions": True},
        "native_capabilities": SHELL.model_dump(mode="json"),
    }
    values.update(metadata)
    return HarnessSession(
        engagement_id="project",
        harness_profile_id=profile.id,
        model=CLAUDE_MODEL,
        metadata=values,
    )


async def _open(tmp_path, monkeypatch, session, profile) -> dict[str, Any]:
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_sdk))
    _Client.opened.clear()

    async def no_prompt(_request: Any) -> Any:
        raise AssertionError("an unrestricted native call must not ask Core")

    connection = await ClaudeAgentSdkAdapter().open(
        harness_module.AdapterOpenRequest(
            profile=profile,
            session=session,
            workspace=tmp_path,
            mcp_profiles=(),
            credential_store=CredentialStore(),
            permission_handler=no_prompt,
        )
    )
    await connection.close()
    return _Client.opened[0].options.kwargs


async def _hook_decision(options: dict[str, Any], tool_name: str) -> str:
    hook = options["hooks"]["PreToolUse"][0].hooks[0]
    result = await hook({"tool_name": tool_name, "tool_input": {}}, None, None)
    return result["hookSpecificOutput"]["permissionDecision"]


def test_claude_profile_accepts_unrestricted_permission_mode():
    profile = _claude_harness(permission_mode="unrestricted")
    assert profile.kind == HarnessKind.CLAUDE_AGENT_SDK
    assert profile.permission_mode == "unrestricted"


async def test_unrestricted_host_session_bypasses_permissions_without_sandbox(
    tmp_path, monkeypatch
):
    profile = _claude_harness(permission_mode="unrestricted", native_capabilities=SHELL)
    options = await _open(tmp_path, monkeypatch, _session(profile), profile)

    assert options["permission_mode"] == "bypassPermissions"
    assert options["sandbox"] == {"enabled": False}
    assert "can_use_tool" not in options
    assert "Bash" in options["tools"]
    assert await _hook_decision(options, "Bash") == "allow"
    # A native tool the profile did not enable stays denied.
    assert await _hook_decision(options, "WebFetch") == "deny"


@pytest.mark.parametrize(
    "metadata",
    [
        {"harness_permission_mode": "managed"},
        {"scope_snapshot": {"bypass_permissions": False}},
        {"execution_mode": "docker"},
        {"control_assistant": True},
        {"analysis_only": True},
        {"planning_enforced": True},
        {"planning_target_turn_id": "turn"},
    ],
    ids=[
        "managed-profile",
        "no-allow-all",
        "docker",
        "butler",
        "analysis",
        "core-planned",
        "planner",
    ],
)
async def test_unrestricted_claude_stays_managed_outside_eligible_sessions(
    tmp_path, monkeypatch, metadata
):
    profile = _claude_harness(permission_mode="unrestricted", native_capabilities=SHELL)
    options = await _open(tmp_path, monkeypatch, _session(profile, **metadata), profile)

    assert options["permission_mode"] == "default"
    assert options["sandbox"]["enabled"] is True
    assert options["sandbox"]["network"]["allowedDomains"] == []
    assert callable(options["can_use_tool"])
    if "Bash" in options["tools"]:
        assert await _hook_decision(options, "Bash") == "ask"


async def test_managed_claude_profile_keeps_sandbox_and_core_approval(
    tmp_path, monkeypatch
):
    profile = _claude_harness(native_capabilities=SHELL)
    options = await _open(tmp_path, monkeypatch, _session(profile), profile)

    assert options["permission_mode"] == "default"
    assert options["sandbox"]["enabled"] is True
    assert await _hook_decision(options, "Bash") == "ask"
