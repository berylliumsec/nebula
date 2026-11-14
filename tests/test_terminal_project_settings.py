"""Project tool follow-up and VPN route: terminal parity with GUI Settings."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from nebula.terminal import projects
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, ClientError, Settings
from tests.test_terminal_shell import CONFIG, CORE, ENTRY, PROJECT, SESSION, UI


def path(request):
    return request.url.path.removeprefix("/api/v1/")


def shell_for(tmp_path, handler, ui):
    settings = Settings(tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE)
    api = AsyncApi(settings, transport=httpx.MockTransport(handler))
    shell = Shell(settings, ui=ui, api=api)
    shell.view = View(
        copy.deepcopy(PROJECT), copy.deepcopy(SESSION), copy.deepcopy(CONFIG)
    )
    return shell


PROVIDERS = [
    {
        "id": "local-p",
        "name": "Local",
        "enabled": True,
        "is_local": True,
        "model_allowlist": ["small"],
        "privacy": {"local_only": True},
    },
    {
        "id": "cloud-p",
        "name": "Cloud",
        "enabled": True,
        "is_local": False,
        "model_allowlist": ["big"],
        "privacy": {"local_only": False, "permits_sensitive_data": True},
    },
    {
        "id": "closed-p",
        "name": "Closed",
        "enabled": True,
        "model_allowlist": ["x"],
        "privacy": {"local_only": False, "permits_sensitive_data": False},
    },
    {"id": "off-p", "name": "Off", "enabled": False, "model_allowlist": ["y"]},
]
HARNESSES = [
    {
        "id": "h",
        "name": "Harness",
        "enabled": True,
        "default_model": "alias",
        "capabilities": {"models": ["alias", "other"]},
        "privacy": {"local_only": True},
    }
]


class FollowupCore:
    def __init__(self):
        self.config = {
            "suggest_next_steps": False,
            "take_notes": False,
            "provider_id": None,
            "backend_kind": "provider",
            "harness_profile_id": None,
            "model": None,
            "cloud_confirmed": False,
            "revision": 4,
        }
        self.puts = []

    def __call__(self, request):
        route = path(request)
        if route == "engagements/project-a/post-tool-assistant":
            if request.method == "PUT":
                body = json.loads(request.content)
                assert body.pop("expected_revision") == self.config["revision"]
                self.puts.append(body)
                self.config = {**body, "revision": self.config["revision"] + 1}
            return httpx.Response(200, json=self.config)
        if route == "providers":
            return httpx.Response(200, json=PROVIDERS)
        if route == "harnesses":
            return httpx.Response(200, json=HARNESSES)
        pytest.fail(route)


def test_tool_followup_needs_a_runtime_then_saves_the_whole_config(tmp_path):
    core = FollowupCore()

    async def run():
        shell = shell_for(
            tmp_path,
            core,
            UI(
                choices=[
                    "take_notes",
                    "runtime",
                    "provider:local-p",
                    "small",
                    "take_notes",
                    "__done",
                ]
            ),
        )
        with pytest.raises(ClientError, match="Choose an analysis runtime first"):
            await projects.post_tool(shell, PROJECT)
        assert core.puts == []
        await projects.post_tool(shell, PROJECT)
        assert core.puts == [
            {
                "suggest_next_steps": False,
                "take_notes": False,
                "provider_id": "local-p",
                "backend_kind": "provider",
                "harness_profile_id": None,
                "model": "small",
                "cloud_confirmed": False,
            },
            {
                "suggest_next_steps": False,
                "take_notes": True,
                "provider_id": "local-p",
                "backend_kind": "provider",
                "harness_profile_id": None,
                "model": "small",
                "cloud_confirmed": False,
            },
        ]
        await shell.api.close()

    asyncio.run(run())


def test_cloud_runtime_needs_permission_and_explicit_confirmation(tmp_path):
    core = FollowupCore()
    seen = []

    class Recording(UI):
        async def choose(self, title, items, **kwargs):
            seen.append((title, [row["id"] for row in items]))
            return await super().choose(title, items, **kwargs)

    async def run():
        shell = shell_for(
            tmp_path,
            core,
            Recording(
                choices=[
                    "runtime",
                    "provider:closed-p",
                    "x",
                    "runtime",
                    "provider:cloud-p",
                    "big",
                    "runtime",
                    "provider:cloud-p",
                    "big",
                    "__done",
                ],
                confirms=[False, True],
            ),
        )
        with pytest.raises(ClientError, match="does not permit sensitive project data"):
            await projects.post_tool(shell, PROJECT)
        await projects.post_tool(shell, PROJECT)
        assert len(core.puts) == 1 and core.puts[0]["cloud_confirmed"] is True
        assert core.puts[0]["provider_id"] == "cloud-p"
        runtimes = next(ids for title, ids in seen if title == "Tool follow-up runtime")
        assert "provider:off-p" not in runtimes and "harness:h" in runtimes
        await shell.api.close()

    asyncio.run(run())


def test_harness_models_come_from_its_default_and_capabilities(tmp_path):
    core = FollowupCore()
    offered = []

    class Recording(UI):
        async def choose(self, title, items, **kwargs):
            if title == "Tool follow-up model":
                offered.append([row["id"] for row in items])
            return await super().choose(title, items, **kwargs)

    async def run():
        shell = shell_for(
            tmp_path,
            core,
            Recording(choices=["runtime", "harness:h", "other", "__done"]),
        )
        await projects.post_tool(shell, PROJECT)
        assert offered == [["alias", "other"]]
        assert core.puts[0]["backend_kind"] == "harness"
        assert core.puts[0]["harness_profile_id"] == "h"
        assert core.puts[0]["provider_id"] is None
        await shell.api.close()

    asyncio.run(run())


class PolicyCore:
    def __init__(self, mode="docker", network=False, vpn=None):
        self.policy = {
            "execution_mode": mode,
            "host_access_acknowledged": mode == "host",
            "approval_policy": "on_boundary",
            "network_enabled": network,
            "runner_profile_id": "runner-a",
            "vpn_profile_id": vpn,
            "max_timeout_ms": 300000,
            "revision": 3,
        }
        self.puts = []

    def __call__(self, request):
        route = path(request)
        if route == "engagements/project-a/automation-policy":
            if request.method == "PUT":
                body = json.loads(request.content)
                self.puts.append(body)
                self.policy.update(
                    {k: v for k, v in body.items() if k != "expected_revision"}
                )
                self.policy["revision"] += 1
            return httpx.Response(200, json=self.policy)
        if route == "vpn-profiles":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "vpn-a",
                        "name": "Lab VPN",
                        "protocol": "udp",
                        "remote_host": "vpn.test",
                        "available": True,
                    },
                    {
                        "id": "vpn-b",
                        "name": "Locked",
                        "protocol": "tcp",
                        "remote_host": "x.test",
                        "available": False,
                    },
                ],
            )
        pytest.fail(route)


def test_choosing_a_vpn_writes_the_whole_policy_with_network_on(tmp_path):
    core = PolicyCore()

    async def run():
        shell = shell_for(
            tmp_path, core, UI(choices=["vpn", "vpn-a", "vpn", "__direct", "__done"])
        )
        await projects.policy(shell, {"id": "project-a", "name": "Project A"})
        assert core.puts[0] == {
            "execution_mode": "docker",
            "host_access_acknowledged": False,
            "approval_policy": "on_boundary",
            "network_enabled": True,
            "runner_profile_id": "runner-a",
            "vpn_profile_id": "vpn-a",
            "max_timeout_ms": 300000,
            "expected_revision": 3,
        }
        assert core.puts[1]["vpn_profile_id"] is None
        assert core.puts[1]["network_enabled"] is True
        await shell.api.close()

    asyncio.run(run())


def test_vpn_is_refused_in_host_mode_and_for_unavailable_profiles(tmp_path):
    host = PolicyCore(mode="host", network=True)
    docker = PolicyCore()

    async def run():
        shell = shell_for(tmp_path, host, UI(choices=["vpn"]))
        with pytest.raises(ClientError, match="Host commands do not use container VPN"):
            await projects.policy(shell, {"id": "project-a", "name": "Project A"})
        await shell.api.close()
        shell = shell_for(tmp_path, docker, UI(choices=["vpn", "vpn-b"]))
        with pytest.raises(ClientError, match="credentials are unavailable"):
            await projects.policy(shell, {"id": "project-a", "name": "Project A"})
        assert host.puts == [] and docker.puts == []
        await shell.api.close()

    asyncio.run(run())


def test_turning_network_off_clears_the_vpn_route(tmp_path):
    core = PolicyCore(network=True, vpn="vpn-a")

    async def run():
        shell = shell_for(tmp_path, core, UI(choices=["network", "__done"]))
        await projects.policy(shell, {"id": "project-a", "name": "Project A"})
        assert core.puts[0]["network_enabled"] is False
        assert core.puts[0]["vpn_profile_id"] is None
        await shell.api.close()

    asyncio.run(run())
