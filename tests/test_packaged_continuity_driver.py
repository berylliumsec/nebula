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


def test_held_provider_turn_uses_core_pending_status():
    from threading import Event
    from types import SimpleNamespace

    from nebula.v3.domain import ChatTurnStatus
    from nebula.v3.execution_kernel import PendingChatTurnStatus

    pending = PendingChatTurnStatus(
        id="original-turn",
        session_id="original-session",
        status=ChatTurnStatus.ROUTING,
        revision=1,
    )
    reads = []

    def serve(request):
        reads.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=pending.model_dump(mode="json"))

    release = Event()
    chat = SimpleNamespace(error=None, finished=Event())
    with client_for(serve) as client:
        observed = driver.held_provider_turn(
            client,
            chat,
            release,
            [1.0],
            pending.session_id,
            pending.id,
            "before bootstrap window close",
        )

    assert reads == [
        (
            "GET",
            "/api/v1/chat/sessions/original-session/pending-turn",
            {"view": "status"},
        )
    ]
    assert observed == {
        "phase": "before bootstrap window close",
        "turn_id": pending.id,
        "session_id": pending.session_id,
        "provider_executions": 1,
        "status": ChatTurnStatus.ROUTING.value,
    }
    assert not release.is_set() and not chat.finished.is_set()


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


def test_native_project_selection_reveals_navigation_and_avoids_remove_confirmation(
    monkeypatch,
):
    from types import SimpleNamespace

    class Node:
        def __init__(self, name, children=(), showing=True):
            self.name, self.children, self.showing = name, list(children), showing

        @property
        def childCount(self):
            return len(self.children)

        def __getitem__(self, index):
            return self.children[index]

        def getState(self):
            return SimpleNamespace(contains=lambda _state: self.showing)

        def getRoleName(self):
            return "push button"

    project = Node("Packaged continuity fixture active", showing=False)
    remove = Node("Remove project Packaged continuity fixture", showing=False)
    switch = Node("Switch project", showing=False)
    navigation = Node("Open navigation")
    root = Node("app", [navigation, switch, project, remove])
    activated = []

    def activate(node):
        activated.append(node.name)
        if node is navigation:
            navigation.showing, switch.showing = False, True
        elif node is switch:
            project.showing = remove.showing = True

    monkeypatch.setattr(desktop, "activate", activate)
    desktop.select_project(
        root, "Packaged continuity fixture", SimpleNamespace(STATE_SHOWING="showing")
    )
    assert activated == [
        "Open navigation",
        "Switch project",
        "Packaged continuity fixture active",
    ]


def test_release_pair_refuses_relabelled_identical_retained_code():
    baseline = {
        "version": "baseline",
        "core_executable": "bin/core",
        "files": {"bin/core": "same-actual-digest"},
    }
    candidate = {**baseline, "version": "candidate"}
    with pytest.raises(ValueError, match="distinct retained Core bytes"):
        driver.verify_release_pair({"baseline": baseline, "candidate": candidate})


def test_native_provenance_binds_real_sidecar_and_resource_inventory(tmp_path):
    import shutil
    from types import SimpleNamespace

    values, manifests = {}, {}
    for prefix, source in (("baseline", "a" * 40), ("candidate", "b" * 40)):
        layout = tmp_path / prefix
        layout.mkdir()
        executable = layout / "nebula-ui"
        executable.write_bytes((prefix + " native binary").encode())
        core = layout / "connector-input"
        core.write_bytes((prefix + " public onefile connector").encode())
        sibling = layout / "nebula-core"
        shutil.copyfile(core, sibling)
        resources = layout / "resources"
        manifest = make_bundle(resources / "nebula-runtime")
        bundle = layout / "input-bundle"
        shutil.copytree(resources / "nebula-runtime", bundle)
        receipt = {
            "format": "nebula.native-build/v1",
            "source_commit": source,
            "desktop_sha256": driver.digest(executable),
            "connector_sha256": driver.digest(core),
            "bundle_manifest_sha256": driver.digest(bundle / "nebula-runtime.json"),
            "frontend_index_sha256": manifest["files"]["ui/index.html"],
            "resource_directory": str(resources),
        }
        provenance = layout / "build.json"
        provenance.write_text(json.dumps(receipt))
        manifests[prefix] = manifest
        values.update(
            {
                prefix + "_desktop": executable,
                prefix + "_core": core,
                prefix + "_bundle": bundle,
                prefix + "_resources": resources,
                prefix + "_provenance": provenance,
            }
        )
    args = SimpleNamespace(**values)
    assert set(driver.verify_native_provenance(args, manifests)) == {
        "baseline",
        "candidate",
    }
    sidecar = args.baseline_desktop.parent / "nebula-core"
    sidecar.write_bytes(b"unrelated actual sibling sidecar")
    with pytest.raises(ValueError, match="fixed sidecar"):
        driver.verify_native_provenance(args, manifests)
    shutil.copyfile(args.baseline_core, sidecar)
    (args.baseline_resources / "nebula-runtime/ui/index.html").write_bytes(
        b"unrelated actual resource UI"
    )
    with pytest.raises(ValueError, match="inventory differs"):
        driver.verify_native_provenance(args, manifests)


