import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from nebula.v3.browser_companion import (
    BrowserCompanion,
    CompanionAction,
    CompanionRequest,
)
from nebula.v3.browser_companion_tools import companion_components, companion_spec
from nebula.v3.browser_engine import BrowserEngineRegistry
from nebula.v3.domain import BrowserIdentity, BrowserSession, ChatSession, Engagement
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import InvalidToolArguments


def setup(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(name="Browser test"))
    identity = store.create(BrowserIdentity(engagement_id=project.id, name="Browser"))
    session = store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=identity.id,
            name="Browser",
            metadata={"browser_companion_version": 1, "assistant_paused": False},
        )
    )
    service = BrowserCompanion(store, BrowserEngineRegistry([]))
    return store, project, identity, session, service


def test_binding_rejects_other_project_and_survives_store_reopen(tmp_path):
    store, project, _, session, service = setup(tmp_path)
    other = store.create(Engagement(name="Other"))
    chat = store.create(
        ChatSession(
            engagement_id=other.id,
            title="Other chat",
            model="fixture",
            provider_profile_id="provider",
        )
    )
    with pytest.raises(ValueError, match="another project"):
        service.bind(session.id, chat.id)
    own = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Own chat",
            model="fixture",
            provider_profile_id="provider",
        )
    )
    service.bind(session.id, own.id)
    reopened = NebulaStore(tmp_path / "nebula.db")
    assert (
        reopened.get(BrowserSession, session.id).metadata["conversation_id"] == own.id
    )


def test_revoked_identity_cannot_be_read_or_controlled(tmp_path):
    store, _, identity, session, service = setup(tmp_path)
    store.update(
        BrowserIdentity, identity.id, {"revoked_at": datetime.now(timezone.utc)}
    )
    with pytest.raises(ValueError, match="revoked"):
        service.session(session.id)


def test_takeover_revokes_pending_actions_across_service_instances(tmp_path):
    store, _, _, session, service = setup(tmp_path)
    second = BrowserCompanion(store, BrowserEngineRegistry([]))
    assert second._locks is service._locks
    action = service.propose(
        session.id,
        CompanionRequest(
            operation="click", tab_id="tab", page_revision="old", element_id="0"
        ),
    )
    second.takeover(session.id, True)
    assert store.get(CompanionAction, action.id).status == "revoked"
    with pytest.raises(ValueError, match="paused"):
        service.propose(session.id, action.request)


