"""Harness session names use only the requested page's related records."""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from nebula.v3.api import create_app
from nebula.v3.database import EntityRow
from nebula.v3.domain import (
    AgentRun,
    ChatBackend,
    ChatSession,
    Engagement,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    RunBackend,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.version import build_metadata


STAMP = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def _session(key: str, position: int, project: str = "project") -> HarnessSession:
    stamp = STAMP + timedelta(minutes=position)
    return HarnessSession(
        id=key,
        engagement_id=project,
        harness_profile_id="synthetic-harness",
        model="fixture-model",
        created_at=stamp,
        updated_at=stamp,
    )


def _turn(key: str, session_id: str, position: int, prompt: str) -> HarnessTurn:
    stamp = STAMP + timedelta(minutes=position)
    return HarnessTurn(
        id=key,
        engagement_id="project",
        harness_session_id=session_id,
        origin=HarnessTurnOrigin.ANALYSIS,
        prompt=prompt,
        response=f"Response for {key}",
        created_at=stamp,
        updated_at=stamp,
    )


def test_paged_names_preserve_precedence_and_ignore_unrelated_history(tmp_path):
    store = NebulaStore(tmp_path / "sessions.db")
    store.create(Engagement(id="project", name="Synthetic project"))
    store.create(Engagement(id="foreign", name="Foreign project"))
    store.create_many(
        [
            _session("chat", 0),
            _session("run", 1),
            _session("turn", 2),
            _session("fallback", 3),
            _session("outside", 4),
        ]
    )
    store.create_many(
        [
            ChatSession(
                id="chat-old",
                engagement_id="project",
                title="Old chat title",
                backend=ChatBackend.HARNESS,
                harness_profile_id="synthetic-harness",
                harness_session_id="chat",
                model="fixture-model",
                created_at=STAMP,
            ),
            ChatSession(
                id="chat-current",
                engagement_id="project",
                title="  Current  chat\n title ",
                backend=ChatBackend.HARNESS,
                harness_profile_id="synthetic-harness",
                harness_session_id="chat",
                model="fixture-model",
                created_at=STAMP + timedelta(minutes=1),
            ),
            ChatSession(
                id="chat-temporary",
                engagement_id="project",
                title="Hidden temporary title",
                backend=ChatBackend.HARNESS,
                harness_profile_id="synthetic-harness",
                harness_session_id="chat",
                model="fixture-model",
                metadata={"temporary_assistant": True},
                created_at=STAMP + timedelta(minutes=2),
            ),
            ChatSession(
                id="foreign-chat",
                engagement_id="foreign",
                title="Foreign chat title",
                backend=ChatBackend.HARNESS,
                harness_profile_id="synthetic-harness",
                harness_session_id="chat",
                model="fixture-model",
                created_at=STAMP + timedelta(minutes=3),
            ),
            AgentRun(
                id="run-name",
                engagement_id="project",
                objective="Run objective",
                backend=RunBackend.HARNESS,
                harness_profile_id="synthetic-harness",
                harness_session_id="run",
                metadata={"name": "  Named   mission  "},
            ),
            _turn("chat-turn", "chat", 0, "Chat turn title"),
            _turn("run-turn", "run", 0, "Run turn title"),
            _turn(
                "turn-first",
                "turn",
                0,
                "Name this conversation from its first exchange: example",
            ),
            _turn("turn-later", "turn", 1, "Later turn title"),
        ]
    )
    # The old implementation needed an extra 1,000-row page to name two rows.
    store.create_many(
        [_turn(f"unrelated-{i:04}", "outside", i, "Unrelated") for i in range(1_001)]
    )
    app = create_app(store, auth_token="synthetic-token")
    headers = {"Authorization": "Bearer synthetic-token"}
    entity_selects: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("SELECT") and "FROM entities" in statement:
            entity_selects.append(statement)

    event.listen(store.database.engine, "before_cursor_execute", capture)
    try:
        client = TestClient(app)
        first = client.get(
            "/api/v1/harness-sessions",
            headers=headers,
            params={"engagement_id": "project", "limit": 2},
        )
        assert first.status_code == 200, first.text
        assert [(row["id"], row["display_name"]) for row in first.json()] == [
            ("chat", "Current chat title"),
            ("run", "Named mission"),
        ]
        assert len(entity_selects) <= 4
        assert all("OFFSET" not in query for query in entity_selects[1:])

        next_page = client.get(
            "/api/v1/harness-sessions",
            headers=headers,
            params={"engagement_id": "project", "limit": 2, "offset": 2},
        )
        assert next_page.status_code == 200, next_page.text
        assert [(row["id"], row["display_name"]) for row in next_page.json()] == [
            ("turn", "Response for turn-first"),
            ("fallback", "Unattached session"),
        ]
        assert client.get("/api/v1/harness-sessions").status_code == 401
    finally:
        event.remove(store.database.engine, "before_cursor_execute", capture)
        store.database.dispose()


def test_harness_session_link_projection_tracks_rebinding(tmp_path):
    store = NebulaStore(tmp_path / "projection.db")
    chat = store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Bound chat",
            backend=ChatBackend.HARNESS,
            harness_profile_id="synthetic-harness",
            harness_session_id="first",
            model="fixture-model",
        )
    )
    store.create(_turn("turn", "first", 0, "First prompt"))
    with store.database.session() as session:
        assert dict(
            session.execute(
                select(EntityRow.id, EntityRow.harness_session_id).where(
                    EntityRow.id.in_(["chat", "turn"])
                )
            ).all()
        ) == {"chat": "first", "turn": "first"}
    store.update(
        ChatSession,
        chat.id,
        {"harness_session_id": "second"},
        expected_revision=chat.revision,
    )
    with store.database.session() as session:
        assert session.get(EntityRow, chat.id).harness_session_id == "second"
    store.database.dispose()


