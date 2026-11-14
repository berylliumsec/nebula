"""Restoring a project's only chat readmits its retired worker by itself."""

import asyncio
import ipaddress
import socket

import pytest
from sqlalchemy import select

from nebula.v3.database import (
    DeploymentReceiptRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
)
from nebula.v3.domain import ChatSession, ChatTurn
from nebula.v3.knowledge_index import _default_model_ready
from tests.v3.runtime_continuity_fixture import completed_response
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_shutdown_readmission import core as core


def _host_name_resolves_to_lan() -> bool:
    # The shared Core fixture binds to this host name's first non-loopback
    # IPv4 address and asserts that one exists.
    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        # diagnostic-expected: an unresolvable host name means no LAN address for the fixture.
        return False
    return any(not ipaddress.ip_address(item[4][0]).is_loopback for item in addresses)


pytestmark = [
    pytest.mark.anyio,
    # The shared Core fixture prepares the product knowledge bundle with the
    # local embedding model and never downloads it.
    pytest.mark.skipif(
        not _default_model_ready(),
        reason="requires the already-cached local embedding model (all-MiniLM-L6-v2)",
    ),
    pytest.mark.skipif(
        not _host_name_resolves_to_lan(),
        reason="A non-loopback LAN interface is required: the host name must resolve to it",
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _owner(core, project_id):
    async with core.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, project_id)
        assert owner is not None
        return owner.runtime_id, owner.fencing_token, owner.retirement_requested


async def test_actual_core_unarchive_after_final_chat_retirement_readmits_project(
    core,
):
    project = (
        await completed_response(
            core.client,
            await core.client.post(
                "/api/v1/engagements",
                json={"name": "Restored chat project", "metadata": {}},
                headers={"Idempotency-Key": "restore-project"},
            ),
        )
    ).json()
    provider = (
        await completed_response(
            core.client,
            await core.client.post(
                "/api/v1/providers",
                json={
                    "name": "Inert restore provider",
                    "provider_type": "vllm",
                    "is_local": True,
                    "model_allowlist": ["model-a"],
                },
                headers={"Idempotency-Key": "restore-provider"},
            ),
        )
    ).json()
    first = await completed_response(
        core.client,
        await core.client.post(
            "/api/v1/chat/completions",
            json={
                "engagement_id": project["id"],
                "provider_id": provider["id"],
                "model": "model-a",
                "include_knowledge": False,
                "stream": True,
                "messages": [{"role": "user", "content": "Only chat"}],
            },
            headers={"Idempotency-Key": "restore-first-chat"},
        ),
    )
    assert "event: done" in first.text
    [chat] = await core.store.list_entities(ChatSession, engagement_id=project["id"])
    old_runtime, old_fence, _ = await _owner(core, project["id"])
    assert old_runtime is not None
    await core.wait_for_chat_maintenance(project["id"])

    archived = (
        await completed_response(
            core.client,
            await core.client.patch(
                f"/api/v1/chat-sessions/{chat.id}",
                json={"archived": True, "expected_revision": chat.revision},
                headers={"Idempotency-Key": "archive-final-chat"},
            ),
        )
    ).json()
    assert archived["metadata"].get("archived_at")
    # Real auto-admission retires the now idle worker and releases ownership.
    async with asyncio.timeout(60):
        while await _owner(core, project["id"]) != (None, old_fence, False):
            await asyncio.sleep(0.05)
    async with core.database.session() as session:
        actions = [
            row.action
            for row in await session.scalars(
                select(DeploymentReceiptRow).order_by(
                    DeploymentReceiptRow.occurred_at_ms, DeploymentReceiptRow.id
                )
            )
            if row.result.get("project_id") == project["id"]
        ]
    assert actions[-2:] == ["project.retirement_requested", "project.quiesced"]

    # The operator's Unarchive alone must readmit the project and finish.
    restore = await core.client.patch(
        f"/api/v1/chat-sessions/{chat.id}",
        json={"archived": False, "expected_revision": archived["revision"]},
        headers={"Idempotency-Key": "restore-final-chat"},
    )
    assert restore.status_code in {200, 202}, restore.text
    restored = (await completed_response(core.client, restore, timeout=90)).json()
    assert "archived_at" not in restored["metadata"]
    reread = await core.client.get(f"/api/v1/chat-sessions/{chat.id}")
    assert reread.status_code == 200 and "archived_at" not in reread.json()["metadata"]

    new_runtime, new_fence, retiring = await _owner(core, project["id"])
    assert new_runtime not in {None, old_runtime} and new_fence == old_fence + 1
    assert not retiring
    async with core.database.session() as session:
        commands = list(
            await session.scalars(
                select(RuntimeCommandRow)
                .where(RuntimeCommandRow.project_id == project["id"])
                .order_by(RuntimeCommandRow.sequence)
            )
        )
    assert [
        (command.idempotency_key, command.chat_start, command.state)
        for command in commands
    ][-1] == ("restore-final-chat", True, "succeeded")
    # No chat message was sent to reopen the project.
    turns = await core.store.list_entities(ChatTurn, engagement_id=project["id"])
    assert [(turn.session_id, turn.status) for turn in turns] == [(chat.id, "complete")]
