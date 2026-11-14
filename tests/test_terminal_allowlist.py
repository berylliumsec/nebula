"""Allowing another model a provider reports, from the terminal /model picker."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from nebula.terminal.settings import model
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, ClientError, Settings
from tests.test_terminal_shell import CONFIG, CORE, ENTRY, PROJECT, SESSION, UI

PROVIDER = {
    "id": "provider-a",
    "name": "Provider A",
    "enabled": True,
    "revision": 4,
    "model_allowlist": ["fixture-model"],
}


def path(request):
    return request.url.path.removeprefix("/api/v1/")


def core(calls, *, unlisted=("model-b",), healthy=True):
    provider = dict(PROVIDER)

    def handler(request):
        route = path(request)
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, route, body))
        if route == "chat-sessions/chat-a/assistant-config":
            return httpx.Response(200, json={"settings": CONFIG, "revision": 2})
        if route == "providers" and request.method == "GET":
            return httpx.Response(200, json=[provider])
        if route == "harnesses":
            return httpx.Response(200, json=[])
        if route == "providers/provider-a/health":
            return httpx.Response(
                200,
                json={
                    "provider_id": "provider-a",
                    "healthy": healthy,
                    "detail": None if healthy else "connection refused",
                    "models": ["fixture-model"],
                    "unlisted_models": list(unlisted),
                    "provider_revision": 5,
                },
            )
        if route == "providers/provider-a" and request.method == "GET":
            return httpx.Response(200, json=provider)
        if route == "providers/provider-a" and request.method == "PATCH":
            assert body["expected_revision"] == 5
            provider.update(body["changes"], revision=6)
            return httpx.Response(200, json=provider)
        if route == "chat-sessions/chat-a":
            return httpx.Response(200, json=SESSION)
        if route.endswith("runtime-switch/preflight"):
            return httpx.Response(200, json={"compatible": True})
        if route.endswith("runtime-switch"):
            return httpx.Response(200, json={})
        if route == "engagements/project-a/assistant-defaults":
            return httpx.Response(200, json={})
        pytest.fail(route)

    return handler


def test_allowing_a_reported_model_patches_the_allowlist_then_switches(tmp_path):
    calls = []

    async def run():
        settings = Settings(
            tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE
        )
        api = AsyncApi(settings, transport=httpx.MockTransport(core(calls)))
        shell = Shell(settings, ui=UI(choices=["more:provider-a", "model-b"]), api=api)
        shell.view = View(
            copy.deepcopy(PROJECT), copy.deepcopy(SESSION), copy.deepcopy(CONFIG)
        )
        await model(shell)
        patch = next(
            body
            for method, route, body in calls
            if method == "PATCH" and route == "providers/provider-a"
        )
        assert patch == {
            "changes": {"model_allowlist": ["fixture-model", "model-b"]},
            "expected_revision": 5,
        }
        switch = next(
            body for _, route, body in calls if route.endswith("/runtime-switch")
        )
        assert switch["model"] == "model-b" and switch["provider_id"] == "provider-a"
        assert "Added model-b to Provider A's allowed models." in shell.ui.output
        await api.close()

    asyncio.run(run())


def test_no_reported_models_explains_without_writing(tmp_path):
    calls = []

    async def run():
        settings = Settings(
            tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE
        )
        api = AsyncApi(
            settings, transport=httpx.MockTransport(core(calls, unlisted=()))
        )
        shell = Shell(settings, ui=UI(choices=["more:provider-a"]), api=api)
        shell.view = View(
            copy.deepcopy(PROJECT), copy.deepcopy(SESSION), copy.deepcopy(CONFIG)
        )
        with pytest.raises(ClientError, match="reports no other models"):
            await model(shell)
        assert not any(method == "PATCH" for method, _, _ in calls)
        await api.close()

    asyncio.run(run())


def test_unhealthy_provider_reports_core_detail_without_writing(tmp_path):
    calls = []

    async def run():
        settings = Settings(
            tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE
        )
        api = AsyncApi(
            settings,
            transport=httpx.MockTransport(core(calls, unlisted=(), healthy=False)),
        )
        shell = Shell(settings, ui=UI(choices=["more:provider-a"]), api=api)
        shell.view = View(
            copy.deepcopy(PROJECT), copy.deepcopy(SESSION), copy.deepcopy(CONFIG)
        )
        with pytest.raises(ClientError, match="connection refused"):
            await model(shell)
        assert not any(method == "PATCH" for method, _, _ in calls)
        await api.close()

    asyncio.run(run())
