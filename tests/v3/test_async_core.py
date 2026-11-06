"""Real async Core entry points with synthetic data and inert providers."""

import asyncio
import sqlite3

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Engagement
from nebula.v3.storage import NebulaStore


@pytest.fixture(autouse=True)
def reject_synchronous_database_waits(monkeypatch):
    """Fail if an exercised runtime path uses a blocking driver on the loop."""

    def assert_off_loop():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise AssertionError("A synchronous database driver ran on the event loop")

    def before_execute(connection, *_args):
        if not connection.dialect.is_async:
            assert_off_loop()

    connect = sqlite3.connect

    def guarded_connect(*args, **kwargs):
        assert_off_loop()
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    event.listen(Engine, "before_cursor_execute", before_execute)
    try:
        yield
    finally:
        event.remove(Engine, "before_cursor_execute", before_execute)


def test_async_core_lifespan_authentication_and_listing(tmp_path):
    async def exercise():
        store = NebulaStore(tmp_path / "core.db")
        project = await store.create(Engagement(name="Synthetic Core project"))
        app = create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="synthetic-token",
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://synthetic.test"
            ) as client:
                assert (await client.get("/api/v1/engagements")).status_code == 401
                headers = {"Authorization": "Bearer synthetic-token"}
                result = await client.get("/api/v1/engagements", headers=headers)
                assert result.status_code == 200, result.text
                assert any(item["id"] == project.id for item in result.json())
                devices = await client.get("/api/v1/auth/devices", headers=headers)
                assert devices.status_code == 200, devices.text
                health = await client.get("/api/v1/health", headers=headers)
                assert health.status_code == 200, health.text
                performance = await client.get(
                    "/api/v1/diagnostics/performance", headers=headers
                )
                assert performance.status_code == 200
                assert performance.json()["database_pool"]["max_overflow"] == 0
        assert store.database.pool_snapshot()["checked_out"] == 0

    asyncio.run(exercise())


def test_async_core_provider_turn_persists_complete_history(tmp_path, monkeypatch):
    import nebula.v3.chat as chat_module
    from nebula.v3.domain import ProviderProfile, ChatMessage, ChatTurn, ChatTurnStatus
    from tests.v3.test_chat_api import ApiChatProvider

    async def exercise():
        store = NebulaStore(tmp_path / "turn.db")
        project = await store.create(Engagement(name="Inert provider turn"))
        profile = await store.create(
            ProviderProfile(
                name="Inert provider",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
                metadata={"default_model": "model-a"},
            )
        )
        monkeypatch.setattr(
            chat_module,
            "provider_from_profile",
            lambda selected: ApiChatProvider(selected.id),
        )
        app = create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="synthetic-token",
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://synthetic.test",
                timeout=10,
            ) as client:
                response = await client.post(
                    "/api/v1/chat/completions",
                    headers={"Authorization": "Bearer synthetic-token"},
                    json={
                        "engagement_id": project.id,
                        "provider_id": profile.id,
                        "model": "model-a",
                        "include_knowledge": False,
                        "stream": True,
                        "messages": [
                            {"role": "user", "content": "Return the fixture response."}
                        ],
                    },
                )
                assert response.status_code == 200, response.text
                import json

                frames = [
                    frame
                    for frame in response.text.split("\n\n")
                    if "event: done" in frame.splitlines()
                ]
                assert frames, response.text
                completed = json.loads(
                    next(
                        line[6:]
                        for line in frames[-1].splitlines()
                        if line.startswith("data: ")
                    )
                )
                assert completed["message"]["content"] == "API chat works."
                turns = await store.list_session_entities(
                    ChatTurn, completed["session_id"]
                )
                assert turns and all(
                    turn.status == ChatTurnStatus.COMPLETE for turn in turns
                )
                messages = await store.list_session_entities(
                    ChatMessage, completed["session_id"]
                )
                assert any(message.content == "API chat works." for message in messages)

    asyncio.run(exercise())


def test_settings_assistant_actions_await_reads_and_preserve_pending_apply(tmp_path):
    from nebula.v3.butler_notes import ButlerNotes
    from nebula.v3.domain import Observation
    from nebula.v3.sandbox import AnalysisOnlyRunner
    from nebula.v3.tools import ToolInvocation

    async def exercise():
        store = NebulaStore(tmp_path / "settings.db")
        try:
            project = await store.create(Engagement(name="Synthetic settings actions"))
            note = await store.create(
                Observation(
                    engagement_id=project.id,
                    observation_type="note",
                    title="Saved note",
                    body="Original",
                )
            )
            actions = {
                tool.spec.name: tool
                for tool in ButlerNotes(store, project.id).actions()
            }

            async def invoke(name, arguments):
                return await actions[name].execute(
                    ToolInvocation(
                        engagement_id=project.id,
                        run_id="fixture",
                        tool_name=name,
                        arguments=arguments,
                        workspace=tmp_path,
                    ),
                    AnalysisOnlyRunner(),
                )

            listed = await invoke("nebula.notes.list", {})
            assert listed.output["notes"][0]["note_id"] == note.id
            proposed = await invoke(
                "nebula.notes.update",
                {
                    "note_id": note.id,
                    "expected_revision": note.revision,
                    "title": "Proposed title",
                },
            )
            assert proposed.output["state"] == "pending_browser_apply"
            assert proposed.output["client_action"]["note_id"] == note.id
            assert (await store.get(Observation, note.id)).title == "Saved note"
        finally:
            await store.event_history.close()
            await store.database.dispose()

    asyncio.run(exercise())
