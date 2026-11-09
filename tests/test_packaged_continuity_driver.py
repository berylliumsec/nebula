"""Small wire/ownership contracts for the opt-in packaged continuity journey."""

import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from scripts import test_packaged_runtime_continuity as driver
from tests.v3 import packaged_continuity_desktop_fixture as desktop


def client_for(handler):
    return httpx.Client(
        base_url="http://fixture/api/v1/", transport=httpx.MockTransport(handler)
    )


def test_mutation_follows_accepted_receipt_without_another_submission():
    calls = []
    responses = 0

    def serve(request):
        nonlocal responses
        calls.append(
            (request.method, request.url.path, request.headers.get("Idempotency-Key"))
        )
        if request.method == "POST":
            assert request.headers["Idempotency-Key"] == "owned-create"
            return httpx.Response(202, json={"receipt": {"command_id": "command-one"}})
        if request.url.path.endswith("/response"):
            responses += 1
            return (
                httpx.Response(
                    202, json={"command_id": "command-one", "state": "running"}
                )
                if responses == 1
                else httpx.Response(201, json={"id": "saved-project"})
            )
        return httpx.Response(
            200, json={"command_id": "command-one", "state": "running"}
        )

    with client_for(serve) as client:
        assert driver.mutate(
            client, "POST", "/engagements", {"name": "Synthetic"}, key="owned-create"
        ) == {"id": "saved-project"}
    assert sum(method == "POST" for method, _, _ in calls) == 1
    assert all("command-one" in path for method, path, _ in calls if method == "GET")


@pytest.mark.parametrize("acceptance", ["receipt", "lost-response"])
def test_chat_joins_original_saved_stream_after_receipt_or_lost_response(acceptance):
    calls = []

    def serve(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            if acceptance == "lost-response":
                raise httpx.ReadError("Synthetic lost acceptance", request=request)
            return httpx.Response(202, json={"receipt": {"command_id": "chat-one"}})
        if request.url.path.endswith("/response"):
            frames = [
                {
                    "type": "started",
                    "sequence": 1,
                    "session_id": "session-one",
                    "turn_id": "turn-one",
                },
                {"type": "delta", "sequence": 2, "content": "Synthetic reply"},
                {"type": "done", "sequence": 3, "session_id": "session-one"},
            ]
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "x-nebula-command-id": "chat-one",
                },
                text="".join(
                    "event: " + frame["type"] + "\ndata: " + json.dumps(frame) + "\n\n"
                    for frame in frames
                ),
            )
        return httpx.Response(200, json={"command_id": "chat-one", "state": "running"})

    with client_for(serve) as client:
        chat = driver.AcceptedChat(client, {"messages": []})
        chat.follow()
    assert chat.error is None
    assert chat.command_id == "chat-one" and chat.cursor == 3
    assert sum(method == "POST" for method, _ in calls) == 1
    assert [frame["type"] for frame in chat.frames] == ["started", "delta", "done"]


def test_management_reads_only_the_original_running_operation():
    calls = []
    identity = None

    def serve(request):
        nonlocal identity
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            identity = json.loads(request.content)["operation_id"]
            return httpx.Response(
                200, json={"operation_id": identity, "state": "running"}
            )
        assert request.url.path.endswith("/" + identity)
        return httpx.Response(
            200,
            json={
                "operation_id": identity,
                "state": "succeeded",
                "result": {"version": "candidate"},
            },
        )

    with client_for(serve) as client:
        assert driver.operation(client, "stage", {}) == {"version": "candidate"}
    assert len(calls) == 2 and calls[0][0] == "POST" and calls[1][0] == "GET"


@pytest.mark.parametrize("state", ["uncertain", "failed"])
def test_uncertain_or_failed_mutation_is_reported_without_resubmission(state):
    calls = []

    def serve(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"command_id": "saved-one"})
        return httpx.Response(200, json={"command_id": "saved-one", "state": state})

    with (
        client_for(serve) as client,
        pytest.raises(RuntimeError, match="reconciliation"),
    ):
        driver.mutate(client, "POST", "/providers", {})
    assert calls == ["POST", "GET"]


