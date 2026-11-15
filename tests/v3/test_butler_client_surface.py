"""Butler never offers a terminal client a handoff only a web browser can apply."""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json

import pytest
from pydantic import ValidationError

import nebula.v3.chat as chat_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import (
    BUTLER_TERMINAL_SURFACE_INSTRUCTION,
    ButlerCapabilities,
)
from nebula.v3.chat import ChatCompletionRequest
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    Engagement,
    HarnessTurn,
    Observation,
    ProviderProfile,
    RiskClass,
)
from nebula.v3.providers import ModelRequest, ModelResponse, ModelUsage, ToolCall
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_failures import tool_failure
from nebula.v3.tools import (
    AnalysisTool,
    CapabilityInputError,
    IdempotencyBehavior,
    ToolSpec,
)
from tests.v3.async_test_support import AsyncTestClient as TestClient
from tests.v3.test_butler_capabilities import _capabilities
from tests.v3.test_butler_memory import ResponseProvider

pytestmark = pytest.mark.anyio

_THEME = {
    "operation": "nebula.settings.set_device_theme",
    "arguments": {"theme": "light"},
}


async def _surface(tmp_path, client_surface):
    store, project, own_chat, _, browser = await _capabilities(tmp_path)
    terminal = await ButlerCapabilities.create(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        project.id,
        work=browser._work,
        client_surface=client_surface,
    )
    return store, project, own_chat, browser, terminal


def _browser_only(error: CapabilityInputError, operation: str) -> None:
    assert error.reason == "browser_only"
    assert error._nebula_before_execution is True
    spec = ToolSpec(
        name="nebula.capabilities.change",
        description="change",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_class=RiskClass.WORKSPACE_WRITE,
    )
    failure = tool_failure(
        spec, {"operation": operation}, error, phase="before_execution"
    )
    assert failure["side_effects"] == "none"
    assert "Nebula web app" in failure["problem"]
    assert "Nebula web app" in failure["next_action"]
    assert "pending_browser_apply" not in json.dumps(failure)


async def test_terminal_butler_refuses_existing_browser_handoffs(tmp_path):
    store, project, _, browser, terminal = await _surface(tmp_path, "terminal")
    note = await store.create(
        Observation(engagement_id=project.id, observation_type="note", title="Lead")
    )
    handoffs = [
        (_THEME, True),
        (
            {
                "operation": "nebula.navigate.open",
                "arguments": {"destination_id": "notebook"},
            },
            True,
        ),
        ({"operation": "nebula.diagnostics.export", "arguments": {}}, True),
        (
            {
                "operation": "nebula.notes.delete",
                "arguments": {
                    "note_id": note.id,
                    "expected_revision": note.revision,
                },
            },
            True,
        ),
    ]
    for payload, change in handoffs:
        # The web app still receives the exact handoff it applies.
        handed_off = await browser.invoke(payload, change=change)
        assert handed_off["state"] == "pending_browser_apply"
        with pytest.raises(CapabilityInputError) as refused:
            await terminal.invoke(payload, change=change)
        _browser_only(refused.value, payload["operation"])
    # Refusing the delete handoff left the note in Core.
    assert (await store.get(Observation, note.id)).revision == note.revision


async def test_terminal_butler_still_saves_core_changes(tmp_path):
    store, _, own_chat, _, terminal = await _surface(tmp_path, "terminal")
    saved = await terminal.invoke(
        {
            "operation": "nebula.work.update_chat",
            "arguments": {
                "session_id": own_chat.id,
                "expected_revision": own_chat.revision,
                "title": "Terminal rename",
            },
        },
        change=True,
    )
    assert saved["title"] == "Terminal rename"
    assert (await store.get(ChatSession, own_chat.id)).title == "Terminal rename"


