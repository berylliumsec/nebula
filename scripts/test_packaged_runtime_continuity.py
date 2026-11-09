#!/usr/bin/env python3
"""Exercise real packaged launchers with one isolated, synthetic installation.

Requires two already-built distributions; never installs a package or replaces an
operator application. Both desktop executables must honor NEBULA_V3_DATA_DIR.
The receipt proves the launcher/API workflow below, not physical desktop input.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@contextmanager
def inert_provider():
    release = threading.Event()
    starts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            payload = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "continuity-model",
                            "object": "model",
                            "context_window": 32768,
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                self.send_error(400)
                return
            body = json.loads(self.rfile.read(length))
            if not body.get("stream"):
                payload = json.dumps(
                    {
                        "id": "inert-metadata",
                        "model": "continuity-model",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Synthetic continuity",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            starts.append(time.monotonic())
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for text in ("Before desktop close. ", "After desktop reopen."):
                    payload = {
                        "id": "inert-stream",
                        "model": "continuity-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": None,
                            }
                        ],
                    }
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                    self.wfile.flush()
                    if text.startswith("Before") and not release.wait(600):
                        return
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", starts, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def connect(core: Path, data: Path, environment: dict) -> dict:
    result = subprocess.run(
        [str(core), "desktop-connect", "--data-dir", str(data)],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
        env=environment,
        cwd=Path(environment["HOME"]).parent,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    value = json.loads(lines[-1])
    if (
        value.get("protocol") != "nebula-desktop-runtime-v1"
        or value.get("host") != "127.0.0.1"
    ):
        raise RuntimeError(
            "packaged Core did not return the persistent desktop protocol"
        )
    if (
        not isinstance(value.get("ipc_token"), str)
        or not 1 <= value.get("port", 0) <= 65535
    ):
        raise RuntimeError("invalid desktop connection identity")
    return value


def api(connection: dict) -> httpx.Client:
    return httpx.Client(
        base_url=f"http://{connection['host']}:{connection['port']}/api/v1",
        headers={"Authorization": "Bearer " + connection["ipc_token"]},
        timeout=90,
        trust_env=False,
    )


def require(response: httpx.Response, *codes):
    if response.status_code not in (codes or (200,)):
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:1000]}"
        )
    return response.json()


def command_identity(response: httpx.Response, document=None):
    value = document if document is not None else response.json()
    receipt = value.get("receipt") or {}
    identity = (
        response.headers.get("x-nebula-command-id")
        or value.get("command_id")
        or receipt.get("command_id")
    )
    if not isinstance(identity, str) or not identity:
        raise RuntimeError("accepted response omitted its saved command identity")
    return identity


def completed_response(client, response, *, timeout=90):
    """Read one accepted mutation; never repeat its effect submission."""
    deadline = time.monotonic() + timeout
    identity = None
    while response.status_code == 202:
        observed = command_identity(response)
        if identity is not None and identity != observed:
            raise RuntimeError("saved request identity changed while observing it")
        identity = observed
        status = require(client.get(f"/runtime/requests/{identity}"))
        if status["state"] in {"failed", "uncertain"}:
            raise RuntimeError(
                "saved request needs reconciliation: " + json.dumps(status)
            )
        if time.monotonic() >= deadline:
            raise TimeoutError("saved request did not produce its response")
        response = client.get(f"/runtime/requests/{identity}/response")
        if response.status_code == 202:
            time.sleep(0.05)
    return response


def mutate(client, method, path, body, *, key=None):
    response = client.request(
        method,
        path,
        json=body,
        headers={"Idempotency-Key": key or "packaged-" + uuid4().hex},
    )
    return require(completed_response(client, response), 200, 201)


def operation(client: httpx.Client, action: str, parameters: dict) -> dict:
    identity = action + "-" + uuid4().hex
    value = require(
        client.post(
            "/deployment/operations",
            json={
                "operation_id": identity,
                "action": action,
                "parameters": parameters,
            },
        )
    )
    deadline = time.monotonic() + 90
    while value["state"] == "running":
        if value.get("operation_id") != identity or time.monotonic() >= deadline:
            raise RuntimeError(
                "deployment operation identity or deadline was not confirmed"
            )
        time.sleep(0.05)
        value = require(client.get("/deployment/operations/" + identity))
    if value["state"] != "succeeded":
        raise RuntimeError(f"{action} needs reconciliation: {json.dumps(value)}")
    return value["result"]


def wait_until(predicate, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError(
        "packaged continuity condition was not observed before its deadline"
    )


def wait_ready(client, version, *, timeout=90):
    def ready():
        health = require(client.get("/health"))
        if health.get("api_ready") is not True:
            return None
        status = require(client.get("/deployment/status"))
        return status if status["api_version"] == version else None

    return wait_until(ready, timeout=timeout)


def verify_bundle(bundle):
    bundle = bundle.resolve(strict=True)
    path = bundle / "nebula-runtime.json"
    manifest = json.loads(path.read_bytes())
    expected_manifest = (bundle / "nebula-runtime.sha256").read_text().strip()
    if (
        digest(path) != expected_manifest
        or manifest.get("format") != "nebula.runtime-bundle/v1"
    ):
        raise ValueError("runtime manifest identity is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("runtime bundle has no verified inventory")
    for name, expected in files.items():
        relative = Path(name)
        selected = bundle / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or selected.resolve().is_relative_to(bundle) is False
        ):
            raise ValueError("runtime inventory path leaves its bundle")
        if not selected.is_file() or digest(selected) != expected:
            raise ValueError("runtime inventory differs: " + name)
    relative = manifest["core_executable"]
    if relative not in files or manifest["ui_directory"] + "/index.html" not in files:
        raise ValueError("runtime executable or UI is not in the verified inventory")
    return manifest


def served_ui(client, manifest, phase):
    response = client.get(client.base_url.copy_with(path="/index.html"))
    if response.status_code != 200:
        raise RuntimeError("packaged production UI is unavailable")
    observed = hashlib.sha256(response.content).hexdigest()
    expected = manifest["files"][manifest["ui_directory"] + "/index.html"]
    if observed != expected:
        raise RuntimeError("served production UI differs from its verified bundle")
    return {"phase": phase, "index_sha256": observed, "version": manifest["version"]}


def verify_release_pair(manifests):
    baseline, candidate = (manifests[prefix] for prefix in ("baseline", "candidate"))
    if baseline["version"] == candidate["version"]:
        raise ValueError(
            "baseline and candidate need distinct verified manifest versions"
        )
    if (
        baseline["files"][baseline["core_executable"]]
        == candidate["files"][candidate["core_executable"]]
    ):
        raise ValueError("code-update acceptance needs distinct retained Core bytes")


def verify_native_provenance(args, manifests):
    receipts = {}
    for prefix, manifest in manifests.items():
        receipt = json.loads(getattr(args, prefix + "_provenance").read_bytes())
        if (
            receipt.get("format") != "nebula.native-build/v1"
            or re.fullmatch(r"[0-9a-f]{40}", receipt.get("source_commit", "")) is None
        ):
            raise ValueError("native build provenance is missing its pinned source")
        bundle = getattr(args, prefix + "_bundle")
        desktop = getattr(args, prefix + "_desktop")
        core = getattr(args, prefix + "_core")
        resources = getattr(args, prefix + "_resources").resolve(strict=True)
        expected = {
            "desktop_sha256": digest(desktop),
            "connector_sha256": digest(core),
            "bundle_manifest_sha256": digest(bundle / "nebula-runtime.json"),
            "frontend_index_sha256": manifest["files"][
                manifest["ui_directory"] + "/index.html"
            ],
        }
        if (
            any(receipt.get(name) != value for name, value in expected.items())
            or Path(receipt["resource_directory"]).resolve(strict=True) != resources
        ):
            raise ValueError(
                "native build inputs differ from their actual build receipt"
            )
        sibling = desktop.parent / "nebula-core"
        if (
            sibling.resolve(strict=True).parent != desktop.parent
            or digest(sibling) != expected["connector_sha256"]
        ):
            raise ValueError(
                "native desktop's fixed sidecar differs from its connector input"
            )
        embedded = resources / "nebula-runtime"
        if (
            digest(embedded / "nebula-runtime.json")
            != expected["bundle_manifest_sha256"]
            or verify_bundle(embedded) != manifest
        ):
            raise ValueError(
                "native desktop resource bundle differs from its supplied release"
            )
        receipts[prefix] = receipt
    if receipts["baseline"]["source_commit"] == receipts["candidate"]["source_commit"]:
        raise ValueError("code-update acceptance needs distinct pinned source builds")
    return receipts


def native_bootstrap_identity(data, manifest, manifest_sha256):
    """Observe the desktop-created service before any connector can start one."""
    connection_file = data / "runtime/connection.json"
    if not connection_file.exists():
        return None
    connection = json.loads(connection_file.read_bytes())
    if (
        connection.get("data_root") != str(data)
        or process_birth(connection["pid"]) != connection["process_birth"]
    ):
        raise RuntimeError("native desktop did not establish its owned runtime")
    with sqlite3.connect(f"file:{data / 'nebula.db'}?mode=ro", uri=True) as database:
        row = database.execute(
            "SELECT manifest_sha256,install_path FROM runtime_releases WHERE version=?",
            (manifest["version"],),
        ).fetchone()
    if row is None or row[0] != manifest_sha256:
        raise RuntimeError("native desktop bootstrapped another retained release")
    installed = Path(row[1]).resolve(strict=True)
    if (
        not installed.is_relative_to(data)
        or digest(installed / manifest["core_executable"])
        != manifest["files"][manifest["core_executable"]]
    ):
        raise RuntimeError("native desktop retained another Core executable")
    return {
        "pid": connection["pid"],
        "process_birth": connection["process_birth"],
        "manifest_sha256": row[0],
        "retained_core_sha256": manifest["files"][manifest["core_executable"]],
    }


def fixture_environment(output, bundle):
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "AT_SPI_BUS_ADDRESS",
        "GTK_MODULES",
        "GTK_A11Y",
        "GDK_BACKEND",
        "LIBGL_ALWAYS_SOFTWARE",
        "WEBKIT_INSPECTOR_SERVER",
    }
    environment = {name: value for name, value in os.environ.items() if name in allowed}
    for name, relative in {
        "HOME": "home",
        "XDG_DATA_HOME": "desktop-profile",
        "XDG_CONFIG_HOME": "desktop-config",
        "XDG_CACHE_HOME": "desktop-cache",
    }.items():
        folder = output / relative
        folder.mkdir(mode=0o700)
        environment[name] = str(folder)
    environment.update(
        NEBULA_V3_DATA_DIR=str(output / "synthetic-core"),
        NEBULA_RUNTIME_BUNDLE=str(bundle),
        PYINSTALLER_RESET_ENVIRONMENT="1",
    )
    return environment


def process_birth(pid):
    """Linux kernel incarnation; no PID-only cleanup authorization."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"linux:{boot}:{fields[19]}"
    except FileNotFoundError:
        return None  # diagnostic-expected: this incarnation has exited.


