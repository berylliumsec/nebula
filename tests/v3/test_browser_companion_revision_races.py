"""#1100: the browser panel's tab poll and Stop land inside companion writes.

The real ``_change_session`` tab-poll write saves the browser session
between a credential or file writer's read and its write; Stop assistant
control marks a running action stale while the browser performs it.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import base64

import pytest
from pydantic import SecretStr

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.browser_companion import (
    BrowserCompanion,
    CompanionCredentialCreate,
    CompanionFileCreate,
    CompanionRequest,
)
from nebula.v3.browser_engine import BrowserEngineRegistry
from nebula.v3.domain import BrowserSession, ChatTurn, ChatTurnStatus, CompanionAction
from tests.v3.revision_window import before_transaction, before_update
from tests.v3.test_browser_companion import setup

pytestmark = pytest.mark.anyio


def _tabs_poll(service, session_id, marker):
    async def write():
        await service._change_session(
            session_id, lambda s: {"metadata": {**s.metadata, "tabs_poll": marker}}
        )

    return write


async def test_saving_a_protected_value_survives_the_tab_poll(tmp_path):
    store, _, _, session, service = await setup(tmp_path)
    deleted = []
    original_delete = service.credentials.delete
    service.credentials.delete = lambda ref: (
        deleted.append(ref),
        original_delete(ref),
    )[1]
    window = before_update(
        store,
        "save_credential",
        _tabs_poll(service, session.id, 1),
        model=BrowserSession,
        direct=False,
    )
    catalog = await service.save_credential(
        session.id, CompanionCredentialCreate(label="Login", secret=SecretStr("pw-123"))
    )
    assert window.fired
    saved = await store.get(BrowserSession, session.id)
    assert [item["label"] for item in catalog] == ["Login"]
    assert len(saved.metadata["browser_credentials"]) == 1
    assert saved.metadata["tabs_poll"] == 1
    assert deleted == []
    reference = catalog[0]["reference"]
    # Removing it also survives the poll, and the secret is deleted once.
    window = before_update(
        store,
        "remove_credential",
        _tabs_poll(service, session.id, 2),
        model=BrowserSession,
        direct=False,
    )
    assert await service.remove_credential(session.id, reference) == []
    assert window.fired
    assert len(deleted) == 1
    saved = await store.get(BrowserSession, session.id)
    assert saved.metadata["browser_credentials"] == {}
    assert saved.metadata["tabs_poll"] == 2


async def test_duplicate_label_is_still_refused_and_rolled_back(tmp_path):
    store, _, _, session, service = await setup(tmp_path)
    deleted = []
    original_delete = service.credentials.delete
    service.credentials.delete = lambda ref: (
        deleted.append(ref),
        original_delete(ref),
    )[1]

    async def other_tab_saves_same_label():
        await service._change_session(
            session.id,
            lambda s: {
                "metadata": {
                    **s.metadata,
                    "browser_credentials": {
                        "other": {"label": "Login", "reference": "elsewhere"}
                    },
                }
            },
        )

    before_update(
        store,
        "save_credential",
        other_tab_saves_same_label,
        model=BrowserSession,
        direct=False,
    )
    with pytest.raises(ValueError, match="already exists"):
        await service.save_credential(
            session.id,
            CompanionCredentialCreate(label="login", secret=SecretStr("pw-123")),
        )
    assert len(deleted) == 1


async def test_attaching_and_removing_a_file_survive_the_tab_poll(tmp_path):
    store, _, _, session, _ = await setup(tmp_path)
    service = BrowserCompanion(
        store,
        BrowserEngineRegistry([]),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )
    window = before_transaction(store, "save_file", _tabs_poll(service, session.id, 1))
    catalog = await service.save_file(
        session.id,
        CompanionFileCreate(
            filename="sample.txt",
            media_type="text/plain",
            content_base64=base64.b64encode(b"file fixture").decode(),
        ),
    )
    assert window.fired
    assert [item["filename"] for item in catalog] == ["sample.txt"]
    saved = await store.get(BrowserSession, session.id)
    assert saved.metadata["tabs_poll"] == 1
    window = before_update(
        store,
        "remove_file",
        _tabs_poll(service, session.id, 2),
        model=BrowserSession,
        direct=False,
    )
    assert await service.remove_file(session.id, catalog[0]["reference"]) == []
    assert window.fired
    saved = await store.get(BrowserSession, session.id)
    assert saved.metadata["browser_files"] == {}
    assert saved.metadata["tabs_poll"] == 2


async def test_performed_action_keeps_its_result_after_stop(tmp_path, monkeypatch):
    store, project, _, session, service = await setup(tmp_path)
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id="chat",
            model="fixture",
            provider_profile_id="provider",
        )
    )
    action = await service.propose(
        session.id,
        CompanionRequest(operation="click", page_revision="old"),
        chat_turn_id=turn.id,
    )
    performed = []

    async def browser_operation(*args, **kwargs):
        performed.append(True)
        live = await store.get(ChatTurn, turn.id)
        # The operator stops the turn, then Stop assistant control.
        await store.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.CANCELLED},
            expected_revision=live.revision,
        )
        await service.invalidate_pending_actions(session.id)
        return {"text": "clicked"}

    monkeypatch.setattr(service, "request", browser_operation)
    result = await service.decide(session.id, action.id, "approve")
    saved = await store.get(CompanionAction, action.id)
    assert performed == [True]
    assert result.status == "complete"
    assert saved.status == "complete"
    assert saved.result == {"text": "clicked"}
