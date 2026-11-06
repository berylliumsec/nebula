"""Recovery hydrates pending work, including legacy and racing records."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, update

import nebula.v3.storage as storage_module
from nebula.v3.chat import ChatService
from nebula.v3.database import EntityRow
from nebula.v3.domain import ChatBackend, ChatTurn, ChatTurnStatus
from nebula.v3.storage import NebulaStore


@asynccontextmanager
async def open_store(tmp_path):
    store = NebulaStore(tmp_path / "recovery.db")
    try:
        yield store
    finally:
        await store.event_history.close()
        await store.database.dispose()


async def _turn(store, name, *, recovery=None, **fields):
    return await store.create(
        ChatTurn(
            id=name,
            engagement_id="synthetic-project",
            session_id=f"session-{name}",
            provider_profile_id=(
                None
                if fields.get("backend") == ChatBackend.HARNESS
                else "inert-provider"
            ),
            model="inert-model",
            content="saved history " * 1000,
            request_snapshot={"recovery": recovery or {}},
            **{"status": ChatTurnStatus.INTERRUPTED, **fields},
        )
    )


async def _scan(store):
    service = SimpleNamespace(
        store=store, _turn_is_pending=ChatService._turn_is_pending
    )
    return await ChatService._stopped_turn_candidates(service)


async def test_recovery_scan_loads_only_pending_interrupted_payloads(
    tmp_path, monkeypatch
):
    async with open_store(tmp_path) as store:
        for index in range(12):
            await _turn(store, f"historical-{index}")
        required = await _turn(
            store, "required", recovery={"required": True, "cause": "user"}
        )
        retry = await _turn(store, "retry", recovery={"automatic_retry_pending": True})
        harness = await _turn(
            store, "harness", recovery={"required": True}, backend=ChatBackend.HARNESS
        )
        for status in (ChatTurnStatus.COMPLETE, ChatTurnStatus.ROUTING):
            await _turn(store, status.value, status=status, recovery={"required": True})
        loaded = []
        queries = []
        read = storage_module._row_to_entity

        def hydrate(row, expected=None):
            if expected is ChatTurn:
                loaded.append(row.id)
            return read(row, expected)

        def capture(_conn, _cursor, statement, params, _context, _many):
            queries.append((statement, params))

        monkeypatch.setattr(storage_module, "_row_to_entity", hydrate)
        event.listen(
            store.database.engine.sync_engine, "before_cursor_execute", capture
        )
        try:
            assert [turn.id for turn in await _scan(store)] == [required.id, retry.id]
        finally:
            event.remove(
                store.database.engine.sync_engine, "before_cursor_execute", capture
            )
        assert set(loaded) == {required.id, retry.id, harness.id}
        assert len(loaded) == 3
        lookups = queries[:2]
        assert len(lookups) == 2
        assert all("entities.payload" not in sql for sql, _ in lookups)
        if store.database.engine.dialect.name == "sqlite":
            async with store.database.engine.connect() as connection:
                for sql, parameters in lookups:
                    plan = (
                        await connection.exec_driver_sql(
                            "EXPLAIN QUERY PLAN " + sql, parameters
                        )
                    ).all()
                    assert any(
                        "COVERING INDEX ix_entities_chat_turn_status" in row[3]
                        for row in plan
                    ), plan


async def test_recovery_scan_keeps_rows_with_missing_projections(tmp_path, monkeypatch):
    del monkeypatch
    async with open_store(tmp_path) as store:
        expected = []
        cases = [
            (None, None, ChatTurnStatus.INTERRUPTED, {"required": True}),
            (None, False, ChatTurnStatus.INTERRUPTED, {"required": True}),
            (None, True, ChatTurnStatus.INTERRUPTED, {"required": True}),
            ("interrupted", None, ChatTurnStatus.INTERRUPTED, {"required": True}),
            (
                "interrupted",
                None,
                ChatTurnStatus.INTERRUPTED,
                {"automatic_retry_pending": True},
            ),
            (None, None, ChatTurnStatus.COMPLETE, {"required": True}),
            ("interrupted", None, ChatTurnStatus.INTERRUPTED, {}),
        ]
        for index, (status, visible, payload_status, recovery) in enumerate(cases):
            turn = await _turn(
                store, f"legacy-{index}", status=payload_status, recovery=recovery
            )
            async with store.database.engine.begin() as connection:
                await connection.execute(
                    update(EntityRow)
                    .where(EntityRow.id == turn.id)
                    .values(chat_turn_status=status, chat_turn_recovery_visible=visible)
                )
            if index < 5:
                expected.append(turn.id)
        assert [turn.id for turn in await _scan(store)] == expected


@pytest.mark.parametrize("change", ["complete", "routing", "settled", "deleted"])
async def test_recovery_scan_rechecks_after_id_selection(tmp_path, monkeypatch, change):
    async with open_store(tmp_path) as store:
        turn = await _turn(store, "racing", recovery={"required": True})
        hydrate = store._iter_readable_ids

        async def changed(model, statement, ids, page_size):
            assert ids == [turn.id]
            if change == "deleted":
                await store.delete(ChatTurn, turn.id)
            else:
                fields = (
                    {"request_snapshot": {}}
                    if change == "settled"
                    else {"status": ChatTurnStatus(change)}
                )
                await store.update(ChatTurn, turn.id, fields)
            async for entity in hydrate(model, statement, ids, page_size):
                yield entity

        monkeypatch.setattr(store, "_iter_readable_ids", changed)
        assert await _scan(store) == []


async def test_recovery_scan_skips_unreadable_candidates(tmp_path, monkeypatch):
    del monkeypatch
    async with open_store(tmp_path) as store:
        broken = await _turn(store, "broken", recovery={"required": True})
        good = await _turn(store, "good", recovery={"required": True})
        async with store.database.engine.begin() as connection:
            await connection.execute(
                update(EntityRow)
                .where(EntityRow.id == broken.id)
                .values(payload={"id": broken.id})
            )
        assert [turn.id for turn in await _scan(store)] == [good.id]


async def test_recovery_journey_on_authenticated_lan_core(tmp_path, monkeypatch):
    """Startup recovers the saved turn once; HTTP and an optional UI reload agree.

    Set NEBULA_TEST_UI_DIST to the production bundle for the local browser gate.
    CI exercises the same real Core lifecycle through its authenticated LAN API.
    """
    import asyncio
    import base64
    from datetime import datetime, timedelta, timezone
    import hashlib
    import ipaddress
    import json
    import os
    from pathlib import Path
    import socket
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import httpx
    import uvicorn

    import nebula.v3.chat as chat_module
    from nebula.v3.api import create_app
    from nebula.v3.domain import ChatMessage, ChatSession, Engagement
    from nebula.v3.providers import ModelRequest
    from tests.v3.test_chat import FakeProvider, _profile

    monkeypatch.setenv("NEBULA_V3_KNOWLEDGE_RERANKER", "off")
    static_dir = os.environ.get("NEBULA_TEST_UI_DIST")
    host = socket.gethostbyname(socket.gethostname())
    assert not host.startswith("127."), (
        "The real Core acceptance gate needs a LAN address"
    )
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic recovery Core")]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address(host)),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "core.pem", tmp_path / "core-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    trust = ssl.create_default_context(cafile=str(cert_path))
    spki = base64.b64encode(
        hashlib.sha256(
            key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).digest()
    ).decode()
    listener = socket.socket()
    listener.bind((host, 0))
    port = listener.getsockname()[1]
    origin = f"https://{host}:{port}"
    browser_origin = f"https://127.0.0.1:{port}"
    # Pairing creation keeps Core's normal host-local control boundary.
    local_listener = socket.socket()
    local_listener.bind(("127.0.0.1", port))
    token = "synthetic-recovery-lan-token"
    headers = {"Authorization": f"Bearer {token}"}
    async with open_store(tmp_path) as store:
        project = await store.create(Engagement(name="Synthetic recovery"))
        profile = await store.create(_profile(local=True))
        session = await store.create(
            ChatSession(
                engagement_id=project.id,
                title="Recovery candidate check",
                provider_profile_id=profile.id,
                model="model-a",
                metadata={"initial_title_state": "operator"},
            )
        )
        await store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=session.id,
                sequence=1,
                role="user",
                content="Continue the saved response.",
            )
        )
        turn = await store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.INTERRUPTED,
                request_snapshot={
                    "model_request": ModelRequest(
                        model="model-a",
                        messages=[
                            {"role": "user", "content": "Continue the saved response."}
                        ],
                    ).model_dump(mode="json"),
                    "context_usage": {},
                    "recovery": {
                        "required": True,
                        "cause": "core_restart",
                        "unknown_tool_call_ids": [],
                        "unknown_hook_execution_ids": [],
                    },
                },
            )
        )
        provider = FakeProvider(profile.id, local=True)
        monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
        app = create_app(
            store, auth_token=token, static_dir=static_dir, execution_data_root=tmp_path
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="error",
                access_log=False,
                ssl_certfile=str(cert_path),
                ssl_keyfile=str(key_path),
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener, local_listener]))
        try:
            async with asyncio.timeout(45):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("throwaway Core did not start")
                    await asyncio.sleep(0.02)
            async with httpx.AsyncClient(
                base_url=origin, trust_env=False, timeout=20, verify=trust
            ) as client:
                path = f"/api/v1/chat/sessions/{session.id}/messages"
                assert (await client.get(path)).status_code == 401
                async with asyncio.timeout(20):
                    while (
                        await store.get(ChatTurn, turn.id)
                    ).status != ChatTurnStatus.COMPLETE:
                        await asyncio.sleep(0.02)
                first = await client.get(path, headers=headers)
                assert first.status_code == 200
                messages = first.json()
                assert [m["role"] for m in messages] == ["user", "assistant"]
                assert (await client.get(path, headers=headers)).json() == messages
                pending = await client.get(
                    f"/api/v1/chat/sessions/{session.id}/pending-turn", headers=headers
                )
                assert pending.status_code == 200 and pending.json() is None
                queue_path = f"/api/v1/chat/sessions/{session.id}/queue"
                empty_queue = await client.get(queue_path, headers=headers)
                assert empty_queue.status_code == 200
                assert empty_queue.json()["revision"] == 0
                assert empty_queue.json()["items"] == []
                unchanged = await client.get(
                    queue_path,
                    headers={**headers, "If-None-Match": empty_queue.headers["etag"]},
                )
                assert unchanged.status_code == 304
                paused_queue = await client.post(
                    queue_path,
                    headers=headers,
                    json={"action": "pause", "expected_revision": 0},
                )
                assert (
                    paused_queue.status_code == 200
                    and paused_queue.json()["paused"] is True
                )
                saved_queue = await client.get(queue_path, headers=headers)
                assert saved_queue.json() == paused_queue.json()
                assert saved_queue.headers["etag"] != empty_queue.headers["etag"]
                receipt = {
                    "origin": origin,
                    "turn_status": "complete",
                    "assistant_messages": 1,
                    "api_reload_matches": True,
                    "unauthenticated_status": 401,
                    "browser": None,
                    "queue_etag_and_write": True,
                }
                if static_dir:
                    from playwright.async_api import async_playwright, expect

                    build = (await client.get("/web-build.json")).json()
                    assert build == json.loads(
                        (Path(static_dir) / "web-build.json").read_text()
                    )
                    async with httpx.AsyncClient(
                        base_url=browser_origin, trust_env=False, verify=trust
                    ) as local:
                        pairing_response = await local.post(
                            "/api/v1/auth/pairings",
                            headers=headers,
                            json={"name": "Recovery check"},
                        )
                    assert pairing_response.status_code == 200
                    pairing = pairing_response.json()
                    async with async_playwright() as playwright:
                        browser = await playwright.chromium.launch(
                            executable_path=os.environ.get(
                                "NEBULA_TEST_CHROMIUM_EXECUTABLE"
                            )
                            or None,
                            args=[
                                "--no-sandbox",
                                f"--ignore-certificate-errors-spki-list={spki}",
                            ],
                        )
                        try:
                            page = await browser.new_page(
                                viewport={"width": 1440, "height": 1000}
                            )
                            errors = []
                            page.on(
                                "pageerror", lambda error: errors.append(str(error))
                            )
                            response = await page.goto(
                                f"{browser_origin}/#pair={pairing['secret']}&code={pairing['confirmation_code']}"
                            )
                            assert response is not None and response.status == 200
                            try:
                                await expect(
                                    page.get_by_role("heading", name="Pair this device")
                                ).to_be_visible(timeout=10000)
                            except AssertionError:
                                pytest.fail(
                                    f"Pairing form did not render: {errors}; {(await page.locator('body').inner_text())[:1000]}"
                                )
                            await page.get_by_label("Device name").fill(
                                "Recovery check"
                            )
                            await page.get_by_role(
                                "button", name="Pair device", exact=True
                            ).click()
                            await expect(page.get_by_label("Device name")).to_be_hidden(
                                timeout=20000
                            )
                            await page.goto(
                                f"{browser_origin}/projects/{project.id}/workbench?view=chat&session={session.id}"
                            )
                            answer = page.locator(
                                ".chat-message.assistant .assistant-markdown"
                            )
                            await expect(answer).to_have_count(1)
                            await expect(answer).to_contain_text(
                                "Evidence-backed answer", timeout=20000
                            )
                            await page.reload()
                            await expect(answer).to_have_count(1)
                            await expect(answer).to_contain_text(
                                "Evidence-backed answer", timeout=20000
                            )
                            receipt.update(
                                browser=f"Chromium {browser.version}; desktop 1440x1000",
                                browser_origin=browser_origin,
                                build=build,
                            )
                        finally:
                            await browser.close()
                assert len(provider.requests) == 1
                assert (
                    await store.get(ChatTurn, turn.id)
                ).status == ChatTurnStatus.COMPLETE
                if destination := os.environ.get("NEBULA_RECOVERY_LAN_RECEIPT"):
                    Path(destination).write_text(json.dumps(receipt, indent=2) + "\n")
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 30)
            listener.close()
            local_listener.close()