def registered_processes(data):
    with sqlite3.connect(f"file:{data / 'nebula.db'}?mode=ro", uri=True) as database:
        rows = [
            dict(
                zip(
                    ("runtime_id", "role", "pid", "process_birth", "state"),
                    row,
                    strict=True,
                )
            )
            for row in database.execute(
                "SELECT runtime_id,role,pid,process_birth,state FROM runtime_processes"
            )
        ]
    connection = json.loads((data / "runtime/connection.json").read_bytes())
    rows.append(
        {
            "runtime_id": "supervisor",
            "role": "supervisor",
            "pid": connection["pid"],
            "process_birth": connection["process_birth"],
            "state": "observed",
        }
    )
    return rows


def retained_attachment_workers(data, project_id):
    """Observe exact live owners without admitting or starting another worker."""
    with sqlite3.connect(f"file:{data / 'nebula.db'}?mode=ro", uri=True) as database:
        owner = database.execute(
            "SELECT o.fencing_token,p.runtime_id,p.version,p.role,p.pid,p.process_birth "
            "FROM project_ownership AS o JOIN runtime_processes AS p "
            "ON p.runtime_id=o.runtime_id WHERE o.project_id=?",
            (project_id,),
        ).fetchone()
        apis = database.execute(
            "SELECT p.runtime_id,p.version,p.role,p.pid,p.process_birth "
            "FROM deployment_host AS h JOIN runtime_processes AS p "
            "ON p.runtime_id=h.api_runtime_id"
        ).fetchall()
    if owner is None or len(apis) != 1:
        raise AssertionError("bootstrap attachment has no exact Project/API owners")
    columns = ("runtime_id", "version", "role", "pid", "process_birth")
    execution = dict(zip(columns, owner[1:], strict=True))
    api_worker = dict(zip(columns, apis[0], strict=True))
    for worker, role in ((execution, "execution"), (api_worker, "api")):
        if (
            worker["role"] != role
            or not worker["pid"]
            or not worker["process_birth"]
            or process_birth(worker["pid"]) != worker["process_birth"]
        ):
            raise AssertionError(
                "bootstrap attachment owner is not the exact live worker"
            )
    if owner[0] <= 0:
        raise AssertionError("bootstrap attachment has no admitted Project fence")
    return {
        "project_id": project_id,
        "fencing_token": owner[0],
        "execution": execution,
        "api": api_worker,
    }