def test_native_bootstrap_observes_desktop_created_release_before_connector(tmp_path):
    import sqlite3

    data = tmp_path / "data"
    (data / "runtime").mkdir(parents=True)
    manifest = {
        "version": "baseline",
        "core_executable": "bin/core",
        "files": {"bin/core": "pending"},
    }
    assert driver.native_bootstrap_identity(data, manifest, "expected-manifest") is None
    installed = data / "runtime/releases/baseline"
    (installed / "bin").mkdir(parents=True)
    (installed / "bin/core").write_bytes(b"actual retained baseline")
    manifest["files"]["bin/core"] = driver.digest(installed / "bin/core")
    (data / "runtime/connection.json").write_text(
        json.dumps(
            {
                "data_root": str(data),
                "pid": os.getpid(),
                "process_birth": driver.process_birth(os.getpid()),
            }
        )
    )
    with sqlite3.connect(data / "nebula.db") as database:
        database.execute(
            "CREATE TABLE runtime_releases (version TEXT, manifest_sha256 TEXT, install_path TEXT)"
        )
        database.execute(
            "INSERT INTO runtime_releases VALUES (?,?,?)",
            ("baseline", "different-manifest", str(installed)),
        )
    with pytest.raises(RuntimeError, match="another retained release"):
        driver.native_bootstrap_identity(data, manifest, "expected-manifest")
    with sqlite3.connect(data / "nebula.db") as database:
        database.execute(
            "UPDATE runtime_releases SET manifest_sha256=?", ("expected-manifest",)
        )
    assert (
        driver.native_bootstrap_identity(data, manifest, "expected-manifest")[
            "retained_core_sha256"
        ]
        == manifest["files"]["bin/core"]
    )


