"""Contract checks for the public, copyable storage sender."""

from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.error import HTTPError

import pytest


def sender_module():
    path = Path(__file__).parents[2] / "examples/storage_sender.py"
    spec = importlib.util.spec_from_file_location("storage_sender_example", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reference_sender_persists_one_event_id_across_retries(tmp_path):
    sender = sender_module()
    path = tmp_path / "observation.json"
    path.write_text(json.dumps({
        "schema_version": 1, "observed_at": "2026-10-13T14:00:00Z",
        "device": {"id": "nas", "label": "NAS"},
        "volume": {"id": "data", "label": "Data"},
        "state": "warning", "free_bytes": 20, "total_bytes": 100,
    }))
    first = sender.prepare(path)
    assert sender.prepare(path) == first
    assert json.loads(first)["event_id"] == json.loads(path.read_text())["event_id"]
    with pytest.raises(ValueError, match="HTTPS"):
        sender.sender_url("http://storage.example/api/v1/storage/events")
    with pytest.raises(ValueError, match="endpoint"):
        sender.sender_url("https://storage.example/api/v1/storage/events?token=leak")


def test_reference_sender_retries_identical_payload_and_requires_ack(monkeypatch):
    sender = sender_module()
    payload = b'{"schema_version":1,"event_id":"same"}'
    requests = []

    class Accepted:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _limit):
            return b'{"accepted":true,"duplicate":false,"current":true}'

    def post(request, timeout):
        assert timeout == 15
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 503, "temporarily unavailable", {}, None)
        return Accepted()

    monkeypatch.setattr(sender, "urlopen", post)
    monkeypatch.setattr(sender.time, "sleep", lambda _: None)
    monkeypatch.setattr(sender.random, "random", lambda: 0.0)
    result = sender.send("https://storage.example/api/v1/storage/events", "secret", payload, attempts=2)
    assert result["accepted"] is True
    assert len(requests) == 2
    assert all(request.data == payload for request in requests)
    assert all(request.get_header("Authorization") == "Bearer secret" for request in requests)


def test_reference_sender_does_not_follow_redirect_with_source_token():
    sender = sender_module()
    forwarded = []

    class Destination(BaseHTTPRequestHandler):
        def do_POST(self):
            forwarded.append(self.headers.get("Authorization"))
            self.send_response(202); self.end_headers()

        def log_message(self, *_):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
    destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/stolen")
            self.end_headers()

        def log_message(self, *_):
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        url = f"http://127.0.0.1:{redirect.server_port}/api/v1/storage/events"
        with pytest.raises(HTTPError) as error:
            sender.send(url, "source-secret", b'{}', attempts=1)
        assert error.value.code == 307
        assert forwarded == []
    finally:
        redirect.shutdown(); redirect.server_close(); redirect_thread.join(timeout=5)
        destination.shutdown(); destination.server_close(); destination_thread.join(timeout=5)