def verify_shutdown(receipt, rows):
    if receipt.get("observed_state") != "stopped":
        raise RuntimeError("shutdown did not confirm observed process exit")
    if any(row["pid"] and not row["process_birth"] for row in rows):
        raise RuntimeError("a registered process has no verifiable birth identity")
    survivors = [
        row
        for row in rows
        if row["pid"]
        and row["process_birth"]
        and process_birth(row["pid"]) == row["process_birth"]
    ]
    if survivors:
        raise RuntimeError(
            "registered processes survived shutdown: " + json.dumps(survivors)
        )


def sse_frames(lines):
    data, event = [], None
    for line in lines:
        if not line:
            if data and "\n".join(data) != "[DONE]":
                value = json.loads("\n".join(data))
                if event and "type" not in value:
                    value["type"] = event
                yield value
            data, event = [], None
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event = line[6:].strip()


class AcceptedChat:
    """Exactly one POST; disconnected viewers use only its saved GET routes."""

    def __init__(self, client, body):
        self.client, self.body = client, body
        self.key = "packaged-chat-" + uuid4().hex
        self.command_id = None
        self.frames, self.responses = [], []
        self.cursor, self.error = 0, None
        self.finished = threading.Event()
        self.worker = threading.Thread(target=self.follow, daemon=True)

    def follow(self):
        method, path = "POST", "/chat/completions"
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                try:
                    kwargs = (
                        {"headers": {"Idempotency-Key": self.key}, "json": self.body}
                        if method == "POST"
                        else {"params": {"after": self.cursor}}
                    )
                    with self.client.stream(method, path, **kwargs) as response:
                        self.responses.append(
                            {
                                "method": method,
                                "path": path,
                                "status": response.status_code,
                            }
                        )
                        if response.status_code == 202:
                            response.read()
                            observed = command_identity(response)
                        elif response.status_code == 200 and response.headers.get(
                            "content-type", ""
                        ).startswith("text/event-stream"):
                            observed = response.headers.get("x-nebula-command-id")
                        elif response.status_code in {429, 503} and method == "GET":
                            response.read()
                            observed = self.command_id
                        else:
                            response.read()
                            raise RuntimeError(
                                "saved chat view failed: " + response.text[:1000]
                            )
                        if observed and self.command_id and observed != self.command_id:
                            raise RuntimeError("chat command identity changed")
                        self.command_id = observed or self.command_id
                        if response.status_code == 200:
                            for frame in sse_frames(response.iter_lines()):
                                self.cursor = max(self.cursor, frame.get("sequence", 0))
                                self.frames.append(frame)
                                if frame.get("type") == "error":
                                    raise RuntimeError(
                                        "chat stream error: " + json.dumps(frame)
                                    )
                                if frame.get("type") == "done":
                                    return
                except httpx.TransportError:
                    # diagnostic-expected: API replacement may close a viewer;
                    # recover the saved command, never submit another effect.
                    if self.command_id is None:
                        lookup = self.client.get(
                            "/runtime/requests/lookup",
                            params={"idempotency_key": self.key},
                        )
                        self.command_id = require(lookup)["command_id"]
                if not self.command_id:
                    raise RuntimeError("chat acceptance omitted saved command identity")
                status = require(
                    self.client.get(f"/runtime/requests/{self.command_id}")
                )
                if status["state"] in {"failed", "uncertain"}:
                    raise RuntimeError(
                        "saved chat needs reconciliation: " + json.dumps(status)
                    )
                method, path = "GET", f"/runtime/requests/{self.command_id}/response"
                time.sleep(0.05)
            raise TimeoutError("saved chat did not finish within600s")
        except Exception as error:
            self.error = error
        finally:
            self.finished.set()

    def wait_frame(self, kind, timeout=90):
        def observed():
            if self.error:
                raise self.error
            return next(
                (frame for frame in self.frames if frame.get("type") == kind), None
            )

        return wait_until(observed, timeout=timeout)


