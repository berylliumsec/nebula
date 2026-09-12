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
        "upload",
        "select",
        "press",
        "scroll",
        "highlight",
    }
    store, _, _, session, _ = setup(tmp_path)
    other = store.create(Engagement(name="Other"))
    with pytest.raises(InvalidToolArguments, match="another project"):
        companion_components(store, other.id, session.id)


def test_real_chromium_capture_retains_fields_and_rejects_changed_document():
    from playwright.async_api import async_playwright
    from nebula.v3.browser_companion_runtime import capture, operate
    from types import SimpleNamespace

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, executable_path=playwright.chromium.executable_path
            )
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content(
                    '<main><h1>Account</h1><input type="password" value="do-not-export"><textarea>private-draft</textarea><button>Save</button></main>'
                )
                request = CompanionRequest(operation="capture", tab_id="tab")
                result = await capture(page, request)
                assert result["elements"][0]["value"] == "do-not-export"
                assert result["elements"][1]["value"] == "private-draft"
                assert "private-draft" in result["html"]
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
                before_replacement = await capture(page, request)
                await page.locator("button").evaluate(
                    "el => el.replaceWith(el.cloneNode(true))"
                )
                after_replacement = await capture(page, request)
                assert before_replacement["text"] == after_replacement["text"]
                assert (
                    before_replacement["page_revision"]
                    != after_replacement["page_revision"]
                )
                with pytest.raises(ValueError, match="Page content changed"):
                    await operate(
                        manager,
                        "identity",
                        CompanionRequest(
                            operation="click",
                            tab_id="tab",
                            element_id=before_replacement["elements"][-1]["id"],
                            page_revision=before_replacement["page_revision"],
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
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
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


@pytest.mark.parametrize("cancel", [False, True, "saved-turn", "late-approval"])
def test_assistant_waits_for_inline_approval_and_receives_actual_result(
    tmp_path, monkeypatch, cancel
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
        chat_turn_id=turn.id,
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
            if cancel in {"saved-turn", "late-approval"}:
                from nebula.v3.domain import ChatTurnStatus

                store.update(
                    ChatTurn,
                    turn.id,
                    {"status": ChatTurnStatus.CANCELLED},
                    expected_revision=turn.revision,
                )
                if cancel == "late-approval":
                    action = await broker.service.decide(
                        session.id, actions[0].id, "approve"
                    )
                    assert action.status == "revoked"
                result = await asyncio.wait_for(task, 2)
                assert result.output["status"] == "revoked"
                assert operations == ["capture"]
                return
            if cancel:
                from nebula.v3.domain import (
                    ToolCall as PersistedToolCall,
                    ToolCallStatus,
                )

                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert broker.service.actions(session.id)[0].status == "revoked"
                assert (
                    store.get(PersistedToolCall, invocation.id).status
                    == ToolCallStatus.FAILED
                )
                assert operations == ["capture"]
                return
            await broker.service.decide(session.id, actions[0].id, "approve")
            result = await asyncio.wait_for(task, 2)
            assert result.output["status"] == "complete"
            assert result.output["result"]["text"] == "Saved"
            assert operations == ["capture", "click"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_project_never_policy_executes_scoped_companion_change_without_prompt(
    tmp_path, monkeypatch
):
    from nebula.v3.browser_companion_tools import CompanionBroker
    from nebula.v3.domain import (
        AutomationProjectPolicy,
        ChatTurn,
        ScopePolicy,
        ToolCallOrigin,
    )
    from nebula.v3.tools import ToolInvocation

    store, project, _, session, service = setup(tmp_path)
    store.create(
        AutomationProjectPolicy(engagement_id=project.id, approval_policy="never")
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Browser",
            model="fixture",
            provider_profile_id="provider",
        )
    )
    service.bind(session.id, chat.id)
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
        chat_turn_id=turn.id,
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

    result = asyncio.run(
        broker.execute(invocation, ScopePolicy(engagement_id=project.id))
    )
    assert result.output["status"] == "complete"
    assert operations == ["capture", "click"]
    assert service.actions(session.id)[0].status == "complete"


def test_manual_action_revokes_approvals_without_clearing_control_grant(tmp_path):
    store, _, _, session, service = setup(tmp_path)
    pending = service.propose(
        session.id,
        CompanionRequest(
            operation="click",
            tab_id="tab",
            page_revision="page-1",
            element_id="0",
        ),
    )

    async def run():
        lock = service._locks.setdefault(session.id, asyncio.Lock())
        async with lock:
            task = asyncio.create_task(
                service.request(
                    session.id,
                    CompanionRequest(
                        operation="navigate",
                        tab_id="tab",
                        url="https://example.test/",
                    ),
                )
            )
            await asyncio.sleep(0)
            assert not task.done()
            assert service.session(session.id).metadata["assistant_paused"] is False
            assert store.get(CompanionAction, pending.id).status == "revoked"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_cancelled_turn_cannot_dispatch_an_approved_action_waiting_for_control(
    tmp_path,
):
    from nebula.v3.domain import ChatTurn, ChatTurnStatus

    store, project, _, session, service = setup(tmp_path)
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id="chat",
            model="fixture",
            provider_profile_id="provider",
        )
    )
    action = service.propose(
        session.id,
        CompanionRequest(
            operation="click", tab_id="tab", page_revision="page-1", element_id="0"
        ),
        chat_turn_id=turn.id,
    )

    async def run():
        lock = service._locks.setdefault(session.id, asyncio.Lock())
        async with lock:
            task = asyncio.create_task(service.decide(session.id, action.id, "approve"))
            await asyncio.sleep(0)
            assert store.get(CompanionAction, action.id).status == "running"
            store.update(
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.CANCELLED},
                expected_revision=turn.revision,
            )
        with pytest.raises(ValueError, match="originating Assistant turn ended"):
            await task
        assert store.get(CompanionAction, action.id).status == "failed"

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


