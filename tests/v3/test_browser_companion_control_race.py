"""An operator's Stop of assistant browser control never loses a write race.

The Assistant browser viewer polls its tab list every three seconds and a
reconnect reopens the session. Both used to rewrite the ``BrowserSession`` row
that Stop updates, so Stop could answer ``409 revision conflict`` and leave the
Assistant in control with its approvals still pending.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from nebula.v3.browser_companion import (
    BrowserCompanion,
    CompanionAction,
    CompanionRequest,
)
from nebula.v3.browser_engine import BrowserEngineRegistry
from nebula.v3.domain import BrowserIdentity, BrowserSession, Engagement
from nebula.v3.storage import ConflictError, NebulaStore


class LiveTabs:
    """A host browser whose only tab can be retitled between viewer polls."""

    def __init__(self) -> None:
        self.title = "Page"
        self.polls = 0

    async def ensure_identity(self, identity_id):
        return None

    async def _request(self, method, path, payload):
        assert payload["operation"] == "tabs"
        self.polls += 1
        return httpx.Response(
            200,
            request=httpx.Request(method, "http://fixture.test" + path),
            json={
                "tabs": [
                    {"id": "tab", "title": self.title, "url": "https://example.test/"}
                ]
            },
        )


async def setup(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "nebula.db")
    project = await store.create(Engagement(name="Browser control"))
    identity = await store.create(
        BrowserIdentity(engagement_id=project.id, name="Browser")
    )
    session = await store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=identity.id,
            name="Browser",
            metadata={"browser_companion_version": 1, "assistant_paused": False},
        )
    )
    service = BrowserCompanion(store, BrowserEngineRegistry([]))
    browser = LiveTabs()

    async def adapter():
        return browser

    monkeypatch.setattr(service, "adapter", adapter)
    monkeypatch.setattr(service.security, "_scope", AsyncMock(return_value=None))
    monkeypatch.setattr(service.security, "_require_in_scope", lambda *args: None)
    # The viewer has already recorded the open tab, as it does when it connects.
    await service.request(session.id, CompanionRequest(operation="tabs"))
    return store, project, session, service, browser


def poll(service, session_id):
    return service.request(session_id, CompanionRequest(operation="tabs"))


async def pending_click(service, session_id):
    return await service.propose(
        session_id,
        CompanionRequest(
            operation="click", tab_id="tab", page_revision="page-1", element_id="0"
        ),
    )


def write_after(monkeypatch, store, *, when, first):
    """Commit ``first`` just before the next session write that ``when`` matches.

    This is the interleaving that loses on an optimistic write: the racing
    writer has read the row, and another writer commits before it does.
    """

    update = store.update
    raced = []

    async def racing_update(model, entity_id, changes, **options):
        if model is BrowserSession and not raced and when(changes):
            raced.append(True)
            await first()
        return await update(model, entity_id, changes, **options)

    monkeypatch.setattr(store, "update", racing_update)
    return raced


async def test_stop_wins_a_viewer_poll_that_rewrites_the_session_first(
    tmp_path, monkeypatch
):
    store, _, session, service, browser = await setup(tmp_path, monkeypatch)
    pending = await pending_click(service, session.id)

    async def viewer_poll_records_a_new_title():
        browser.title = "Page finished loading"
        await poll(service, session.id)

    raced = write_after(
        monkeypatch,
        store,
        when=lambda changes: (
            changes.get("metadata", {}).get("assistant_paused") is True
        ),
        first=viewer_poll_records_a_new_title,
    )

    await service.takeover(session.id, True)

    assert raced, "Stop wrote the session without meeting the racing poll"
    saved = await store.get(BrowserSession, session.id)
    assert saved.metadata["assistant_paused"] is True
    assert [tab.title for tab in saved.tabs] == ["Page finished loading"]
    assert (await store.get(CompanionAction, pending.id)).status == "revoked"
    with pytest.raises(ValueError, match="paused"):
        await service.propose(session.id, pending.request)


async def test_resume_wins_a_viewer_poll_that_rewrites_the_session_first(
    tmp_path, monkeypatch
):
    store, _, session, service, browser = await setup(tmp_path, monkeypatch)
    await service.takeover(session.id, True)

    async def viewer_poll_records_a_new_title():
        browser.title = "Page finished loading"
        await poll(service, session.id)

    raced = write_after(
        monkeypatch,
        store,
        when=lambda changes: (
            changes.get("metadata", {}).get("assistant_paused") is False
        ),
        first=viewer_poll_records_a_new_title,
    )

    await service.takeover(session.id, False)

    assert raced
    assert (await store.get(BrowserSession, session.id)).metadata[
        "assistant_paused"
    ] is False


async def test_viewer_poll_keeps_a_stop_that_landed_while_it_read_the_tabs(
    tmp_path, monkeypatch
):
    store, _, session, service, browser = await setup(tmp_path, monkeypatch)
    pending = await pending_click(service, session.id)
    browser.title = "Page finished loading"

    raced = write_after(
        monkeypatch,
        store,
        when=lambda changes: "tabs" in changes,
        first=lambda: service.takeover(session.id, True),
    )

    result = await poll(service, session.id)

    assert raced, "the poll wrote the session without meeting the racing Stop"
    assert [tab["title"] for tab in result["tabs"]] == ["Page finished loading"]
    saved = await store.get(BrowserSession, session.id)
    assert saved.metadata["assistant_paused"] is True
    assert [tab.title for tab in saved.tabs] == ["Page finished loading"]
    assert (await store.get(CompanionAction, pending.id)).status == "revoked"


async def test_viewer_poll_leaves_an_unchanged_session_row_alone(tmp_path, monkeypatch):
    store, _, session, service, browser = await setup(tmp_path, monkeypatch)
    before = await store.get(BrowserSession, session.id)

    for _ in range(5):
        await poll(service, session.id)

    after = await store.get(BrowserSession, session.id)
    assert browser.polls == 6
    assert after.revision == before.revision
    assert after.updated_at == before.updated_at

    browser.title = "Page finished loading"
    await poll(service, session.id)
    changed = await store.get(BrowserSession, session.id)
    assert changed.revision == before.revision + 1
    assert [tab.title for tab in changed.tabs] == ["Page finished loading"]


async def test_stop_reports_a_session_that_never_stops_changing(tmp_path, monkeypatch):
    """A Stop that cannot be saved fails loudly; it never reports success."""

    store, _, session, service, browser = await setup(tmp_path, monkeypatch)
    pending = await pending_click(service, session.id)
    update = store.update
    writes = 0

    async def always_raced(model, entity_id, changes, **options):
        nonlocal writes
        if (
            model is BrowserSession
            and changes.get("metadata", {}).get("assistant_paused") is True
        ):
            writes += 1
            browser.title = f"Retitled {writes}"
            await poll(service, session.id)
        return await update(model, entity_id, changes, **options)

    monkeypatch.setattr(store, "update", always_raced)

    with pytest.raises(ConflictError):
        await service.takeover(session.id, True)

    assert writes > 1, "Stop gave up without trying again"
    assert (await store.get(BrowserSession, session.id)).metadata[
        "assistant_paused"
    ] is False
    assert (await store.get(CompanionAction, pending.id)).status == "pending"


async def test_core_stop_request_wins_concurrent_viewer_polls_and_reconnects(
    tmp_path, monkeypatch
):
    """The operator journey over HTTP: Stop racing the viewer's own requests."""

    from tests.v3.async_test_support import AsyncTestClient
    from nebula.v3.api import create_app

    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token")
    client = AsyncTestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    project = (
        await client.post(
            "/api/v1/engagements", headers=headers, json={"name": "Browser"}
        )
    ).json()
    service = app.state.browser_companion
    browser = LiveTabs()

    async def adapter():
        return browser

    monkeypatch.setattr(service, "adapter", adapter)
    opened = await client.post(
        f"/api/v1/engagements/{project['id']}/browser-companion",
        headers=headers,
        params={"draft_id": "draft-viewer"},
    )
    assert opened.status_code == 200, opened.text
    base = f"/api/v1/browser-companion/{opened.json()['session_id']}"

    async def viewer(rounds):
        # What the open viewer sends: its tab poll, and a reconnect.
        statuses = []
        for index in range(rounds):
            browser.title = f"Page {index}"
            statuses.append(
                (
                    await client.post(
                        base + "/operations",
                        headers=headers,
                        json={"operation": "tabs"},
                    )
                ).status_code
            )
            statuses.append(
                (
                    await client.post(
                        f"/api/v1/engagements/{project['id']}/browser-companion",
                        headers=headers,
                        params={"draft_id": "draft-viewer"},
                    )
                ).status_code
            )
        return statuses

    for round_number in range(12):
        resumed = await client.put(
            base + "/control", headers=headers, params={"paused": "false"}
        )
        assert resumed.status_code == 200, resumed.text
        pending = await service.propose(
            opened.json()["session_id"],
            CompanionRequest(
                operation="click",
                tab_id="tab",
                page_revision="page-1",
                element_id="0",
            ),
        )
        polling = asyncio.create_task(viewer(3))
        # Land the Stop at a different point of the viewer's requests each round.
        await asyncio.sleep(0.002 * round_number)
        stopped = await client.put(
            base + "/control", headers=headers, params={"paused": "true"}
        )
        statuses = await polling
        assert stopped.status_code == 200, (round_number, stopped.text)
        assert set(statuses) == {200}, (round_number, statuses)
        control = await client.get(base + "/control", headers=headers)
        assert control.json()["paused"] is True, round_number
        assert (await store.get(CompanionAction, pending.id)).status == "revoked"