def test_ready_wait_requires_both_admitted_api_and_exact_version():
    calls = []
    health_calls = 0

    def serve(request):
        nonlocal health_calls
        calls.append(request.url.path)
        if request.url.path.endswith("/health"):
            health_calls += 1
            return httpx.Response(
                200, json={"status": "degraded", "api_ready": health_calls > 1}
            )
        return httpx.Response(
            200, json={"api_version": "old" if health_calls == 2 else "expected"}
        )

    with client_for(serve) as client:
        assert (
            driver.wait_ready(client, "expected", timeout=2)["api_version"]
            == "expected"
        )
    assert health_calls == 3


def test_fixture_environment_uses_owned_home_and_supplied_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/synthetic-unrelated-home")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-never-inherit")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/owned-fixture-bus")
    monkeypatch.setenv("WEBKIT_INSPECTOR_SERVER", "127.0.0.1:54321")
    bundle = tmp_path / "verified-bundle"
    environment = driver.fixture_environment(tmp_path, bundle)
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["NEBULA_RUNTIME_BUNDLE"] == str(bundle)
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/owned-fixture-bus"
    assert environment["WEBKIT_INSPECTOR_SERVER"] == "127.0.0.1:54321"
    assert "OPENAI_API_KEY" not in environment
    assert Path(environment["HOME"]).stat().st_mode & 0o777 == 0o700


def make_bundle(root):
    (root / "bin").mkdir(parents=True)
    (root / "ui").mkdir()
    (root / "bin/core").write_bytes(b"retained onedir executable")
    (root / "ui/index.html").write_bytes(b"verified UI")
    manifest = {
        "format": "nebula.runtime-bundle/v1",
        "version": "synthetic-v1",
        "core_executable": "bin/core",
        "ui_directory": "ui",
        "files": {
            name: driver.digest(root / name) for name in ("bin/core", "ui/index.html")
        },
    }
    (root / "nebula-runtime.json").write_text(json.dumps(manifest))
    (root / "nebula-runtime.sha256").write_text(
        driver.digest(root / "nebula-runtime.json")
    )
    return manifest


def test_runtime_bundle_inventory_does_not_require_connector_hash_equality(tmp_path):
    bundle = tmp_path / "bundle"
    expected = make_bundle(bundle)
    connector = tmp_path / "onefile-connector"
    connector.write_bytes(b"independent public connector archive")
    assert driver.digest(connector) != expected["files"]["bin/core"]
    assert driver.verify_bundle(bundle) == expected


@pytest.mark.parametrize("name", ["bin/core", "ui/index.html"])
def test_runtime_bundle_refuses_changed_executable_or_ui_inventory(tmp_path, name):
    bundle = tmp_path / "bundle"
    make_bundle(bundle)
    (bundle / name).write_bytes(b"changed after manifest")
    with pytest.raises(ValueError, match="inventory differs"):
        driver.verify_bundle(bundle)


def test_shutdown_requires_observed_stop_and_exact_registered_process_exit():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        rows = [{"pid": child.pid, "process_birth": driver.process_birth(child.pid)}]
        with pytest.raises(RuntimeError, match="observed process exit"):
            driver.verify_shutdown({"state": "succeeded"}, rows)
        with pytest.raises(RuntimeError, match="survived shutdown"):
            driver.verify_shutdown({"observed_state": "stopped"}, rows)
        child.terminate()
        child.wait(timeout=5)
        driver.verify_shutdown({"observed_state": "stopped"}, rows)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_native_window_close_rejects_wrong_process_birth_before_x_access(monkeypatch):
    def forbidden():
        raise AssertionError("must not touch any window for a different incarnation")

    monkeypatch.setattr(desktop, "X11", forbidden)
    with pytest.raises(RuntimeError, match="incarnation changed"):
        desktop.run(
            {
                "pid": os.getpid(),
                "process_birth": "synthetic-wrong-birth",
                "operation": "close",
            }
        )