def test_screenshot_tool_persists_owned_image_and_replays_without_recapture(
    tmp_path, monkeypatch
):
    import base64
    from types import SimpleNamespace
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.browser_companion_tools import CompanionBroker
    from nebula.v3.chat import ChatService
    from nebula.v3.domain import ChatTurn, ToolCallOrigin, ScopePolicy
    from nebula.v3.providers import ModelRequest
    from nebula.v3.tools import ToolInvocation

    store, project, _, session, service = setup(tmp_path)
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Images",
            model="fixture",
            provider_profile_id="fixture",
        )
    )
    service.bind(session.id, chat.id)
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            model="fixture",
            provider_profile_id="fixture",
            tools_enabled=True,
        )
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    broker = CompanionBroker(
        store, session.id, artifact_store=artifacts, image_supported=True
    )
    image_data = base64.b64encode(b"controlled image bytes").decode()
    captures = []

    async def request(*args, **kwargs):
        captures.append(args)
        return {"image": image_data, "text": "Page", "page_revision": "1"}

    monkeypatch.setattr(broker.service, "request", request)
    invocation = ToolInvocation(
        engagement_id=project.id,
        run_id=turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=chat.id,
        tool_name="browser.companion",
        arguments={
            "operation": "capture",
            "capture_kind": "region",
            "tab_id": "tab",
            "url": "https://example.test/",
            "width": 100,
            "height": 100,
        },
        workspace=tmp_path,
    )

    async def run():
        result = await broker.execute(invocation, ScopePolicy(engagement_id=project.id))
        assert "image" not in result.output
        assert result.mcp_content_blocks[0]["data"] == image_data
        repeated = await broker.execute(
            invocation, ScopePolicy(engagement_id=project.id)
        )
        assert repeated.output == result.output
        assert len(captures) == 1
        turn.tool_history = [
            {
                "name": "browser.companion",
                "status": "complete",
                "tool_call_id": invocation.id,
                "artifacts": result.output["artifacts"],
            }
        ]
        prepared = SimpleNamespace(
            model_request=ModelRequest(model="fixture", messages=[]),
            provider_profile=SimpleNamespace(capabilities=SimpleNamespace(vision=True)),
        )
        owner = SimpleNamespace(store=store, artifact_store=artifacts)
        messages = ChatService._browser_screenshot_messages(owner, prepared, turn)
        assert messages[-1].content[1]["data"] == image_data
        prepared.provider_profile.capabilities.vision = False
        assert ChatService._browser_screenshot_messages(owner, prepared, turn) == []

    asyncio.run(run())
    assert (
        "region"
        not in companion_spec().input_schema["properties"]["capture_kind"]["enum"]
    )


