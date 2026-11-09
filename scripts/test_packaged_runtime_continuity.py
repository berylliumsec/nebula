#!/usr/bin/env python3
"""Exercise real packaged launchers with one isolated, synthetic installation.

Requires two already-built distributions; never installs a package or replaces an
operator application. Both desktop executables must honor NEBULA_V3_DATA_DIR.
The receipt proves the launcher/API workflow below, not physical desktop input.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from uuid import uuid4

import httpx


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
            payload = json.dumps({"object": "list", "data": [{"id": "continuity-model", "object": "model", "context_window": 32768}]}).encode()
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
                payload = json.dumps({"id": "inert-metadata", "model": "continuity-model", "choices": [{"message": {"role": "assistant", "content": "Synthetic continuity"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
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
                    payload = {"id": "inert-stream", "model": "continuity-model", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                    self.wfile.flush()
                    if text.startswith("Before") and not release.wait(120):
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
    result = subprocess.run([str(core), "desktop-connect", "--data-dir", str(data)],
                            check=True, capture_output=True, text=True, timeout=90, env=environment)
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    value = json.loads(lines[-1])
    if value.get("protocol") != "nebula-desktop-runtime-v1" or value.get("host") != "127.0.0.1":
        raise RuntimeError("packaged Core did not return the persistent desktop protocol")
    if not isinstance(value.get("ipc_token"), str) or not 1 <= value.get("port", 0) <= 65535:
        raise RuntimeError("invalid desktop connection identity")
    return value


def api(connection: dict) -> httpx.Client:
    return httpx.Client(base_url=f"http://{connection['host']}:{connection['port']}/api/v1",
                        headers={"Authorization": "Bearer " + connection["ipc_token"]},
                        timeout=90, trust_env=False)


def require(response: httpx.Response, *codes):
    if response.status_code not in (codes or (200,)):
        raise RuntimeError(f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:1000]}")
    return response.json()


def operation(client: httpx.Client, action: str, parameters: dict) -> dict:
    value = require(client.post("/deployment/operations", json={
        "operation_id": action + "-" + uuid4().hex,
        "action": action, "parameters": parameters,
    }))
    if value["state"] != "succeeded":
        raise RuntimeError(f"{action} was not confirmed: {value['state']}")
    return value["result"]


def wait_until(predicate, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError("packaged continuity condition was not observed before its deadline")


def run(args) -> dict:
    # mkdir(exist_ok=False) establishes ownership before any process or write.
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    data = args.output / "synthetic-core"
    environment = {name: value for name, value in os.environ.items()
                   if name in {"HOME", "PATH", "LANG", "LC_ALL", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"}}
    environment["NEBULA_V3_DATA_DIR"] = str(data)
    environment["XDG_DATA_HOME"] = str(args.output / "desktop-profile")
    environment["XDG_CONFIG_HOME"] = str(args.output / "desktop-config")
    environment["XDG_CACHE_HOME"] = str(args.output / "desktop-cache")
    desktops = []
    clients = []
    log_handles = []
    stopped = False

    def open_desktop(path, label):
        output = (args.output / (label + ".log")).open("wb")
        log_handles.append(output)
        child = subprocess.Popen([str(path)], env=environment, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        desktops.append(child)
        return child

    try:
        with inert_provider() as (provider_url, starts, release):
            desktop = open_desktop(args.baseline_desktop, "baseline-desktop")
            first = connect(args.baseline_core, data, environment)
            client = api(first)
            clients.append(client)
            original_identity = require(client.get("/runtime/identity"))
            original_status = require(client.get("/deployment/status"))
            project = require(client.post("/engagements", json={"name": "Packaged continuity fixture", "status": "active", "metadata": {}}), 200, 201)
            provider = require(client.post("/providers", json={
                "name": "Inert local continuity provider", "provider_type": "vllm", "endpoint": provider_url,
                "is_local": True, "enabled": True, "model_allowlist": ["continuity-model"],
                "metadata": {"default_model": "continuity-model", "options": {"context_window": 32768}},
            }), 200, 201)
            events = queue.Queue()
            key = "packaged-continuity-" + uuid4().hex

            def submit():
                try:
                    with client.stream("POST", "/chat/completions", headers={"Idempotency-Key": key}, json={
                        "provider_id": provider["id"], "engagement_id": project["id"], "model": "continuity-model",
                        "include_knowledge": False, "stream": True,
                        "messages": [{"role": "user", "content": "Complete this one synthetic continuity check."}],
                    }) as response:
                        events.put(("response", response.status_code, response.headers.get("x-nebula-command-id")))
                        for line in response.iter_lines():
                            if line.startswith("data: "):
                                events.put(("frame", json.loads(line[6:])))
                except Exception as error:
                    events.put(("transport", type(error).__name__))

            submission = threading.Thread(target=submit, daemon=True)
            submission.start()
            frame = None
            command_id = None
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                item = events.get(timeout=max(.1, deadline - time.monotonic()))
                if item[0] == "response":
                    if item[1] not in {200, 202}:
                        raise RuntimeError("packaged chat submission was not accepted")
                    command_id = item[2]
                elif item[0] == "frame" and item[1].get("type") == "delta":
                    frame = item[1]
                    break
            if frame is None or command_id is None:
                raise RuntimeError("no durable streamed acceptance before closing desktop")
            desktop.terminate()
            desktop.wait(timeout=15)
            require(client.get("/deployment/status"))
            candidate_manifest = json.loads((args.candidate_bundle / "nebula-runtime.json").read_text())
            operation(client, "stage", {"source": str(args.candidate_bundle),
                      "manifest_sha256": digest(args.candidate_bundle / "nebula-runtime.json")})
            prepared = operation(client, "prepare_api", {"version": candidate_manifest["version"]})
            current_status = require(client.get("/deployment/status"))
            operation(client, "promote_api", {"runtime_id": prepared["runtime_id"],
                      "expected_revision": current_status["revision"]})
            candidate_desktop = open_desktop(args.candidate_desktop, "candidate-desktop")
            candidate = connect(args.candidate_core, data, environment)
            if first != candidate:
                raise AssertionError("desktop reopen changed the stable endpoint or authentication token")
            candidate_identity = require(client.get("/runtime/identity"))
            if candidate_identity != original_identity:
                raise AssertionError("desktop update changed Core/principal identity")
            upgraded_status = require(client.get("/deployment/status"))
            if upgraded_status["api_version"] == original_status["api_version"]:
                raise AssertionError("candidate packaged API was not promoted")
            release.set()
            response = client.get(f"/runtime/requests/{command_id}/response?after={frame['sequence']}")
            if response.status_code != 200 or "event: done" not in response.text:
                raise AssertionError("the original accepted turn did not finish after desktop reopen")
            if len(starts) != 1:
                raise AssertionError("desktop update resubmitted provider work")
            submission.join(timeout=10)
            current = require(client.get("/engagements/" + project["id"]))
            saved = client.patch("/engagements/" + project["id"], json={
                "expected_revision": current["revision"], "changes": {"name": "Committed under candidate"},
            }, headers={"Idempotency-Key": "candidate-edit-" + uuid4().hex})
            require(saved, 200, 202)
            wait_until(lambda: require(client.get("/engagements/" + project["id"]))["name"] == "Committed under candidate")
            candidate_desktop.terminate()
            candidate_desktop.wait(timeout=15)
            current_status = require(client.get("/deployment/status"))
            operation(client, "rollback_code", {"version": original_status["api_version"],
                      "expected_revision": current_status["revision"]})
            rollback = connect(args.baseline_core, data, environment)
            if rollback != first:
                raise AssertionError("rollback changed the persistent endpoint")
            final_status = require(client.get("/deployment/status"))
            if final_status["api_version"] != original_status["api_version"]:
                raise AssertionError("baseline launcher did not restore the compatible API")
            if require(client.get("/engagements/" + project["id"]))["name"] != "Committed under candidate":
                raise AssertionError("code rollback lost candidate-era data")
            result = subprocess.run([str(args.candidate_core), "runtime-stop", "--data-dir", str(data)],
                                    check=True, capture_output=True, text=True, timeout=60, env=environment)
            shutdown = json.loads(result.stdout.strip())
            if shutdown.get("state") not in {"succeeded", "stopped"}:
                raise AssertionError("explicit runtime shutdown was not confirmed")

            def endpoint_closed():
                try:
                    client.get("/health", timeout=1)
                    return False
                except httpx.TransportError:
                    return True

            wait_until(endpoint_closed, timeout=30)
            stopped = True
            return {"schema_version": 1, "result": "passed", "workflow": "desktop close/update/reopen/code rollback/explicit shutdown",
                    "artifacts": {name: digest(getattr(args, name)) for name in ("baseline_core", "candidate_core", "baseline_desktop", "candidate_desktop")},
                    "origin": str(client.base_url), "core_id": original_identity["core_id"],
                    "versions": {"baseline": original_status["api_version"], "candidate": upgraded_status["api_version"], "rollback": final_status["api_version"]},
                    "provider_executions": len(starts), "physical_desktop_input": False,
                    "installation": "prebuilt artifact paths; no system package installation performed"}
    finally:
        for child in desktops:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=15)
        if data.exists() and not stopped:
            subprocess.run([str(args.candidate_core), "runtime-stop", "--data-dir", str(data)],
                           capture_output=True, text=True, timeout=60, env=environment)
        for client in clients:
            client.close()
        for output in log_handles:
            output.close()
        # This private tree contains ephemeral credentials; retain only the
        # sanitized receipt and desktop diagnostics after exact runtime stop.
        if stopped:
            for directory, _children, _files in os.walk(data):
                Path(directory).chmod(0o700)
            shutil.rmtree(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline-core", "candidate-core", "baseline-desktop", "candidate-desktop"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--baseline-bundle", type=Path, required=True)
    parser.add_argument("--candidate-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for name in ("baseline_core", "candidate_core", "baseline_desktop", "candidate_desktop"):
        selected = getattr(args, name)
        if selected.is_symlink() or not selected.is_file() or not os.access(selected, os.X_OK):
            parser.error(name + " must name an existing executable artifact")
        setattr(args, name, selected.resolve())
    for prefix in ("baseline", "candidate"):
        bundle = getattr(args, prefix + "_bundle").resolve()
        manifest = json.loads((bundle / "nebula-runtime.json").read_text())
        relative = Path(manifest["core_executable"])
        if relative.is_absolute() or ".." in relative.parts:
            parser.error("bundle Core path must stay inside its artifact")
        expected = manifest["files"][relative.as_posix()]
        if digest(bundle / relative) != expected or digest(getattr(args, prefix + "_core")) != expected:
            parser.error(prefix + " Core does not match its runtime manifest")
        setattr(args, prefix + "_bundle", bundle)
    if digest(args.baseline_core) == digest(args.candidate_core):
        parser.error("baseline and candidate must be distinct built artifacts")
    receipt = run(args)
    (args.output / "continuity-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