def test_actual_source_pin_and_staged_build_graph_generate_audited_inputs(tmp_path):
    import shutil
    from tests.v3 import packaged_continuity_artifacts as artifacts

    records = {}
    for prefix in ("baseline", "candidate"):
        source = tmp_path / prefix / "source"
        (source / "ui/src-tauri").mkdir(parents=True)
        (source / "src").mkdir()
        (source / "src/build_marker.py").write_text(f"fixture_label = {prefix!r}\n")
        (source / "ui/src-tauri/tauri.conf.json").write_text(
            json.dumps({"build": {"frontendDist": "../dist"}})
        )
        (source / "ui/dist").mkdir()
        (source / "ui/dist/index.html").write_bytes(b"verified UI")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(
            ["git", "-C", str(source), "add", "src", "ui/src-tauri/tauri.conf.json"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Synthetic fixture",
                "-c",
                "user.email=synthetic@example.invalid",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "-qm",
                "Synthetic " + prefix,
            ],
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        layout = tmp_path / prefix / "extracted"
        binary = layout / "usr/bin/nebula-ui"
        binary.parent.mkdir(parents=True)
        binary.write_bytes((prefix + " synthetic native fixture").encode())
        binary.chmod(0o755)
        connector = tmp_path / prefix / "public-connector"
        connector.write_bytes((prefix + " synthetic public connector").encode())
        connector.chmod(0o755)
        shutil.copy2(connector, binary.with_name("nebula-core"))
        resources = layout / "usr/lib/Nebula"
        manifest = make_bundle(resources / "nebula-runtime")
        retained = resources / "nebula-runtime/bin/core"
        retained.write_bytes((prefix + " synthetic retained code").encode())
        manifest["version"] = prefix
        manifest["files"]["bin/core"] = driver.digest(retained)
        manifest_path = resources / "nebula-runtime/nebula-runtime.json"
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.with_name("nebula-runtime.sha256").write_text(
            driver.digest(manifest_path)
        )
        command = tmp_path / prefix / "build-command.json"
        command.write_text(json.dumps(["synthetic-fixture-compiler", str(source)]))
        log = tmp_path / prefix / "build.log"
        log.write_text("Synthetic test graph only; no native compiler executed.\n")
        receipt = tmp_path / prefix / "receipt.json"
        arguments = dict(
            source=source,
            desktop=binary,
            connector=connector,
            resources=resources,
            build_command=command,
            build_log=log,
        )
        with pytest.raises(ValueError, match="differs from its build pin"):
            artifacts.record_build(**arguments, source_commit="0" * 40, output=receipt)
        record = artifacts.record_build(
            **arguments, source_commit=commit, output=receipt
        )
        assert record["source_commit"] == commit
        assert record["connector_sha256"] != manifest["files"]["bin/core"]
        records[prefix] = receipt
    inputs = artifacts.write_inputs(**records, output=tmp_path / "inputs.json")
    assert inputs["baseline_core"].endswith("extracted/usr/bin/nebula-core")
    assert inputs["candidate_resources"].endswith("extracted/usr/lib/Nebula")
    (source / "ui/dist/index.html").write_bytes(b"changed native frontend input")
    with pytest.raises(ValueError, match="frontendDist differs"):
        artifacts.record_build(
            **arguments, source_commit=commit, output=tmp_path / "rejected.json"
        )


def test_reopened_native_view_cannot_pass_by_reselecting_chat_or_rewriting_draft(
    monkeypatch,
):
    calls = []
    expected = {
        "observed_session_id": "selected-older-chat",
        "draft": "Exact unsent older-chat draft.",
    }
    current = {"observed_session_id": None, "draft": ""}

    def native(_args, _child, _environment, operation, **_values):
        calls.append(operation)
        if operation in {"select", "select_and_write"}:
            current.update(expected)  # Manual recovery would conceal the defect.
        return dict(current)

    monkeypatch.setattr(driver, "desktop_observe", native)
    view = driver.NativeChatView(
        None, {"session_id": "selected-older-chat", "draft": expected["draft"]}
    )
    view.initial_setup(None, {})
    with pytest.raises(RuntimeError, match="already completed"):
        view.initial_setup(None, {})
    for _phase in ("candidate", "rollback"):
        current.update(observed_session_id="newer-default-chat", draft="")
        with pytest.raises(RuntimeError, match="initial view did not restore"):
            view.observe(None, {})
        assert current == {"observed_session_id": "newer-default-chat", "draft": ""}
    current.update(expected)
    assert view.observe(None, {}) == expected
    assert calls == ["select_and_write", "observe", "observe", "observe"]


def test_newer_distractor_uses_unstarted_goal_and_authoritative_empty_readback():
    original = {
        "id": "selected-chat",
        "engagement_id": "project",
        "created_at": "2026-11-09T12:00:00Z",
        "updated_at": "2026-11-09T12:00:01Z",
    }
    newer = {
        "id": "newer-chat",
        "engagement_id": "project",
        "created_at": "2026-11-09T12:00:02Z",
        "updated_at": "2026-11-09T12:00:02Z",
    }
    goal = {
        "id": "unstarted-goal",
        "engagement_id": "project",
        "session_id": "newer-chat",
        "status": "draft",
    }
    calls, keys = [], []
    state = {"messages": []}

    def serve(request):
        path = request.url.path.removeprefix("/api/v1")
        calls.append((request.method, path))
        if request.method == "POST":
            assert path == "/chat/goal-conversations"
            keys.append(request.headers["Idempotency-Key"])
            body = json.loads(request.content)
            assert (
                body["provider_id"] == "provider" and body["engagement_id"] == "project"
            )
            assert body["model"] == "continuity-model" and body["completion_criteria"]
            assert "draft-goal" in body["objective"]
            return httpx.Response(201, json={"session": newer, "goal": goal})
        assert request.method == "GET"
        if path == "/chat-sessions/selected-chat":
            return httpx.Response(200, json=original)
        if path == "/chat-sessions/newer-chat":
            return httpx.Response(200, json=newer)
        if path == "/chat/sessions/newer-chat/goal":
            return httpx.Response(200, json=goal)
        if path == "/chat/sessions/newer-chat/messages":
            assert request.url.params["include_replaced"] == "true"
            return httpx.Response(200, json=state["messages"])
        if path == "/chat/sessions/newer-chat/pending-turn":
            return httpx.Response(200, text="null")
        assert path == "/chat-sessions" and request.url.params["recent_first"] == "true"
        return httpx.Response(200, json=[newer, original])

    with client_for(serve) as client:
        created, observed = driver.create_newer_draft_goal(
            client,
            project_id="project",
            provider_id="provider",
            original_session_id="selected-chat",
        )
        assert created["session"]["id"] == "newer-chat"
        assert observed["kind"] == "unstarted_draft_goal"
        assert observed["goal_status"] == "draft" and observed["message_count"] == 0
        assert observed["recent_first_session_id"] == "newer-chat"
        state["messages"] = [{"id": "unexpected-persisted-message"}]
        with pytest.raises(AssertionError, match="empty newer draft-goal"):
            driver.verify_newer_draft_goal(
                client,
                created,
                project_id="project",
                original_session_id="selected-chat",
            )
    assert keys and len(keys) == 1
    assert [path for method, path in calls if method == "POST"] == [
        "/chat/goal-conversations"
    ]