def test_authenticated_lan_core_lists_same_names_after_refresh(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    if ipaddress.ip_address(address).is_loopback:
        pytest.skip("A non-loopback LAN interface is required")
    listener = socket.socket()
    listener.bind((address, 0))
    listener.listen(128)
    origin = f"http://{address}:{listener.getsockname()[1]}"
    token = secrets.token_urlsafe(32)
    auth = {"Authorization": f"Bearer {token}"}
    store = NebulaStore(tmp_path / "lan-core" / "nebula.db")
    store.create(Engagement(id="project", name="Synthetic listing project"))
    store.create(_session("resume", 0))
    store.create(_turn("first-turn", "resume", 0, "Synthetic resumed work"))
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(store, auth_token=token), host=address, log_level="error"
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Synthetic LAN Core did not start"
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            assert client.get("/api/v1/harness-sessions").status_code == 401
            listed = client.get(
                "/api/v1/harness-sessions",
                headers=auth,
                params={"engagement_id": "project"},
            )
            assert listed.status_code == 200, listed.text
            assert [(row["id"], row["display_name"]) for row in listed.json()] == [
                ("resume", "Synthetic resumed work")
            ]
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            refreshed = client.get(
                "/api/v1/harness-sessions",
                headers=auth,
                params={"engagement_id": "project"},
            )
            assert refreshed.status_code == 200
            assert refreshed.json() == listed.json()
            empty = client.get(
                "/api/v1/harness-sessions",
                headers=auth,
                params={"engagement_id": "project", "offset": 1},
            )
            assert empty.status_code == 200
            assert empty.json() == []
        print(
            json.dumps(
                {
                    "origin": origin,
                    "build": build_metadata(),
                    "fixture": "isolated synthetic Core, bearer auth, no provider calls",
                    "journey": "list, refresh, empty page, auth rejection",
                }
            )
        )
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        store.database.dispose()
        assert not thread.is_alive(), "Synthetic LAN Core did not shut down"
