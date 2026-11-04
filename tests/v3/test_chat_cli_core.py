"""Actual headless Core and terminal processes over paired LAN TLS."""

from __future__ import annotations

import ipaddress
import json
import os
import select
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.fixture
def inert_provider():
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.reply({"data": [{"id": "fixture-model", "max_model_len": 32768}]})

        def reply(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            delayed = any(
                "wait for cancellation" in str(message.get("content", ""))
                for message in body.get("messages", [])
            )
            try:
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for chunk in ("Synthetic ", "Butler answer."):
                        data = {
                            "id": "fixture",
                            "model": "fixture-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": chunk},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                        self.wfile.flush()
                        if delayed:
                            release.wait(20)
                    final = {
                        "id": "fixture",
                        "model": "fixture-model",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 5,
                            "total_tokens": 10,
                        },
                    }
                    self.wfile.write(
                        f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n".encode()
                    )
                else:
                    self.reply(
                        {
                            "id": "fixture",
                            "model": "fixture-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "Synthetic Butler answer.",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 5,
                                "completion_tokens": 5,
                                "total_tokens": 10,
                            },
                        }
                    )
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_actual_headless_core_pairs_streams_reconnects_stops_and_revokes(
    tmp_path, inert_provider
):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        # Route lookup only; no traffic is sent to this documentation address.
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    if ipaddress.ip_address(address).is_loopback:
        pytest.skip("A non-loopback LAN interface is required")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic CLI Core")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address(address)),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "core.pem", tmp_path / "core-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    local, lan = f"https://127.0.0.1:{port}", f"https://{address}:{port}"
    config, data_dir = tmp_path / "terminal" / "chat.json", tmp_path / "core-data"
    # Direct connections only to this test's own LAN and loopback servers.
    bypass = ",".join(
        filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", address])
    )
    env = {
        **os.environ,
        "NO_PROXY": bypass,
        "no_proxy": bypass,
        "NEBULA_V3_API_TOKEN": "synthetic-host-only-token",
        "NEBULA_V3_KNOWLEDGE_RERANKER": "off",
        "NEBULA_CORE_URL": lan,
        "NEBULA_V3_KNOWLEDGE_INDEX_DIR": str(data_dir / "knowledge-index"),
        "NEBULA_V3_KNOWLEDGE_MODELS_DIR": str(data_dir / "knowledge-models"),
    }
    command = [
        sys.executable,
        "-m",
        "nebula.v3.cli",
        "headless",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--allow-remote",
        "--tls-cert",
        str(cert_path),
        "--tls-key",
        str(key_path),
        "--data-dir",
        str(data_dir),
    ]
    log = (tmp_path / "headless.log").open("w+")
    process, client_process = None, None
    admin = httpx.Client(
        base_url=local + "/api/v1/",
        verify=ssl.create_default_context(cafile=str(cert_path)),
        trust_env=False,
        headers={"Authorization": "Bearer synthetic-host-only-token"},
        timeout=10,
    )

    def terminate(candidate):
        if candidate.poll() is not None:
            return True
        candidate.terminate()
        try:
            candidate.wait(timeout=45)
            return True
        except subprocess.TimeoutExpired:
            candidate.kill()
            candidate.wait(timeout=5)
            return False

    def start():
        candidate = subprocess.Popen(command, env=env, stdout=log, stderr=log)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if candidate.poll() is not None:
                log.seek(0)
                pytest.fail(f"Headless Core exited: {log.read()[-5000:]}")
            try:
                if admin.get("health").status_code == 200:
                    return candidate
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        terminate(candidate)
        pytest.fail("Headless Core did not become healthy within 45 seconds")

    def run(*args, text="", alternate_config=None, core=None):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "nebula.chat_cli",
                "--config",
                str(alternate_config or config),
                *(["--core", core] if core else []),
                *args,
            ],
            env=env,
            input=text,
            text=True,
            capture_output=True,
            timeout=30,
        )

    try:
        process = start()
        assert admin.get("/").status_code == 404
        assert (
            httpx.get(
                local + "/api/v1/health",
                verify=ssl.create_default_context(cafile=str(cert_path)),
                trust_env=False,
            ).status_code
            == 401
        )
        offer = run("invite", "--ca-cert", str(cert_path), core=local)
        assert offer.returncode == 0, offer.stdout + offer.stderr
        secret = next(
            line.split(": ", 1)[1]
            for line in offer.stdout.splitlines()
            if line.startswith("Pairing secret:")
        )
        code = next(
            line.split(": ", 1)[1]
            for line in offer.stdout.splitlines()
            if line.startswith("Confirmation code:")
        )
        paired = run(
            "pair", "--ca-cert", str(cert_path), core=lan, text=f"{secret}\n{code}\n"
        )
        assert paired.returncode == 0, paired.stdout + paired.stderr
        assert "synthetic-host-only-token" not in config.read_text()
        replay = run(
            "pair",
            "--ca-cert",
            str(cert_path),
            core=lan,
            text=f"{secret}\n{code}\n",
            alternate_config=tmp_path / "replay.json",
        )
        assert replay.returncode == 1
        assert "invalid or expired" in replay.stderr
        created = run("projects", "--create", "Terminal Project")
        assert created.returncode == 0, created.stdout + created.stderr
        provider_url, release = inert_provider
        profile = admin.post(
            "providers",
            json={
                "name": "Synthetic local provider",
                "provider_type": "vllm",
                "endpoint": provider_url,
                "is_local": True,
                "model_allowlist": ["fixture-model"],
                "metadata": {
                    "default_model": "fixture-model",
                    "options": {"context_window": 32768},
                },
                "capabilities": {
                    "streaming": True,
                    "tool_calling": True,
                },
            },
        )
        assert profile.status_code == 201, profile.text
        chatted = run(
            "chat", "--project", "Terminal Project", text="Hello Butler\n/quit\n"
        )
        assert chatted.returncode == 0, chatted.stdout + chatted.stderr
        assert "Synthetic Butler answer." in chatted.stdout, chatted.stdout
        project = next(
            item
            for item in admin.get("engagements").json()
            if item["name"] == "Terminal Project"
        )
        session = admin.get(
            "chat/control-assistant/session", params={"engagement_id": project["id"]}
        ).json()
        assert session["metadata"]["control_assistant"] is True
        messages_path = f"chat/sessions/{session['id']}/messages"
        before = admin.get(messages_path).json()
        assert [message["content"] for message in before] == [
            "Hello Butler",
            "Synthetic Butler answer.",
        ]
        assert terminate(process), (
            "The synthetic Core required forced termination before restart"
        )
        process = start()
        reconnected = run(
            "chat", "--project", "Terminal Project", text="/status\n/quit\n"
        )
        assert reconnected.returncode == 0, reconnected.stdout + reconnected.stderr
        assert "Synthetic Butler answer." in reconnected.stdout
        assert admin.get(messages_path).json() == before
        client_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "nebula.chat_cli",
                "--config",
                str(config),
                "chat",
                "--project",
                "Terminal Project",
            ],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        client_process.stdin.write("wait for cancellation\n")
        client_process.stdin.flush()
        received = b""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ready, _, _ = select.select([client_process.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(client_process.stdout.fileno(), 8192)
                if not chunk:
                    break
                received += chunk
            if b"You: Butler: Synthetic " in received:
                break
        assert b"You: Butler: Synthetic " in received, received.decode()
        turn = admin.get(f"chat/sessions/{session['id']}/pending-turn").json()
        assert turn and turn["status"] not in {
            "complete",
            "failed",
            "cancelled",
            "interrupted",
        }, turn
        client_process.send_signal(signal.SIGINT)
        output, errors = client_process.communicate("/quit\n", timeout=15)
        output = received.decode() + output
        release.set()
        assert client_process.returncode == 0, output + errors
        assert "cancelled" in output, output
        state = admin.get(f"chat/sessions/{session['id']}/state")
        assert state.status_code == 200, state.text
        observed = state.json()
        assert observed["turn_id"] == turn["id"], observed
        assert observed["execution"] == "cancelled" and observed["busy"] is False, (
            observed
        )
        unpaired = run("unpair")
        assert unpaired.returncode == 0, unpaired.stdout + unpaired.stderr
        assert not json.loads(config.read_text())["cores"]
        assert admin.get("auth/devices").json() == []
        receipt = {
            "origin": lan,
            "entry_point": "nebula-core headless / python -m nebula.chat_cli",
            "build": "candidate Python source; no frontend bundle",
            "fixture": "isolated TLS Core and inert loopback provider",
            "checks": [
                "authenticated headless startup",
                "host invitation",
                "LAN pairing",
                "single-use offer rejection",
                "project creation readback",
                "Butler streaming and durable transcript",
                "Core restart and terminal reconnect",
                "SIGINT cancellation",
                "device revocation",
            ],
            "result": "passed",
        }
        (tmp_path / "cli-lan-receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))
    finally:
        inert_provider[1].set()
        if client_process and client_process.poll() is None:
            terminate(client_process)
        if process and process.poll() is None:
            terminate(process)
        admin.close()
        log.close()
        if (tmp_path / "cli-lan-receipt.json").exists():
            shutil.rmtree(data_dir)
            shutil.rmtree(config.parent)
            cert_path.unlink()
            key_path.unlink()
            (tmp_path / "headless.log").unlink()
