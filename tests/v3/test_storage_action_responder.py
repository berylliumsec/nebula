"""A public source can verify preview, drift, idempotency, and status lookup."""

from __future__ import annotations

import importlib.util
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SCRIPT = Path(__file__).parents[2] / "examples" / "storage_action_responder.py"
SPEC = importlib.util.spec_from_file_location("storage_action_responder", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "storage_callback_contract", Path(__file__).parents[2] / "examples" / "storage_callback_contract.py")
assert CONTRACT_SPEC and CONTRACT_SPEC.loader
contract = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(contract)


class Backend(module.DemoBackend):
    def __init__(self, root):
        super().__init__(root)
        self.version = 1
        self.runs = 0

    def effects(self, offer_id, incident_id):
        return [{"operation": offer_id, "incident_id": incident_id, "version": self.version}]

    def execute(self, offer_id, effects, key):
        self.runs += 1
        return {"run": self.runs, "key": key}


def test_preview_drift_expiry_and_idempotent_receipt(tmp_path):
    clock = [datetime(2026, 10, 13, tzinfo=timezone.utc)]
    backend = Backend(tmp_path)
    responder = module.ActionResponder(tmp_path / "state.db", backend, now=lambda: clock[0])
    body = {"incident_id": "incident", "action_id": "action"}
    first = responder.preview("copy", body)
    backend.version = 2
    with pytest.raises(ValueError, match="drifted"):
        responder.execute("copy", {**body, "digest": first["digest"], "idempotency_key": "key"})
    second = responder.preview("copy", body)
    executed = responder.execute("copy", {**body, "digest": second["digest"], "idempotency_key": "key"})
    assert executed["status"] == "complete"
    assert responder.execute("copy", {**body, "digest": second["digest"], "idempotency_key": "key"}) == executed
    assert responder.status({"action_id": "action", "idempotency_key": "key"}) == executed
    assert backend.runs == 1
    clock[0] += timedelta(minutes=6)
    with pytest.raises(ValueError, match="expired"):
        responder.execute("copy", {**body, "digest": second["digest"], "idempotency_key": "new-key"})


def test_unknown_execution_is_never_retried(tmp_path):
    backend = Backend(tmp_path)
    responder = module.ActionResponder(tmp_path / "state.db", backend)
    body = {"incident_id": "incident", "action_id": "action"}
    preview = responder.preview("copy", body)

    def fail_after_effect(*args):
        backend.runs += 1
        raise RuntimeError("lost receipt")

    backend.execute = fail_after_effect
    request = {**body, "digest": preview["digest"], "idempotency_key": "key"}
    with pytest.raises(RuntimeError, match="lost receipt"):
        responder.execute("copy", request)
    assert responder.status({"action_id": "action", "idempotency_key": "key"}) == {"status": "unknown"}
    assert responder.execute("copy", request)["status"] == "unknown"
    assert backend.runs == 1


def test_http_contract_requires_bearer(tmp_path):
    responder = module.ActionResponder(tmp_path / "state.db", Backend(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.handler_for(responder, "secret"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/diagnose"
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(Request(url, data=b'{}', method="POST"), timeout=5)
        assert error.value.code == 401
        request = Request(url, data=json.dumps({"incident_id": "i"}).encode(), method="POST",
                          headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.load(response)["offers"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_public_contract_probe_checks_a_disposable_responder(tmp_path):
    backend = Backend(tmp_path)
    responder = module.ActionResponder(tmp_path / "state.db", backend)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.handler_for(responder, "secret"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = contract.run_contract(f"http://127.0.0.1:{server.server_port}", "secret",
                                       "incident", "event")
        assert result["authenticated"] is True
        assert result["changed_digest_rejected"] is True
        assert result["idempotent_receipt"]["run"] == 1
        assert backend.runs == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_callback_probe_does_not_forward_bearer_on_redirect():
    forwarded = []

    class Destination(BaseHTTPRequestHandler):
        def do_POST(self):
            forwarded.append(self.headers.get("Authorization"))
            self.send_response(200); self.end_headers()
            self.wfile.write(b"{}")

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
        with pytest.raises(ValueError, match="redirected"):
            contract.post(f"http://127.0.0.1:{redirect.server_port}", "/v1/diagnose", {}, "secret")
        assert forwarded == []
    finally:
        redirect.shutdown(); redirect.server_close(); redirect_thread.join(timeout=5)
        destination.shutdown(); destination.server_close(); destination_thread.join(timeout=5)
