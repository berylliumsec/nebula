"""Live turn status projections keep rosters fast and recovery semantics intact."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import text

from nebula.v3.chat_agent_messages import AgentMessageService
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    EngagementStatus,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.mcp import McpGatewaySession
from nebula.v3.mcp_gateway import GatewayClient
from nebula.v3.storage import NebulaStore
from nebula.v3.work import WorkService


async def test_status_lookup_is_covering_and_tracks_every_turn_update(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Active"))
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main",
            provider_profile_id="provider-1",
            model="model-a",
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id="provider-1",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            content="historical payload" * 1000,
        )
    )
    assert [
        (row.id, row.status)
        for row in (
            await store.chat_turn_status_rows(
                [ChatTurnStatus.ROUTING], engagement_id=project.id
            )
        )
    ] == [(turn.id, ChatTurnStatus.ROUTING)]
    assert [
        row.id for row in (await store.find_entities(ChatTurn, {"status": ["routing"]}))
    ] == [turn.id]
    async with store.database.engine.connect() as connection:
        project_plan = (
            await connection.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT id, engagement_id, created_at, "
                    "chat_session_id, "
                    "chat_turn_status, chat_turn_recovery_visible, "
                    "chat_turn_core_resume_pending FROM entities "
                    "WHERE kind = 'chat_turns' AND chat_turn_status IN "
                    "('routing', 'interrupted') AND engagement_id = :project"
                ),
                {"project": project.id},
            )
        ).all()
        global_plan = (
            await connection.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT id, engagement_id, created_at, "
                    "chat_session_id, "
                    "chat_turn_status, chat_turn_recovery_visible, "
                    "chat_turn_core_resume_pending FROM entities "
                    "WHERE kind = 'chat_turns' AND chat_turn_status IN "
                    "('routing', 'interrupted')"
                ),
            )
        ).all()
    assert any(
        "COVERING INDEX ix_entities_project_chat_turn_status" in row[3]
        for row in project_plan
    ), project_plan
    assert any(
        "COVERING INDEX ix_entities_chat_turn_status" in row[3] for row in global_plan
    ), global_plan

    # Historical imports can use ISO `T` while ORM rows use a space. Preserve
    # SQLite's original created_at ordering without comparing aware and naive
    # Python datetimes.
    other = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id="provider-1",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
        )
    )
    async with store.database.engine.begin() as connection:
        (
            await connection.execute(
                text("UPDATE entities SET created_at=:created WHERE id=:id"),
                {"created": "2026-11-04T00:00:00+00:00", "id": turn.id},
            )
        )
        (
            await connection.execute(
                text("UPDATE entities SET created_at=:created WHERE id=:id"),
                {"created": "2026-11-04 12:00:00.000000", "id": other.id},
            )
        )
    assert [
        row.id for row in (await store.chat_turn_status_rows([ChatTurnStatus.ROUTING]))
    ] == [
        other.id,
        turn.id,
    ]
    (await store.update(ChatTurn, other.id, {"status": ChatTurnStatus.COMPLETE}))

    (await store.update(ChatTurn, turn.id, {"status": ChatTurnStatus.INTERRUPTED}))
    assert (await store.chat_turn_status_rows([ChatTurnStatus.ROUTING])) == []
    interrupted = (await store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED]))[0]
    assert (interrupted.recovery_visible, interrupted.core_resume_pending) == (
        False,
        False,
    )
    (
        await store.update(
            ChatTurn,
            turn.id,
            {
                "request_snapshot": {
                    "recovery": {"required": True, "cause": "core_restart"}
                }
            },
        )
    )
    interrupted = (await store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED]))[0]
    assert (interrupted.recovery_visible, interrupted.core_resume_pending) == (
        True,
        True,
    )
    (
        await store.update(
            ChatTurn,
            turn.id,
            {"request_snapshot": {"recovery": {"required": True, "cause": "user"}}},
        )
    )
    interrupted = (await store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED]))[0]
    assert (interrupted.recovery_visible, interrupted.core_resume_pending) == (
        True,
        False,
    )
    (await store.update(ChatTurn, turn.id, {"status": ChatTurnStatus.COMPLETE}))
    assert (await store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED])) == []


async def test_work_roster_preserves_recovery_scope_and_turn_order(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Active"))
    archived = await store.create(
        Engagement(name="Archived", status=EngagementStatus.ARCHIVED)
    )
    start = datetime(2026, 11, 4, tzinfo=timezone.utc)

    async def add(
        title, status, recovery=None, *, engagement=project, metadata=None, minute=0
    ):
        session = await store.create(
            ChatSession(
                engagement_id=engagement.id,
                title=title,
                provider_profile_id="provider-1",
                model="model-a",
                metadata=metadata or {},
            )
        )
        turn = await store.create(
            ChatTurn(
                engagement_id=engagement.id,
                session_id=session.id,
                provider_profile_id="provider-1",
                model="model-a",
                status=status,
                request_snapshot={"recovery": recovery} if recovery is not None else {},
                created_at=start + timedelta(minutes=minute),
            )
        )
        return turn

    routing = await add("Routing", ChatTurnStatus.ROUTING, minute=0)
    resumed = await add(
        "Core recovery",
        ChatTurnStatus.INTERRUPTED,
        {"required": True, "cause": "core_restart"},
        minute=1,
    )
    manual = await add(
        "Manual recovery",
        ChatTurnStatus.INTERRUPTED,
        {"required": True, "cause": "user"},
        minute=2,
    )
    retry = await add(
        "Automatic retry",
        ChatTurnStatus.INTERRUPTED,
        {"automatic_retry_pending": True},
        minute=3,
    )
    (await add("Settled interruption", ChatTurnStatus.INTERRUPTED, minute=4))
    (await add("Side", ChatTurnStatus.ROUTING, metadata={"side_chat": True}, minute=5))
    (
        await add(
            "Temporary",
            ChatTurnStatus.ROUTING,
            metadata={"temporary_assistant": True},
            minute=6,
        )
    )
    (
        await add(
            "Archived project", ChatTurnStatus.ROUTING, engagement=archived, minute=7
        )
    )
    result = await WorkService(store).active_agents()
    assert [row.turn_id for row in result] == [
        routing.id,
        resumed.id,
        manual.id,
        retry.id,
    ]
    assert [row.state for row in result] == [
        "working",
        "working",
        "waiting",
        "waiting",
    ]
    assert [
        row.turn_id for row in (await WorkService(store).active_agents(project.id))
    ] == [
        routing.id,
        resumed.id,
        manual.id,
        retry.id,
    ]
    assert (await WorkService(store).active_agents(archived.id)) == []


async def test_peer_status_uses_latest_unfinished_turn_and_filters_sessions(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Active"))

    async def session(title, **metadata):
        return await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id="provider-1",
                model="model-a",
                metadata={"allow_agent_messaging": True, **metadata},
            )
        )

    sender = await session("Sender")
    peer = await session("Peer")
    (await session("Archived", archived_at="2026-11-04T00:00:00Z"))
    (await session("Temporary", temporary_assistant=True))
    (await session("Side", side_chat=True))

    async def turn(status):
        return await store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=peer.id,
                provider_profile_id="provider-1",
                model="model-a",
                status=status,
            )
        )

    first = await turn(ChatTurnStatus.ROUTING)
    later = await turn(ChatTurnStatus.WAITING_CALLBACK)
    service = AgentMessageService(store)
    assert (await service.peers(sender)) == [
        {
            "session_id": peer.id,
            "title": "Peer",
            "backend": "provider",
            "model": "model-a",
            "state": "waiting_callback",
        }
    ]
    (await store.update(ChatTurn, later.id, {"status": ChatTurnStatus.COMPLETE}))
    assert (await service.peers(sender))[0]["state"] == first.status.value

    profile = await store.create(
        HarnessProfile(
            name="Inert fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            privacy={"local_only": True, "permits_sensitive_data": False},
        )
    )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: None,
    )
    runtime.bind_agent_messages(service)
    _, _, gateway_turn = await runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="List peers",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    gateway_session = await store.get(HarnessSession, gateway_turn.harness_session_id)
    runtime._active[gateway_session.id] = SimpleNamespace(
        turn_id=gateway_turn.id, connection=None, task=None
    )

    async def list_through_gateway():
        gateway = McpGatewaySession(
            list_tools=lambda params: runtime._gateway_catalog(
                gateway_session, params, turn=gateway_turn
            ),
            call_tool=lambda name, arguments: runtime._gateway_call(
                gateway_session, name, arguments
            ),
        )
        launch = await gateway.start()
        client = GatewayClient(launch.socket_path, launch.token)
        try:
            return await client.request(
                "tools/call", {"name": "agent.list", "arguments": {}}
            )
        finally:
            await client.close()
            await gateway.close()

    result = await list_through_gateway()
    assert result["isError"] is False
    assert {
        item["session_id"]: item["state"]
        for item in result["structuredContent"]["agents"]
    }[peer.id] == first.status.value


pytestmark = pytest.mark.anyio