@pytest.mark.parametrize("read_only", [True, False])
async def test_any_new_browser_handoff_is_refused_on_the_terminal(tmp_path, read_only):
    """No list of names: a future action that hands off is refused by its result."""

    _, project, _, browser, terminal = await _surface(tmp_path, "terminal")
    name = "nebula.future.browser_only"

    async def handoff(_arguments):
        return {
            "state": "pending_browser_apply",
            "client_action": {"kind": "future", "project_id": project.id},
        }

    async def saved(_arguments):
        return {"state": "core_records_saved", "read_back": True}

    for capabilities in (browser, terminal):
        for operation, handler in ((name, handoff), (name + "_saved", saved)):
            capabilities._actions[operation] = AnalysisTool(
                ToolSpec(
                    name=operation,
                    description="A future action.",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    risk_class=(
                        RiskClass.LOCAL_READ if read_only else RiskClass.WORKSPACE_WRITE
                    ),
                    idempotency=IdempotencyBehavior.SAFE,
                ),
                handler,
            )
    change = not read_only
    payload = {"operation": name, "arguments": {}}
    assert (await browser.invoke(payload, change=change))["state"] == (
        "pending_browser_apply"
    )
    with pytest.raises(CapabilityInputError) as refused:
        await terminal.invoke(payload, change=change)
    _browser_only(refused.value, name)
    assert (
        await terminal.invoke(
            {"operation": name + "_saved", "arguments": {}}, change=change
        )
    )["state"] == "core_records_saved"


async def test_client_surface_defaults_to_browser_and_rejects_unknown_values(
    tmp_path,
):
    request = {
        "provider_id": "provider",
        "messages": [{"role": "user", "content": "hello"}],
    }
    assert ChatCompletionRequest(**request).client_surface == "browser"
    terminal = ChatCompletionRequest(**request, client_surface="terminal")
    assert terminal.client_surface == "terminal"
    with pytest.raises(ValidationError):
        ChatCompletionRequest(**request, client_surface="kiosk")
    store, project, *_ = await _capabilities(tmp_path)
    with pytest.raises(ValueError, match="client surface"):
        await ButlerCapabilities.create(
            store,
            ArtifactStore(tmp_path / "artifacts"),
            project.id,
            client_surface="kiosk",
        )


class ThemeProvider(ResponseProvider):
    """A model that asks Butler to change the device theme, then answers."""

    def __init__(self, provider_id: str):
        super().__init__(provider_id)
        self.turns: list[tuple[ModelRequest, ModelRequest]] = []
        self._asked: ModelRequest | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        usage = ModelUsage(input_tokens=5, output_tokens=4, total_tokens=9)
        tools = {tool.name for tool in request.tools}
        if "nebula.capabilities.change" in tools and not request.tool_results:
            self._asked = request
            return ModelResponse(
                provider_id=self.config.id,
                model=request.model or "model-a",
                tool_calls=[
                    ToolCall(
                        id=f"theme-{len(self.requests)}",
                        name="nebula.capabilities.change",
                        arguments=_THEME,
                    )
                ],
                usage=usage,
                finish_reason="tool_calls",
            )
        if request.tool_results and self._asked is not None:
            self.turns.append((self._asked, request))
            self._asked = None
        return ModelResponse(
            provider_id=self.config.id,
            model=request.model or "model-a",
            text="Done.",
            usage=usage,
            finish_reason="stop",
        )