def held_provider_turn(client, chat, release, starts, session_id, turn_id, label):
    """Observe the original provider turn without starting another producer."""
    if chat.error:
        raise chat.error
    pending = require(
        client.get(
            f"/chat/sessions/{session_id}/pending-turn",
            params={"view": "status"},
        )
    )
    # The status API exposes ChatTurnStatus; "running" is only its queue state.
    if (
        release.is_set()
        or chat.finished.is_set()
        or len(starts) != 1
        or not pending
        or (pending["id"], pending["session_id"], pending["status"])
        != (turn_id, session_id, "routing")
    ):
        raise AssertionError(
            "original provider turn was not held through " + label
        )
    return {
        "phase": label,
        "turn_id": turn_id,
        "session_id": session_id,
        "provider_executions": len(starts),
        "status": pending["status"],
    }


def desktop_observe(args, child, environment, operation, **values):
    birth = process_birth(child.pid)
    if birth is None:
        raise RuntimeError("native desktop exited before observation")
    result = subprocess.run(
        [sys.executable, str(args.desktop_fixture)],
        input=json.dumps(
            {"operation": operation, "pid": child.pid, "process_birth": birth, **values}
        ),
        capture_output=True,
        text=True,
        timeout=90,
        env=environment,
        cwd=args.output,
    )
    (args.output / f"desktop-{operation}-{child.pid}.stderr").write_text(result.stderr)
    if result.returncode:
        raise RuntimeError("native GUI observation failed: " + result.stderr[-1500:])
    value = json.loads(result.stdout)
    if (
        value.get("pid") != child.pid
        or value.get("process_birth") != birth
        or not value.get("window_id")
    ):
        raise RuntimeError("GUI fixture did not confirm the exact owned window")
    if operation == "close":
        child.wait(timeout=30)
        if (
            value.get("close_event") != "WM_DELETE_WINDOW"
            or value.get("window_closed") is not True
            or child.returncode != 0
        ):
            raise RuntimeError("native window close did not complete normally")
    return value


class NativeChatView:
    """Allow setup once; a reopened window must restore its initial view itself."""

    def __init__(self, args, view):
        self.args, self.view = args, dict(view)
        self.setup_complete = False

    def _capture(self, child, environment, operation, expected_text=None):
        view = dict(self.view)
        if expected_text is not None:
            view["expected_text"] = expected_text
        observed = desktop_observe(self.args, child, environment, operation, **view)
        if (
            observed.get("observed_session_id") != view["session_id"]
            or observed.get("draft") != view["draft"]
        ):
            raise RuntimeError(
                "native initial view did not restore the selected chat and exact draft"
            )
        return observed

    def initial_setup(self, child, environment):
        if self.setup_complete:
            raise RuntimeError("initial native chat setup already completed")
        observed = self._capture(child, environment, "select_and_write")
        self.setup_complete = True
        return observed

    def observe(self, child, environment, *, expected_text=None):
        if not self.setup_complete:
            raise RuntimeError("initial native chat setup has not completed")
        return self._capture(child, environment, "observe", expected_text)


