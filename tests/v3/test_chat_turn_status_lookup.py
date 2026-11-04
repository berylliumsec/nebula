"""Live turn status projections keep rosters fast and recovery semantics intact."""

import asyncio
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


def test_status_lookup_is_covering_and_tracks_every_turn_update(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Active"))
    session = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main",
            provider_profile_id="provider-1",
            model="model-a",
        )
    )
    turn = store.create(
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
        for row in store.chat_turn_status_rows(
            [ChatTurnStatus.ROUTING], engagement_id=project.id
        )
    ] == [(turn.id, ChatTurnStatus.ROUTING)]
    assert [
        row.id for row in store.find_entities(ChatTurn, {"status": ["routing"]})
    ] == [turn.id]
    with store.database.engine.connect() as connection:
        project_plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id, engagement_id, created_at, "
                "chat_session_id, "
                "chat_turn_status, chat_turn_recovery_visible, "
                "chat_turn_core_resume_pending FROM entities "
                "WHERE kind = 'chat_turns' AND chat_turn_status IN "
                "('routing', 'interrupted') AND engagement_id = :project"
            ),
            {"project": project.id},
        ).all()
        global_plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id, engagement_id, created_at, "
                "chat_session_id, "
                "chat_turn_status, chat_turn_recovery_visible, "
                "chat_turn_core_resume_pending FROM entities "
                "WHERE kind = 'chat_turns' AND chat_turn_status IN "
                "('routing', 'interrupted')"
            ),
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
    other = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id="provider-1",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
        )
    )
    with store.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE entities SET created_at=:created WHERE id=:id"),
            {"created": "2026-11-04T00:00:00+00:00", "id": turn.id},
        )
        connection.execute(
            text("UPDATE entities SET created_at=:created WHERE id=:id"),
            {"created": "2026-11-04 12:00:00.000000", "id": other.id},
        )
    assert [
        row.id for row in store.chat_turn_status_rows([ChatTurnStatus.ROUTING])
    ] == [
        other.id,
        turn.id,
    ]
    store.update(ChatTurn, other.id, {"status": ChatTurnStatus.COMPLETE})

    store.update(ChatTurn, turn.id, {"status": ChatTurnStatus.INTERRUPTED})
    assert store.chat_turn_status_rows([ChatTurnStatus.ROUTING]) == []
    interrupted = store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED])[0]
    assert (interrupted.recovery_visible, interrupted.core_resume_pending) == (
        False,
        False,
    )
    store.update(
        ChatTurn,
        turn.id,
        {"request_snapshot": {"recovery": {"required": True, "cause": "core_restart"}}},
    )
    interrupted = store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED])[0]
    assert (interrupted.recovery_visible, interrupted.core_resume_pending) == (
        True,
        True,
    )
    store.update(
        ChatTurn,
        turn.id,
        {"request_snapshot": {"recovery": {"required": True, "cause": "user"}}},
    )
    interrupted = store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED])[0]
    assert (interrupted.recovery_visible, interrupted.core_resume_pending) == (
        True,
        False,
    )
    store.update(ChatTurn, turn.id, {"status": ChatTurnStatus.COMPLETE})
    assert store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED]) == []


def test_work_roster_preserves_recovery_scope_and_turn_order(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Active"))
    archived = store.create(
        Engagement(name="Archived", status=EngagementStatus.ARCHIVED)
    )
    start = datetime(2026, 11, 4, tzinfo=timezone.utc)

    def add(
        title, status, recovery=None, *, engagement=project, metadata=None, minute=0
    ):
        session = store.create(
            ChatSession(
                engagement_id=engagement.id,
                title=title,
                provider_profile_id="provider-1",
                model="model-a",
                metadata=metadata or {},
            )
        )
        turn = store.create(
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

    routing = add("Routing", ChatTurnStatus.ROUTING, minute=0)
    resumed = add(
        "Core recovery",
        ChatTurnStatus.INTERRUPTED,
        {"required": True, "cause": "core_restart"},
        minute=1,
    )
    manual = add(
        "Manual recovery",
        ChatTurnStatus.INTERRUPTED,
        {"required": True, "cause": "user"},
        minute=2,
    )
    retry = add(
        "Automatic retry",
        ChatTurnStatus.INTERRUPTED,
        {"automatic_retry_pending": True},
        minute=3,
    )
    add("Settled interruption", ChatTurnStatus.INTERRUPTED, minute=4)
    add("Side", ChatTurnStatus.ROUTING, metadata={"side_chat": True}, minute=5)
    add(
        "Temporary",
        ChatTurnStatus.ROUTING,
        metadata={"temporary_assistant": True},
        minute=6,
    )
    add("Archived project", ChatTurnStatus.ROUTING, engagement=archived, minute=7)
    result = WorkService(store).active_agents()
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
    assert [row.turn_id for row in WorkService(store).active_agents(project.id)] == [
        routing.id,
        resumed.id,
        manual.id,
        retry.id,
    ]
    assert WorkService(store).active_agents(archived.id) == []


def test_peer_status_uses_latest_unfinished_turn_and_filters_sessions(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Active"))

    def session(title, **metadata):
        return store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id="provider-1",
                model="model-a",
                metadata={"allow_agent_messaging": True, **metadata},
            )
        )

    sender = session("Sender")
    peer = session("Peer")
    session("Archived", archived_at="2026-11-04T00:00:00Z")
    session("Temporary", temporary_assistant=True)
    session("Side", side_chat=True)

    def turn(status):
        return store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=peer.id,
                provider_profile_id="provider-1",
                model="model-a",
                status=status,
            )
        )

    first = turn(ChatTurnStatus.ROUTING)
    later = turn(ChatTurnStatus.WAITING_CALLBACK)
    service = AgentMessageService(store)
    assert service.peers(sender) == [
        {
            "session_id": peer.id,
            "title": "Peer",
            "backend": "provider",
            "model": "model-a",
            "state": "waiting_callback",
        }
    ]
    store.update(ChatTurn, later.id, {"status": ChatTurnStatus.COMPLETE})
    assert service.peers(sender)[0]["state"] == first.status.value

    profile = store.create(
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
    _, _, gateway_turn = runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="List peers",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    gateway_session = store.get(HarnessSession, gateway_turn.harness_session_id)
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

    result = asyncio.run(list_through_gateway())
    assert result["isError"] is False
    assert {
        item["session_id"]: item["state"]
        for item in result["structuredContent"]["agents"]
    }[peer.id] == first.status.value