async def test_provider_butler_turn_carries_terminal_surface_end_to_end(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "core.sqlite3")
    project = await store.create(Engagement(name="Project A"))
    profile = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
            capability_verifications={
                "model-a": {"model": "model-a", "status": "verified"}
            },
        )
    )
    provider = ThemeProvider(profile.id)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    client = TestClient(
        create_app(
            store,
            auth_token="test-token",
            artifact_store=artifacts,
            tool_platform=runtime,
        )
    )
    headers = {"Authorization": "Bearer test-token"}

    async def turn(**surface):
        response = await client.post(
            "/api/v1/chat/completions",
            headers=headers,
            json={
                "engagement_id": project.id,
                "provider_id": profile.id,
                "model": "model-a",
                "control_assistant": True,
                "messages": [{"role": "user", "content": "Use the light theme."}],
                **surface,
            },
        )
        assert response.status_code == 200, response.text
        session_id = response.json()["session_id"]
        saved = await store.find_entities(ChatTurn, {"session_id": session_id})
        return saved[-1], *provider.turns[-1]

    terminal_turn, first, after_tool = await turn(client_surface="terminal")
    assert terminal_turn.request_snapshot["client_surface"] == "terminal"
    assert BUTLER_TERMINAL_SURFACE_INSTRUCTION in first.instructions
    tool_result = json.dumps(
        [item.model_dump(mode="json") for item in after_tool.tool_results]
    )
    assert "Nebula web app" in tool_result
    assert "pending_browser_apply" not in tool_result

    # A web client that omits the field keeps today's browser handoff.
    browser_turn, first, after_tool = await turn()
    assert browser_turn.request_snapshot["client_surface"] == "browser"
    assert BUTLER_TERMINAL_SURFACE_INSTRUCTION not in first.instructions
    tool_result = json.dumps(
        [item.model_dump(mode="json") for item in after_tool.tool_results]
    )
    assert "pending_browser_apply" in tool_result


async def test_harness_butler_turn_carries_terminal_surface(tmp_path):
    from tests.v3.test_harnesses import _runtime

    store, project, profile, _, _, runtime = await _runtime(tmp_path)

    async def prepare(**surface):
        _, _, harness_turn = await runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Use the light theme.",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            control_assistant=True,
            **surface,
        )
        return harness_turn

    terminal = await prepare(client_surface="terminal")
    assert terminal.metadata["client_surface"] == "terminal"
    assert BUTLER_TERMINAL_SURFACE_INSTRUCTION in terminal.prompt
    assert BUTLER_TERMINAL_SURFACE_INSTRUCTION not in json.dumps(
        terminal.metadata["standing_instructions"]
    )
    with pytest.raises(CapabilityInputError) as refused:
        await (await runtime._butler_capabilities(project.id, turn=terminal)).invoke(
            _THEME, change=True
        )
    _browser_only(refused.value, _THEME["operation"])

    browser = await prepare()
    assert browser.metadata["client_surface"] == "browser"
    assert BUTLER_TERMINAL_SURFACE_INSTRUCTION not in browser.prompt
    saved = await store.get(HarnessTurn, browser.id)
    handed_off = await (
        await runtime._butler_capabilities(project.id, turn=saved)
    ).invoke(_THEME, change=True)
    assert handed_off["state"] == "pending_browser_apply"
    await runtime.shutdown()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("nebula.settings.set_device_theme", {"theme": "light"}),
        ("nebula.navigate.open", {"destination_id": "projects"}),
    ],
)
async def test_harness_direct_settings_tools_refuse_terminal_handoffs(
    tmp_path, monkeypatch, name, arguments
):
    """The legacy direct names skip ButlerCapabilities.invoke; same refusal."""
    from nebula.v3.domain import HarnessSession
    from tests.v3.test_harnesses import _runtime

    store, project, profile, _, _, runtime = await _runtime(tmp_path)

    async def call(**surface):
        _, _, harness_turn = await runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Use the light theme.",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            control_assistant=True,
            **surface,
        )

        async def active(_session_id):
            return await store.get(HarnessTurn, harness_turn.id)

        monkeypatch.setattr(runtime, "_active_gateway_turn", active)
        session = await store.get(HarnessSession, harness_turn.harness_session_id)
        return await runtime._gateway_call_unwrapped(session, name, arguments)

    with pytest.raises(CapabilityInputError) as refused:
        await call(client_surface="terminal")
    _browser_only(refused.value, name)

    handed_off = await call()
    assert handed_off["structuredContent"]["state"] == "pending_browser_apply"
    await runtime.shutdown()