def verify_newer_draft_goal(client, created, *, project_id, original_session_id):
    """Prove the distractor is saved, empty, unstarted and ahead of the selection."""
    original = require(client.get(f"/chat-sessions/{original_session_id}"))
    newer = require(client.get(f"/chat-sessions/{created['session']['id']}"))
    goal = require(client.get(f"/chat/sessions/{newer['id']}/goal"))
    messages = require(
        client.get(
            f"/chat/sessions/{newer['id']}/messages",
            params={"include_replaced": "true"},
        )
    )
    pending = require(
        client.get(
            f"/chat/sessions/{newer['id']}/pending-turn", params={"view": "status"}
        )
    )
    recent = require(
        client.get(
            "/chat-sessions",
            params={"engagement_id": project_id, "recent_first": "true", "limit": 20},
        )
    )
    if (
        newer["id"] == original_session_id
        or original["id"] != original_session_id
        or any(item["engagement_id"] != project_id for item in (original, newer, goal))
        or goal["id"] != created["goal"]["id"]
        or goal["session_id"] != newer["id"]
        or goal["status"] != "draft"
        or messages != []
        or pending is not None
        or not all(
            datetime.fromisoformat(newer[field])
            > datetime.fromisoformat(original[field])
            for field in ("created_at", "updated_at")
        )
        or not recent
        or recent[0]["id"] != newer["id"]
        or original_session_id not in {item["id"] for item in recent}
    ):
        raise AssertionError("fixture did not establish an empty newer draft-goal chat")
    return {
        "kind": "unstarted_draft_goal",
        "session_id": newer["id"],
        "goal_id": goal["id"],
        "goal_status": goal["status"],
        "message_count": len(messages),
        "created_at": newer["created_at"],
        "updated_at": newer["updated_at"],
        "selected_created_at": original["created_at"],
        "selected_updated_at": original["updated_at"],
        "recent_first_session_id": recent[0]["id"],
    }


def create_newer_draft_goal(client, *, project_id, provider_id, original_session_id):
    # Generic session POST is intentionally unsupported. This public action
    # saves a conversation and a draft goal without starting provider work.
    created = mutate(
        client,
        "POST",
        "/chat/goal-conversations",
        {
            "engagement_id": project_id,
            "provider_id": provider_id,
            "model": "continuity-model",
            "objective": "Newer synthetic draft-goal conversation",
            "completion_criteria": ["Remain an unstarted, empty fixture draft"],
        },
    )
    return created, verify_newer_draft_goal(
        client, created, project_id=project_id, original_session_id=original_session_id
    )