def test_harness_image_capability_follows_discovered_model_modalities():
    from nebula.v3.harnesses import CodexAppServerAdapter

    class Catalog:
        async def request(self, method, params):
            assert method == "model/list"
            return {
                "data": [
                    {"model": "vision", "inputModalities": ["text", "image"]},
                    {"model": "text-only", "inputModalities": ["text"]},
                    {"model": "unknown"},
                ]
            }

    _, models = asyncio.run(CodexAppServerAdapter()._models(Catalog(), timeout=1))
    assert {model.model: model.image_input for model in models} == {
        "vision": True,
        "text-only": False,
        "unknown": False,
    }


def test_protected_reference_fills_only_after_approval_and_retains_capture(
    tmp_path, monkeypatch
):
    import json
    from pydantic import SecretStr
    from nebula.v3.browser_companion import CompanionCredentialCreate
    from nebula.v3.credentials import CredentialStore

    store, _, _, session, service = setup(tmp_path)
    secret = "private-browser-value-123"
    catalog = service.save_credential(
        session.id,
        CompanionCredentialCreate(label="Test password", secret=SecretStr(secret)),
    )
    reference = catalog[0]["reference"]
    assert secret not in json.dumps(catalog)
    assert secret not in service.session(session.id).model_dump_json()
    action = service.propose(
        session.id,
        CompanionRequest(
            operation="fill",
            tab_id="tab",
            page_revision="revision",
            element_id="0",
            credential_ref=reference,
        ),
    )
    assert secret not in action.model_dump_json()
    payloads = []

    class Response:
        is_error = False

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class Adapter:
        async def _request(self, method, path, payload):
            if payload["operation"] == "tabs":
                return Response(
                    {
                        "tabs": [
                            {
                                "id": "tab",
                                "url": "https://example.test/",
                                "title": "Test",
                            }
                        ]
                    }
                )
            payloads.append(payload)
            assert payload["text"] == secret
            return Response(
                {
                    "text": secret,
                    "title": secret,
                    "page_revision": "revision",
                    "elements": [],
                }
            )

    async def adapter():
        return Adapter()

    monkeypatch.setattr(service, "adapter", adapter)
    monkeypatch.setattr(service.security, "_scope", lambda _: None)
    monkeypatch.setattr(service.security, "_require_in_scope", lambda *args: None)
    assert payloads == []
    result = asyncio.run(service.decide(session.id, action.id, "approve"))
    assert result.status == "complete" and len(payloads) == 1
    assert result.result["text"] == secret
    with pytest.raises(ValueError):
        asyncio.run(
            service.request(
                session.id,
                CompanionRequest(
                    operation="fill",
                    tab_id="tab",
                    page_revision="revision",
                    element_id="0",
                    credential_ref="env:UNATTACHED",
                ),
            )
        )
    assert len(payloads) == 1
    restarted = BrowserCompanion(
        store, BrowserEngineRegistry([]), credentials=CredentialStore()
    )
    assert restarted.credential_catalog(session.id)[0]["available"] is False


