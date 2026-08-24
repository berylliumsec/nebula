import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from nebula.v3.api import create_app
from nebula.v3.domain import Engagement
from nebula.v3.language_server import (
    LanguageDiagnosticsRequest,
    LanguageDocument,
    LanguageServerSession,
    analyze_documents,
    path_from_uri,
    uri_for_path,
)
from nebula.v3.storage import NebulaStore


def test_language_paths_are_workspace_relative_and_uri_safe() -> None:
    assert uri_for_path("src/a b.py") == "file:///workspace/src/a%20b.py"
    assert path_from_uri("file:///workspace/src/a%20b.py") == "src/a b.py"
    for value in ("/etc/passwd", "../outside.py", "a//b.py", "a\\b.py"):
        with pytest.raises(ValueError):
            uri_for_path(value)
    for value in (
        "file:///etc/passwd",
        "file:///workspace/a%2fb.py",
        "file:///workspace/a/%2e%2e/b.py",
    ):
        with pytest.raises(ValueError):
            path_from_uri(value)


def test_language_document_rejects_nul_and_oversized_utf8() -> None:
    with pytest.raises(ValidationError):
        LanguageDocument(path="a.py", source="a\0b")
    with pytest.raises(ValidationError):
        LanguageDocument(path="a.py", source="😀" * 300_000)


def test_batch_diagnostics_find_python_errors() -> None:
    response = asyncio.run(
        analyze_documents(
            LanguageDiagnosticsRequest(
                engagement_id="engagement-1",
                documents=[
                    LanguageDocument(
                        path="probe.py", source="print(missing)\n", version=7
                    )
                ],
            )
        )
    )
    document = response.documents[0]
    assert document.version == 7
    assert any(item["code"] == "F821" for item in document.diagnostics)


def test_lsp_session_supports_intelligence_and_versioned_diagnostics() -> None:
    async def exercise() -> None:
        session = LanguageServerSession("engagement-1")
        initialized = await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"rootUri": "file:///workspace"},
            }
        )
        capabilities = initialized[0]["result"]["capabilities"]
        assert capabilities["hoverProvider"] is True
        assert capabilities["definitionProvider"] is True
        source = (
            "def greet(name: str):\n    return name.upper()\n\nvalue = greet(missing)\n"
        )
        published = await session.handle(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": "file:///workspace/demo.py",
                        "languageId": "python",
                        "version": 1,
                        "text": source,
                    }
                },
            }
        )
        assert published[0]["params"]["version"] == 1
        assert any(
            item["code"] == "F821" for item in published[0]["params"]["diagnostics"]
        )
        completion = await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "textDocument/completion",
                "params": {
                    "textDocument": {"uri": "file:///workspace/demo.py"},
                    "position": {"line": 1, "character": 16},
                },
            }
        )
        assert any(item["label"] == "upper" for item in completion[0]["result"])
        definition = await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": "file:///workspace/demo.py"},
                    "position": {"line": 3, "character": 10},
                },
            }
        )
        assert definition[0]["result"][0]["range"]["start"] == {
            "line": 0,
            "character": 4,
        }
        changed = await session.handle(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {"uri": "file:///workspace/demo.py", "version": 2},
                    "contentChanges": [{"text": source.replace("missing", "'ok'")}],
                },
            }
        )
        assert changed[0]["params"]["version"] == 2
        assert not any(
            item.get("code") == "F821" for item in changed[0]["params"]["diagnostics"]
        )
        stale = await session.handle(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {"uri": "file:///workspace/demo.py", "version": 2},
                    "contentChanges": [{"text": source}],
                },
            }
        )
        assert stale == []
        invalid_version = await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {
                        "uri": "file:///workspace/demo.py",
                        "version": True,
                    },
                    "contentChanges": [{"text": source}],
                },
            }
        )
        assert invalid_version[0]["error"]["code"] == -32602

    asyncio.run(exercise())


def test_lsp_utf16_positions_and_external_root_fail_closed() -> None:
    async def exercise() -> None:
        session = LanguageServerSession("engagement-1")
        rejected = await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"rootUri": "file:///tmp"},
            }
        )
        assert rejected[0]["error"]["code"] == -32602
        session = LanguageServerSession("engagement-1")
        await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"rootUri": "file:///workspace"},
            }
        )
        await session.handle(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": "file:///workspace/emoji.py",
                        "languageId": "python",
                        "version": 1,
                        "text": "value = '😀'.upp\n",
                    }
                },
            }
        )
        completion = await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "textDocument/completion",
                "params": {
                    "textDocument": {"uri": "file:///workspace/emoji.py"},
                    "position": {"line": 0, "character": 16},
                },
            }
        )
        assert any(item["label"] == "upper" for item in completion[0]["result"])

    asyncio.run(exercise())


def test_authenticated_language_websocket_and_batch_endpoint(tmp_path) -> None:
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Language service"))
    client = TestClient(create_app(store, auth_token="test-token"))
    response = client.post(
        "/api/v1/code/diagnostics",
        headers={"Authorization": "Bearer test-token"},
        json={
            "engagement_id": engagement.id,
            "documents": [{"path": "a.py", "source": "print(nope)\n", "version": 1}],
        },
    )
    assert response.status_code == 200
    assert response.json()["documents"][0]["diagnostics"][0]["code"] == "F821"
    with client.websocket_connect(
        f"/api/v1/engagements/{engagement.id}/language-server/ws",
        subprotocols=["nebula.language-server.v1", "nebula.auth.dGVzdC10b2tlbg"],
    ) as websocket:
        assert websocket.accepted_subprotocol == "nebula.language-server.v1"
        websocket.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"rootUri": "file:///workspace"},
            }
        )
        assert (
            websocket.receive_json()["result"]["capabilities"]["hoverProvider"] is True
        )
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"/api/v1/engagements/{engagement.id}/language-server/ws",
            subprotocols=["nebula.language-server.v1"],
        ):
            pass
    assert exc_info.value.code == 4401