def run(args) -> dict:
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifests = {
        prefix: verify_bundle(getattr(args, prefix + "_bundle"))
        for prefix in ("baseline", "candidate")
    }
    verify_release_pair(manifests)
    if "chat.accept/v1" not in manifests["baseline"].get("features", []):
        raise ValueError(
            "held-turn setup requires the baseline's typed chat acceptance"
        )
    native_provenance = verify_native_provenance(args, manifests)
    from scripts.package_audit import (
        inspect_pyinstaller_binary,
        inspect_pyinstaller_directory,
    )

    archive_audits = {}
    for prefix, manifest in manifests.items():
        runtime_executable = (
            getattr(args, prefix + "_bundle") / manifest["core_executable"]
        )
        archive_audits[prefix] = {
            "connector": inspect_pyinstaller_binary(getattr(args, prefix + "_core")),
            "retained_runtime": inspect_pyinstaller_directory(
                runtime_executable.parent, runtime_executable.name
            ),
        }
    data = args.output / "synthetic-core"
    environment = fixture_environment(args.output, args.baseline_bundle)
    environments = {
        prefix: {
            **environment,
            "NEBULA_RUNTIME_BUNDLE": str(getattr(args, prefix + "_bundle")),
        }
        for prefix in manifests
    }
    cache = getattr(args, "public_model_cache", None)
    if cache is not None:
        destination = (
            Path(environment["HOME"]) / ".cache/chroma/onnx_models/all-MiniLM-L6-v2"
        )
        shutil.copytree(cache, destination)
    desktops, clients, log_handles, observations = [], [], [], []
    stopped = False
    chat = None
    failure = None

    def open_desktop(prefix, label):
        output = (args.output / (label + ".log")).open("wb")
        log_handles.append(output)
        child = subprocess.Popen(
            [str(getattr(args, prefix + "_desktop"))],
            env=environments[prefix],
            cwd=args.output,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        desktops.append((child, environments[prefix]))
        observations.append(
            {
                **desktop_observe(args, child, environments[prefix], "ready"),
                "phase": label,
            }
        )
        return child

    def close_desktop(child, prefix, *, phase=None):
        observed = desktop_observe(args, child, environments[prefix], "close")
        observations.append({**observed, **({"phase": phase} if phase else {})})

    try:
        with inert_provider() as (provider_url, starts, release):
            desktop = open_desktop("baseline", "baseline-bootstrap-desktop")
            native_start = wait_until(
                lambda: native_bootstrap_identity(
                    data,
                    manifests["baseline"],
                    digest(args.baseline_bundle / "nebula-runtime.json"),
                ),
                timeout=90,
            )
            first = connect(args.baseline_core, data, environments["baseline"])
            client = api(first)
            clients.append(client)
            original_status = wait_ready(client, manifests["baseline"]["version"])
            ui_observations = [served_ui(client, manifests["baseline"], "baseline")]
            with httpx.Client(trust_env=False) as anonymous:
                if anonymous.get(str(client.base_url) + "health").status_code != 401:
                    raise AssertionError(
                        "normal packaged authentication was not enforced"
                    )
            original_identity = require(client.get("/runtime/identity"))
            project = mutate(
                client,
                "POST",
                "/engagements",
                {
                    "name": "Packaged continuity fixture",
                    "status": "active",
                    "metadata": {},
                },
            )
            provider = mutate(
                client,
                "POST",
                "/providers",
                {
                    "name": "Inert local continuity provider",
                    "provider_type": "vllm",
                    "endpoint": provider_url,
                    "is_local": True,
                    "enabled": True,
                    "model_allowlist": ["continuity-model"],
                    "metadata": {
                        "default_model": "continuity-model",
                        "options": {"context_window": 32768},
                    },
                },
            )

            # Global SQL creates do not admit a project's execution owner. Let
            # this read observe admission before negotiating typed chat; a first
            # chat sent without an owner uses legacy HTTP delivery with no binding.
            def project_ready():
                response = client.get(
                    "/chat-sessions", params={"engagement_id": project["id"]}
                )
                if response.status_code == 503:
                    return False
                if require(response) != []:
                    raise AssertionError(
                        "new fixture project already has conversations"
                    )
                return True

            wait_until(project_ready, timeout=90)
            prompt = "Complete this one synthetic continuity check."
            draft = "Unsent packaged draft stays on this device."
            chat = AcceptedChat(
                client,
                {
                    "provider_id": provider["id"],
                    "engagement_id": project["id"],
                    "model": "continuity-model",
                    "include_knowledge": False,
                    "stream": True,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            chat.worker.start()
            chat.wait_frame("delta")
            binding = require(client.get(f"/runtime/requests/{chat.command_id}"))
            session_id, turn_id = binding["session_id"], binding["turn_id"]
            session = require(client.get(f"/chat-sessions/{session_id}"))
            view = {
                "project_name": project["name"],
                "session_id": session_id,
                "session_title": session["title"],
                "prompt": prompt,
                "draft": draft,
                "expected_text": "Before desktop close.",
            }

            def assert_held(label):
                return held_provider_turn(
                    client, chat, release, starts, session_id, turn_id, label
                )

            held = []

            def bootstrap_attachment_observation(phase):
                core = native_bootstrap_identity(
                    data,
                    manifests["baseline"],
                    digest(args.baseline_bundle / "nebula-runtime.json"),
                )
                identity = require(client.get("/runtime/identity"))
                workers = retained_attachment_workers(data, project["id"])
                if core != native_start or identity != original_identity:
                    raise AssertionError(
                        "bootstrap window changed the retained Core identity"
                    )
                held.append(assert_held(phase))
                return {
                    "phase": phase,
                    "core": core,
                    "identity": identity,
                    "workers": workers,
                }

            # The first window must create Core before the HTTP fixture can seed
            # its Project. Start the UI again after seeding instead of assuming
            # a browser Ctrl+R shortcut refreshes the native webview/catalog.
            before_bootstrap_close = bootstrap_attachment_observation(
                "before bootstrap window close"
            )
            close_desktop(desktop, "baseline", phase="bootstrap before chat setup")
            bootstrap_closed = bootstrap_attachment_observation(
                "bootstrap window closed"
            )
            if bootstrap_closed["workers"] != before_bootstrap_close["workers"]:
                raise AssertionError(
                    "bootstrap close changed the retained Project/API owners"
                )
            # Observe the closed-window state before any connector or new window
            # could start a replacement for a service that incorrectly exited.
            desktop = open_desktop("baseline", "baseline-desktop")
            bootstrap_reopened = bootstrap_attachment_observation(
                "bootstrap reopened before chat setup"
            )
            if bootstrap_reopened["workers"] != before_bootstrap_close["workers"]:
                raise AssertionError(
                    "bootstrap reopen replaced the retained Project/API owners"
                )
            if connect(args.baseline_core, data, environments["baseline"]) != first:
                raise AssertionError(
                    "bootstrap reopen changed the stable desktop connection"
                )
            bootstrap_attachment = {
                "before_close": before_bootstrap_close,
                "closed": bootstrap_closed,
                "reopened": bootstrap_reopened,
                "connection_unchanged": True,
            }
            native_chat = NativeChatView(args, view)
            observations.append(
                native_chat.initial_setup(desktop, environments["baseline"])
            )
            held.append(assert_held("baseline GUI draft"))

            def accepted_delivery_finished():
                value = require(client.get(f"/runtime/requests/{chat.command_id}"))
                if value["state"] in {"failed", "uncertain"}:
                    raise RuntimeError("original chat acceptance needs reconciliation")
                return value if value["state"] == "succeeded" else None

            acceptance = wait_until(accepted_delivery_finished, timeout=90)
            # Keep a newer saved chat available so restoring sessions[0] cannot
            # masquerade as restoring the selected older conversation.
            newer_draft, newer_observed = create_newer_draft_goal(
                client,
                project_id=project["id"],
                provider_id=provider["id"],
                original_session_id=session_id,
            )
            newer = newer_draft["session"]
            distractors = [{"phase": "before baseline close", **newer_observed}]
            held.append(assert_held("newer unstarted draft-goal creation"))
            observations.append(native_chat.observe(desktop, environments["baseline"]))
            close_desktop(desktop, "baseline")
            held.append(assert_held("baseline window close"))
            candidate_version = manifests["candidate"]["version"]
            staged = operation(
                client,
                "stage",
                {
                    "source": str(args.candidate_bundle),
                    "manifest_sha256": digest(
                        args.candidate_bundle / "nebula-runtime.json"
                    ),
                },
            )
            if staged["version"] != candidate_version:
                raise AssertionError("staging returned a different candidate version")
            revision = require(client.get("/deployment/status"))["revision"]
            migration = operation(
                client,
                "migrate",
                {"version": candidate_version, "expected_revision": revision},
            )
            prepared = operation(client, "prepare_api", {"version": candidate_version})
            revision = require(client.get("/deployment/status"))["revision"]
            operation(
                client,
                "promote_api",
                {"runtime_id": prepared["runtime_id"], "expected_revision": revision},
            )
            upgraded_status = wait_ready(client, candidate_version)
            ui_observations.append(
                served_ui(client, manifests["candidate"], "candidate")
            )
            distractors.append(
                {
                    "phase": "before candidate reopen",
                    **verify_newer_draft_goal(
                        client,
                        newer_draft,
                        project_id=project["id"],
                        original_session_id=session_id,
                    ),
                }
            )
            candidate_desktop = open_desktop("candidate", "candidate-desktop")
            candidate = connect(args.candidate_core, data, environments["candidate"])
            if (
                first != candidate
                or require(client.get("/runtime/identity")) != original_identity
            ):
                raise AssertionError(
                    "candidate reopen changed stable endpoint, principal or Core identity"
                )
            observations.append(
                native_chat.observe(candidate_desktop, environments["candidate"])
            )
            held.append(assert_held("candidate promotion and GUI reopen"))
            # Global creation runs under the currently selected API code. A
            # project PATCH would remain with the original execution worker.
            created = mutate(
                client,
                "POST",
                "/engagements",
                {"name": "Created under packaged candidate"},
            )
            close_desktop(candidate_desktop, "candidate")
            held.append(assert_held("candidate window close"))
            revision = require(client.get("/deployment/status"))["revision"]
            operation(
                client,
                "rollback_code",
                {
                    "version": original_status["api_version"],
                    "expected_revision": revision,
                },
            )
            final_status = wait_ready(client, original_status["api_version"])
            ui_observations.append(served_ui(client, manifests["baseline"], "rollback"))
            distractors.append(
                {
                    "phase": "before rollback reopen",
                    **verify_newer_draft_goal(
                        client,
                        newer_draft,
                        project_id=project["id"],
                        original_session_id=session_id,
                    ),
                }
            )
            rollback_desktop = open_desktop("baseline", "rollback-baseline-desktop")
            rollback = connect(args.baseline_core, data, environments["baseline"])
            if (
                rollback != first
                or require(client.get("/runtime/identity")) != original_identity
            ):
                raise AssertionError(
                    "baseline GUI reopen after rollback changed runtime identity"
                )
            observations.append(
                native_chat.observe(rollback_desktop, environments["baseline"])
            )
            held.append(assert_held("code rollback and baseline GUI reopen"))
            if (
                require(client.get("/engagements/" + created["id"]))["name"]
                != created["name"]
            ):
                raise AssertionError("code rollback lost candidate-code global SQL")
            release.set()
            done = chat.wait_frame("done")
            chat.worker.join(timeout=15)
            if (
                chat.worker.is_alive()
                or len(starts) != 1
                or done.get("session_id") != session_id
            ):
                raise AssertionError(
                    "the original accepted turn did not finish exactly once"
                )
            final_binding = require(client.get(f"/runtime/requests/{chat.command_id}"))
            if (final_binding["session_id"], final_binding["turn_id"]) != (
                session_id,
                turn_id,
            ):
                raise AssertionError(
                    "reconnected delivery changed the original chat or turn"
                )
            messages = require(client.get(f"/chat/sessions/{session_id}/messages"))
            if (
                sum(
                    item.get("role") == "user" and item.get("content") == prompt
                    for item in messages
                )
                != 1
            ):
                raise AssertionError(
                    "GUI/update workflow duplicated the original submission"
                )
            completed = "Before desktop close. After desktop reopen."
            if (
                sum(
                    item.get("role") == "assistant" and item.get("content") == completed
                    for item in messages
                )
                != 1
            ):
                raise AssertionError(
                    "the saved original reply was not complete exactly once"
                )
            observations.append(
                native_chat.observe(
                    rollback_desktop, environments["baseline"], expected_text=completed
                )
            )
            close_desktop(rollback_desktop, "baseline")
            process_rows = registered_processes(data)
            result = subprocess.run(
                [str(args.candidate_core), "runtime-stop", "--data-dir", str(data)],
                capture_output=True,
                text=True,
                timeout=90,
                env=environments["candidate"],
                cwd=args.output,
            )
            (args.output / "shutdown.stderr").write_text(result.stderr)
            (args.output / "shutdown.stdout").write_text(result.stdout)
            if result.returncode:
                raise RuntimeError(
                    "explicit packaged shutdown failed; private data retained"
                )
            shutdown = json.loads(result.stdout)
            verify_shutdown(shutdown, process_rows)
            stopped = True
            receipt = {
                "schema_version": 2,
                "result": "passed",
                "workflow": "real native window close/candidate promotion/automatic older-chat and draft restore/code rollback/baseline GUI restore/held-turn completion/verified shutdown",
                "artifacts": {
                    name: digest(getattr(args, name))
                    for name in (
                        "baseline_core",
                        "candidate_core",
                        "baseline_desktop",
                        "candidate_desktop",
                    )
                },
                "bundles": {
                    prefix: {
                        "version": manifest["version"],
                        "manifest_sha256": digest(
                            getattr(args, prefix + "_bundle") / "nebula-runtime.json"
                        ),
                        "retained_core_sha256": manifest["files"][
                            manifest["core_executable"]
                        ],
                        "ui_index_sha256": manifest["files"][
                            manifest["ui_directory"] + "/index.html"
                        ],
                        "verified_files": len(manifest["files"]),
                    }
                    for prefix, manifest in manifests.items()
                },
                "origin": str(client.base_url),
                "identity": original_identity,
                "versions": {
                    "baseline": original_status["api_version"],
                    "candidate": upgraded_status["api_version"],
                    "rollback": final_status["api_version"],
                },
                "migration": migration,
                "provider_executions": len(starts),
                "session_id": session_id,
                "newer_session_id": newer["id"],
                "newer_draft_goal_observations": distractors,
                "original_acceptance_state": acceptance["state"],
                "turn_id": turn_id,
                "command_id": chat.command_id,
                "held_observations": held,
                "native_gui": observations,
                "accepted_responses": chat.responses,
                "actual_frames": chat.frames,
                "runtime_processes": process_rows,
                "shutdown_observed_state": shutdown["observed_state"],
                "physical_desktop_input": False,
                "installation": "prebuilt artifacts in owned fixture; no existing installation changed",
            }
            receipt["archive_audits"] = archive_audits
            receipt["served_production_ui"] = ui_observations
            receipt["native_build_provenance"] = native_provenance
            receipt["desktop_created_runtime"] = native_start
            receipt["bootstrap_attachment"] = bootstrap_attachment
            (args.output / "continuity-receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
            return receipt
    except Exception as error:
        failure = {
            "result": "failed",
            "error_type": type(error).__name__,
            "detail": str(error),
            "native_gui": observations,
            "accepted_responses": chat.responses if chat else [],
            "actual_frames": chat.frames if chat else [],
        }
        raise
    finally:
        cleanup_errors = []
        for child, selected_environment in desktops:
            if child.poll() is None:
                try:
                    desktop_observe(args, child, selected_environment, "close")
                except Exception as error:
                    cleanup_errors.append("GUI cleanup: " + str(error))
                    # Fixture-only failure cleanup, never GUI-close evidence.
                    child.terminate()
                    child.wait(timeout=15)
        if data.exists() and not stopped:
            try:
                rows = registered_processes(data)
                cleanup = subprocess.run(
                    [str(args.candidate_core), "runtime-stop", "--data-dir", str(data)],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    env=environments["candidate"],
                    cwd=args.output,
                )
                (args.output / "cleanup.stderr").write_text(cleanup.stderr)
                (args.output / "cleanup.stdout").write_text(cleanup.stdout)
                if cleanup.returncode:
                    raise RuntimeError(
                        "fallback runtime-stop returned " + str(cleanup.returncode)
                    )
                verify_shutdown(json.loads(cleanup.stdout), rows)
                stopped = True
            except Exception as error:
                cleanup_errors.append("Runtime cleanup unconfirmed: " + str(error))
        for client in clients:
            client.close()
        for output in log_handles:
            output.close()
        supervisor_log = data / "runtime/supervisor.log"
        if supervisor_log.is_file():
            text = supervisor_log.read_text(errors="replace")
            if "first" in locals():
                text = text.replace(first["ipc_token"], "<redacted fixture credential>")
            (args.output / "supervisor.log").write_text(text)
        if failure is not None:
            failure["cleanup_confirmed"] = stopped
            failure["cleanup_errors"] = cleanup_errors
            (args.output / "continuity-failure.json").write_text(
                json.dumps(failure, indent=2) + "\n"
            )
        # Keep private diagnostic state whenever exact process exit is unknown.
        if stopped:
            for selected in (
                data,
                *(
                    Path(environment[key])
                    for key in (
                        "HOME",
                        "XDG_DATA_HOME",
                        "XDG_CONFIG_HOME",
                        "XDG_CACHE_HOME",
                    )
                ),
            ):
                if selected.exists():
                    for directory, _children, _files in os.walk(selected):
                        Path(directory).chmod(0o700)
                    shutil.rmtree(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "baseline-core",
        "candidate-core",
        "baseline-desktop",
        "candidate-desktop",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--baseline-bundle", type=Path, required=True)
    parser.add_argument("--candidate-bundle", type=Path, required=True)
    for prefix in ("baseline", "candidate"):
        parser.add_argument("--" + prefix + "-provenance", type=Path, required=True)
        parser.add_argument("--" + prefix + "-resources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--desktop-fixture",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "tests/v3/packaged_continuity_desktop_fixture.py",
    )
    parser.add_argument("--public-model-cache", type=Path)
    args = parser.parse_args()
    for name in (
        "baseline_core",
        "candidate_core",
        "baseline_desktop",
        "candidate_desktop",
    ):
        selected = getattr(args, name)
        if (
            selected.is_symlink()
            or not selected.is_file()
            or not os.access(selected, os.X_OK)
        ):
            parser.error(name + " must name an existing executable artifact")
        setattr(args, name, selected.resolve())
    for prefix in ("baseline", "candidate"):
        bundle = getattr(args, prefix + "_bundle").resolve()
        setattr(args, prefix + "_bundle", bundle)
        for suffix in ("provenance", "resources"):
            setattr(
                args,
                prefix + "_" + suffix,
                getattr(args, prefix + "_" + suffix).resolve(strict=True),
            )
    args.desktop_fixture = args.desktop_fixture.resolve(strict=True)
    if args.public_model_cache is not None:
        args.public_model_cache = args.public_model_cache.resolve(strict=True)
    run(args)


if __name__ == "__main__":
    main()