def test_attached_files_are_scoped_revocable_and_integrity_checked(tmp_path):
    import base64
    from pydantic import ValidationError
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.browser_companion import CompanionFileCreate
    from nebula.v3.domain import Artifact

    store, project, identity, session, _ = setup(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    service = BrowserCompanion(
        store, BrowserEngineRegistry([]), artifact_store=artifacts
    )
    with pytest.raises(ValidationError):
        CompanionFileCreate(filename="../secret.txt", content_base64="")
    catalog = service.save_file(
        session.id,
        CompanionFileCreate(
            filename="sample.txt",
            media_type="text/plain",
            content_base64=base64.b64encode(b"file fixture").decode(),
        ),
    )
    reference = catalog[0]["reference"]
    assert catalog[0]["size"] == 12
    assert "content_base64" not in str(catalog)
    assert (
        service.file_payload(session.id, reference)["content_base64"]
        == "ZmlsZSBmaXh0dXJl"
    )
    other = store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=identity.id,
            name="Other browser",
            metadata={"browser_companion_version": 1},
        )
    )
    with pytest.raises(ValueError, match="attached"):
        service.file_payload(other.id, reference)
    request = CompanionRequest(
        operation="upload",
        file_ref=reference,
        tab_id="tab",
        page_revision="page",
        element_id="2",
    )
    with pytest.raises(ValueError, match="inline approval"):
        asyncio.run(service.request(session.id, request))
    action = service.propose(session.id, request)
    service.remove_file(session.id, reference)
    assert store.get(CompanionAction, action.id).status == "revoked"
    with pytest.raises(ValueError, match="attached"):
        service.file_payload(session.id, reference)
    catalog = service.save_file(
        session.id,
        CompanionFileCreate(filename="sample.txt", content_base64="ZmlsZSBmaXh0dXJl"),
    )
    reference = catalog[0]["reference"]
    operator_action = service.propose(
        session.id,
        request.model_copy(update={"file_ref": reference}),
        operator_requested=True,
    )
    assert operator_action.operator_requested
    entry = service.session(session.id).metadata["browser_files"][reference]
    artifact = store.get(Artifact, entry["artifact_id"])
    path = artifacts.path_for(artifact)
    path.chmod(0o600)
    path.write_bytes(b"changed data")
    with pytest.raises(ValueError, match="integrity"):
        service.file_payload(session.id, reference)


def test_background_tab_poll_preserves_lost_state_until_navigation(
    tmp_path, monkeypatch
):
    import httpx

    store, project, _, session, service = setup(tmp_path)
    store.update(
        BrowserSession,
        session.id,
        {
            "tabs": [{"id": "old-tab", "title": "Unsaved page", "position": 0}],
            "active_tab_id": "old-tab",
        },
    )
    pending = service.propose(
        session.id,
        CompanionRequest(operation="click", tab_id="old-tab", page_revision="old"),
    )

    class Adapter:
        async def ensure_identity(self, identity_id):
            return None

        async def _request(self, method, path, payload):
            return httpx.Response(
                200,
                request=httpx.Request(method, "http://fixture.test"),
                json={
                    "tabs": [
                        {"id": "new-tab", "title": "New page", "url": "about:blank"}
                    ]
                }
                if payload["operation"] == "tabs"
                else {"text": "Fresh page"},
            )

    async def adapter():
        return Adapter()

    monkeypatch.setattr(service, "adapter", adapter)
    monkeypatch.setattr(service.security, "_scope", lambda _: None)
    monkeypatch.setattr(service.security, "_require_in_scope", lambda *args: None)

    async def run():
        await service.request(session.id, CompanionRequest(operation="tabs"))
        saved = NebulaStore(tmp_path / "nebula.db").get(BrowserSession, session.id)
        assert saved.metadata["browser_page_state_reset"] is True
        assert saved.metadata["assistant_paused"] is True
        assert store.get(CompanionAction, pending.id).status == "revoked"
        assert (await service.open(project.id))["page_state_reset"] is True
        assert (await service.open(project.id))["page_state_reset"] is True
        await service.request(
            session.id,
            CompanionRequest(
                operation="navigate", tab_id="new-tab", url="https://example.test/"
            ),
        )
        assert (await service.open(project.id))["page_state_reset"] is False

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["transport", "timeout", "page_timeout"])
def test_host_failure_explains_recovery_without_replaying(
    tmp_path, monkeypatch, failure
):
    import httpx

    _, _, _, session, service = setup(tmp_path)
    calls = []

    class Adapter:
        async def _request(self, *args):
            calls.append(args)
            if failure == "transport":
                raise httpx.ConnectError("private endpoint")
            if failure == "timeout":
                raise httpx.ReadTimeout("private endpoint")
            return httpx.Response(504, json={"detail": "private upstream details"})

    async def adapter():
        return Adapter()

    monkeypatch.setattr(service, "adapter", adapter)
    with pytest.raises(ValueError, match="[Rr]econnect|[Rr]etry") as error:
        asyncio.run(service.request(session.id, CompanionRequest(operation="tabs")))
    assert len(calls) == 1
    assert "private" not in str(error.value)


