"""The legacy nebula-chat client follows Core's saved-request receipts too."""

from __future__ import annotations

import json

import httpx
import pytest

from nebula import chat_cli
from nebula.terminal import transport
from nebula.terminal.transport import (
    AcceptedRequest,
    ClientError,
    Settings,
    connection_options,
    request,
)


def receipt(command="command-a"):
    return httpx.Response(
        202,
        json={
            "state": "accepted",
            "receipt": {"command_id": command, "project_id": "project-a"},
            "detail": "Saved; waiting for its execution owner",
        },
        headers={"X-Nebula-Command-Id": command, "Retry-After": "1"},
    )


def queued(command="command-a"):
    return httpx.Response(202, json={"state": "queued", "command_id": command})


def frame(event, **data):
    return f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n"


@pytest.fixture
def client_for(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(chat_cli.time, "sleep", waits.append)
    monkeypatch.setattr(transport.time, "sleep", waits.append)
    settings = Settings(
        tmp_path / "chat.json",
        {
            "cores": {
                "https://core.example": {
                    "device_id": "fixture",
                    "device_token": "fixture-device",
                    "csrf_token": "fixture-csrf",
                }
            }
        },
        "https://core.example",
    )
    clients = []

    def make(handler):
        client = httpx.Client(
            **connection_options(settings), transport=httpx.MockTransport(handler)
        )
        clients.append(client)
        return client

    yield make, waits
    for client in clients:
        client.close()


def test_first_message_on_a_cold_project_is_saved_not_lost(client_for, capsys):
    make, waits = client_for
    seen = []
    polls = iter(
        [
            queued(),
            httpx.Response(
                200,
                text=frame("started", turn_id="turn-a")
                + frame("delta", delta="Saved answer")
                + frame("done", message={"content": "Saved answer"}),
            ),
        ]
    )

    def handler(request):
        seen.append((request.method, request.url.path, request.url.query))
        if request.method == "POST":
            return receipt()
        return next(polls)

    chat_cli.stream(
        make(handler), method="POST", path="chat/completions", body={"x": 1}
    )
    out = capsys.readouterr().out
    assert "Saved in Core; waiting for its execution owner" in out
    assert "Saved answer" in out
    assert "Connection ended" not in out
    assert [method for method, _, _ in seen] == ["POST", "GET", "GET"]
    assert seen[1][1] == "/api/v1/runtime/requests/command-a/response"
    assert seen[1][2] == b"after=0"
    assert waits == [1.0, 1.0]


def test_unstarted_message_reports_saved_and_is_not_resent(client_for, monkeypatch):
    make, _ = client_for
    monkeypatch.setattr(chat_cli, "RECEIPT_WAIT_SECONDS", 0)
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return receipt()
        return queued()

    with pytest.raises(ClientError, match="has not started it yet. Nothing was resent"):
        chat_cli.stream(make(handler), method="POST", path="chat/completions", body={})
    assert len(posts) == 1


def test_sync_mutations_read_the_saved_result(client_for):
    make, waits = client_for
    seen = []
    polls = iter([queued(), httpx.Response(200, json={"status": "cancelled"})])

    def handler(request):
        seen.append(request.method)
        if request.method == "POST":
            return receipt()
        return next(polls)

    client = make(handler)
    assert request(client, "POST", "chat/turns/turn-a/cancel") == {
        "status": "cancelled"
    }
    assert seen == ["POST", "GET", "GET"]

    expired = make(lambda request: receipt() if request.method == "POST" else queued())
    with pytest.raises(AcceptedRequest) as raised:
        transport.saved_response(
            expired, "command-a", expired.post("chat/turns/turn-a/cancel"), wait=0
        )
    assert raised.value.command_id == "command-a"


def test_other_accepted_responses_and_errors_are_unchanged(client_for):
    make, _ = client_for

    def handler(request):
        if request.url.path.endswith("job"):
            return httpx.Response(202, json={"id": "job-a", "state": "accepted"})
        return httpx.Response(503, json={"detail": "unavailable"})

    client = make(handler)
    assert request(client, "POST", "job") == {"id": "job-a", "state": "accepted"}
    with pytest.raises(ClientError, match="503"):
        chat_cli.stream(client, method="POST", path="chat/completions", body={})