def test_expired_approval_does_not_execute(tmp_path, monkeypatch):
    store, _, _, session, service = setup(tmp_path)
    action = service.propose(
        session.id, CompanionRequest(operation="click", page_revision="old")
    )
    store.update(
        CompanionAction,
        action.id,
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("Expired approval executed")

    monkeypatch.setattr(service, "request", forbidden)
    assert (
        asyncio.run(service.decide(session.id, action.id, "approve")).status
        == "revoked"
    )


def test_decided_mutation_is_never_replayed(tmp_path, monkeypatch):
    _, _, _, session, service = setup(tmp_path)
    action = service.propose(
        session.id, CompanionRequest(operation="click", page_revision="old")
    )
    calls = []

    async def execute(*args, **kwargs):
        calls.append(args)
        return {"text": "changed"}

    monkeypatch.setattr(service, "request", execute)
    assert (
        asyncio.run(service.decide(session.id, action.id, "approve")).status
        == "complete"
    )
    with pytest.raises(ValueError, match="already been decided"):
        asyncio.run(service.decide(session.id, action.id, "approve"))
    assert len(calls) == 1


def test_companion_tool_has_no_script_or_security_testing_operations(tmp_path):
    spec = companion_spec()
    operations = spec.input_schema["properties"]["operation"]["enum"]
    assert set(operations) == {
        "tabs",
        "navigate",
        "capture",
        "click",
        "fill",
        "select",
        "press",
        "scroll",
        "highlight",
    }
    store, _, _, session, _ = setup(tmp_path)
    other = store.create(Engagement(name="Other"))
    with pytest.raises(InvalidToolArguments, match="another project"):
        companion_components(store, other.id, session.id)


def test_real_chromium_capture_redacts_fields_and_rejects_changed_document():
    from playwright.async_api import async_playwright
    from nebula.v3.browser_companion_runtime import capture, operate
    from types import SimpleNamespace

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content(
                    '<main><h1>Account</h1><input type="password" value="do-not-export"><textarea>private-draft</textarea><button>Save</button></main>'
                )
                request = CompanionRequest(operation="capture", tab_id="tab")
                result = await capture(page, request)
                assert "do-not-export" not in result["text"]
                assert "private-draft" not in result["text"]
                assert result["elements"][0]["sensitive"] is True
                await page.locator("button").evaluate("el => el.textContent = 'Delete'")

                class Manager:
                    def __init__(self):
                        self._tabs = {("identity", "tab"): page}

                    async def ensure_identity(self, identity_id):
                        return SimpleNamespace(
                            tab_ids=[
                                key[1] for key in self._tabs if key[0] == identity_id
                            ]
                        )

                    async def page_for_screencast(self, identity_id, tab_id):
                        return self._tabs[(identity_id, tab_id)]

                manager = Manager()
                created = await operate(
                    manager, "identity", CompanionRequest(operation="new_tab")
                )
                assert len(created["tabs"]) == 2
                closed = await operate(
                    manager,
                    "identity",
                    CompanionRequest(
                        operation="close_tab", tab_id=created["active_tab_id"]
                    ),
                )
                assert len(closed["tabs"]) == 1
                with pytest.raises(ValueError, match="Page content changed"):
                    await operate(
                        manager,
                        "identity",
                        CompanionRequest(
                            operation="click",
                            tab_id="tab",
                            element_id="2",
                            page_revision=result["page_revision"],
                        ),
                    )
            finally:
                await browser.close()

    asyncio.run(run())


def test_real_core_requires_auth_and_reports_absent_browser_runtime(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from nebula.v3.api import create_app

    monkeypatch.delenv("NEBULA_BROWSERD_URL", raising=False)
    monkeypatch.delenv("NEBULA_BROWSERD_TOKEN", raising=False)
    store = NebulaStore(tmp_path / "core.db")
    client = TestClient(create_app(store, auth_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    project = client.post(
        "/api/v1/engagements", headers=headers, json={"name": "Browser"}
    ).json()
    endpoint = f"/api/v1/engagements/{project['id']}/browser-companion"
    assert client.post(endpoint).status_code == 401
    result = client.post(endpoint, headers=headers)
    assert result.status_code == 409
    assert "saved conversations remain available" in result.text


def test_assistant_waits_for_inline_approval_and_receives_actual_result(
    tmp_path, monkeypatch
):
    from nebula.v3.browser_companion_tools import CompanionBroker
    from nebula.v3.domain import ScopePolicy
    from nebula.v3.tools import ToolInvocation

    store, project, _, session, service = setup(tmp_path)
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Browser",
            model="fixture",
            provider_profile_id="provider",
        )
    )
    service.bind(session.id, chat.id)
    from nebula.v3.domain import ChatTurn, ToolCallOrigin

    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            model="fixture",
            provider_profile_id="provider",
            tools_enabled=True,
        )
    )
    broker = CompanionBroker(store, session.id)
    operations = []

    async def request(session_id, request, **kwargs):
        operations.append(request.operation)
        return {
            "page_revision": "page-1",
            "elements": [{"id": "0", "sensitive": False}],
            "text": "Saved",
        }

    monkeypatch.setattr(broker.service, "request", request)
    invocation = ToolInvocation(
        engagement_id=project.id,
        run_id=turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=chat.id,
        tool_name="browser.companion",
        arguments={
            "operation": "click",
            "tab_id": "tab",
            "page_revision": "page-1",
            "element_id": "0",
            "url": "https://example.test/",
        },
        workspace=tmp_path,
    )

    async def run():
        task = asyncio.create_task(
            broker.execute(invocation, ScopePolicy(engagement_id=project.id))
        )
        try:
            for _ in range(100):
                if task.done():
                    await task
                actions = broker.service.actions(session.id)
                if actions:
                    break
                await asyncio.sleep(0.01)
            assert actions and not task.done()
            assert operations == ["capture"]
            await broker.service.decide(session.id, actions[0].id, "approve")
            result = await asyncio.wait_for(task, 2)
            assert result.output["status"] == "complete"
            assert result.output["result"]["text"] == "Saved"
            assert operations == ["capture", "click"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_model_results_remove_url_credentials_and_tokens():
    from nebula.v3.browser_companion_tools import model_browser_result

    result = model_browser_result(
        {
            "tabs": [
                {
                    "url": "https://user:secret@example.test:443/page?token=private#secret"
                }
            ]
        }
    )
    assert result["tabs"][0]["url"] == "https://example.test:443/page"