def test_attached_browser_has_project_graph_capabilities(tmp_path):
    from nebula.v3.domain import ScopePolicy
    from nebula.v3.application_model.tools import INPUTS

    store, project, _, session, _ = setup(tmp_path)
    scope = store.create(ScopePolicy(engagement_id=project.id))
    store.update(
        Engagement,
        project.id,
        {"scope_policy_id": scope.id},
        expected_revision=project.revision,
    )
    runtime = companion_components(store, project.id, session.id)
    assert set(runtime.specs) == {"browser.companion", *INPUTS}
    assert "application-model-v2" in runtime.runtime_digest


@pytest.mark.parametrize(
    "operation,assistant,expected",
    [
        ("navigate", True, True),
        ("capture", True, True),
        ("click", True, True),
        ("tabs", True, False),
        ("navigate", False, False),
    ],
)
def test_agent_browser_result_carries_durable_model_evidence(
    tmp_path, operation, assistant, expected
):
    from nebula.v3.domain import Observation
    from nebula.v3.application_model.service import ApplicationModelService
    from nebula.v3.application_model.graph import GraphTransaction

    store, project, identity, session, service = setup(tmp_path)
    result = {
        "url": "https://example.test/",
        "title": "Example page",
        "page_revision": "page-1",
    }
    service._record_interaction(
        session,
        CompanionRequest(operation=operation),
        result,
        assistant=assistant,
        chat_turn_id=None,
    )
    assert ("model_evidence" in result) is expected
    if not expected:
        return
    reference = result["model_evidence"]
    assert result["model_authentication_context"] == identity.id
    reopened = NebulaStore(tmp_path / "nebula.db")
    observation = reopened.get(Observation, reference["id"])
    assert observation.engagement_id == project.id
    assert observation.revision == reference["revision"]
    model = ApplicationModelService(reopened)
    transaction = GraphTransaction.model_validate(
        {
            "expected_revision": 0,
            "idempotency_key": "browser-observation-1",
            "operations": [
                {
                    "op": "put_object",
                    "id": "page-example",
                    "label": "Example page",
                    "authentication_context": result["model_authentication_context"],
                    "classification": {
                        "value": "Page",
                        "status": "observed",
                        "evidence": [reference],
                    },
                    "properties": {
                        "purpose": {
                            "value": "Identify the entry point into the captured application workflow."
                        }
                    },
                }
            ],
        }
    )
    model.transact(project.id, transaction, producer="assistant")
    model.transact(project.id, transaction, producer="assistant")
    assert len(model.search(project.id)["objects"]) == 1


def test_automatic_browser_model_workflow_reaches_provider_and_harness():
    from types import SimpleNamespace
    from nebula.v3.application_model.workflow import BROWSER_MODEL_WORKFLOW
    from nebula.v3.chat import _CHAT_TOOL_INSTRUCTIONS
    from nebula.v3.harnesses import _harness_developer_instructions
    from nebula.v3.domain import HarnessNativeCapabilities

    assert BROWSER_MODEL_WORKFLOW in _CHAT_TOOL_INSTRUCTIONS
    assert BROWSER_MODEL_WORKFLOW in companion_spec().description
    assert "Use browser.companion" in BROWSER_MODEL_WORKFLOW
    assert "model.transact" in BROWSER_MODEL_WORKFLOW
    instructions = _harness_developer_instructions(
        SimpleNamespace(metadata={}, mcp_snapshot=[]),
        HarnessNativeCapabilities(),
        vendor="test",
    )
    assert BROWSER_MODEL_WORKFLOW in instructions
