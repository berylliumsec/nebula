"""Two owned frozen-Core journeys: async SQLite and PostgreSQL, never live data."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

import httpx
from sqlalchemy.engine import make_url


class ProviderFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        payload = json.dumps(
            {"data": [{"id": "synthetic-model", "object": "model"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        request = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
        )
        choice = {
            "index": 0,
            "message": {"role": "assistant", "content": "Synthetic response."},
            "finish_reason": "stop",
        }
        body = {
            "id": "synthetic-completion",
            "object": "chat.completion",
            "model": "synthetic-model",
            "choices": [choice],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        }
        if request.get("stream"):
            delta = {
                **body,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "Synthetic response.",
                        },
                        "finish_reason": None,
                    }
                ],
            }
            final = {
                **body,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            payload = (
                "data: "
                + json.dumps(delta)
                + "\n\ndata: "
                + json.dumps(final)
                + "\n\ndata: [DONE]\n\n"
            ).encode()
            content_type = "text/event-stream"
        else:
            payload = json.dumps(body).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def database_url(kind):
    if kind == "sqlite":
        yield None
        return
    import psycopg
    from psycopg import sql

    configured = os.environ.get("NEBULA_TEST_POSTGRES_URL")
    if not configured:
        raise ValueError(
            "The PostgreSQL frozen journey requires NEBULA_TEST_POSTGRES_URL"
        )
    url = make_url(configured)
    schema = "frozen_async_" + uuid4().hex
    # This standalone fixture connection is an auxiliary allocation, not a Core
    # request path. It closes before any frozen runtime pool opens.
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    ) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield url.update_query_dict(
            {"options": "-csearch_path=" + schema}
        ).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(
            url.set(drivername="postgresql").render_as_string(hide_password=False),
            autocommit=True,
        ) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def journey(binary, output, kind, endpoint):
    token = secrets.token_urlsafe(32)
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    log_path = output.with_suffix(f".{kind}.core.log")
    with (
        tempfile.TemporaryDirectory(prefix=f"nebula-frozen-{kind}-") as directory,
        database_url(kind) as url,
        log_path.open("w") as log,
    ):
        root = Path(directory)
        environment = dict(os.environ)
        environment.pop("NEBULA_V3_DATABASE_URL", None)
        environment.update(
            XDG_DATA_HOME=str(root / "data"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_CACHE_HOME=str(root / "cache"),
            XDG_STATE_HOME=str(root / "state"),
            TMPDIR=str(root),
            PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
            NEBULA_V3_KNOWLEDGE_RERANKER="off",
            NEBULA_V3_DB_ADMIN_RESERVE="5",
            NEBULA_V3_DB_OTHER_CONNECTIONS="12",
            NEBULA_V3_DB_PEAK_PROCESSES="2",
            NEBULA_V3_DB_POOL_SIZE="2",
        )
        if url:
            environment["NEBULA_V3_DATABASE_URL"] = url
        process = None
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            trust_env=False,
        )

        def start():
            nonlocal process
            process = subprocess.Popen(
                [
                    str(binary),
                    "serve",
                    "--data-dir",
                    str(root / "core"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--token",
                    token,
                ],
                env=environment,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(
                        f"Owned frozen Core exited at startup: {process.returncode}"
                    )
                try:
                    response = client.get("/api/v1/health")
                    if response.status_code == 200:
                        assert response.json()["dialect"] == kind
                        return response.json()
                except httpx.TransportError:
                    # diagnostic-expected: poll only the owned Core during startup.
                    pass
                time.sleep(0.1)
            raise AssertionError("Owned frozen Core startup timed out")

        def stop():
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    # diagnostic-expected: terminate only this owned process group on failed shutdown.
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                    raise AssertionError("Owned frozen Core shutdown timed out")
                assert process.returncode == 0, process.returncode

        def request(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        try:
            health = start()
            assert (
                httpx.get(
                    client.base_url.join("/api/v1/health"), trust_env=False
                ).status_code
                == 401
            )
            project = request(
                "POST",
                "/api/v1/engagements",
                {"name": "Synthetic frozen async project"},
            )
            provider = request(
                "POST",
                "/api/v1/providers",
                {
                    "name": "Inert local provider",
                    "provider_type": "vllm",
                    "endpoint": endpoint,
                    "is_local": True,
                    "model_allowlist": ["synthetic-model"],
                    "capabilities": {"streaming": True, "tool_calling": False},
                },
            )
            chat = request(
                "POST",
                "/api/v1/chat/sessions",
                {
                    "engagement_id": project["id"],
                    "title": "Frozen async fixture",
                    "provider_profile_id": provider["id"],
                    "model": "synthetic-model",
                },
            )
            completion = request(
                "POST",
                "/api/v1/chat/completions",
                {
                    "engagement_id": project["id"],
                    "session_id": chat["id"],
                    "provider_id": provider["id"],
                    "model": "synthetic-model",
                    "include_knowledge": False,
                    "messages": [
                        {"role": "user", "content": "Return a synthetic response."}
                    ],
                    "stream": False,
                },
            )
            assert completion["message"]["content"] == "Synthetic response.", completion
            with ThreadPoolExecutor(max_workers=8) as workers:
                results = list(
                    workers.map(
                        lambda _: request("GET", "/api/v1/engagements"), range(24)
                    )
                )
            assert all(
                any(row["id"] == project["id"] for row in rows) for rows in results
            )
            performance = request("GET", "/api/v1/diagnostics/performance")
            pool = performance["database_pool"]
            assert pool["max_overflow"] == 0
            assert pool["timeouts"] == pool["connection_failures"] == 0
            assert pool["peak_checked_out"] <= pool["configured_size"]
            if kind == "postgresql":
                assert pool["configured_size"] == 2 <= pool["budget"]["ceiling"]
            stop()
            start()
            messages = request("GET", f"/api/v1/chat/sessions/{chat['id']}/messages")
            assert sum(row["role"] == "user" for row in messages) == 1, messages
            assert (
                sum(
                    row["role"] == "assistant"
                    and row["content"] == "Synthetic response."
                    for row in messages
                )
                == 1
            ), messages
            stop()
            from nebula.v3.knowledge_index import ChromaKnowledgeIndex
            from scripts.build_product_knowledge import source_digest, source_paths

            product = ChromaKnowledgeIndex(
                root / "core" / "knowledge-index"
            )._product_collection
            digest = source_digest(source_paths())
            assert product.metadata["source_digest"] == digest
            assert product.count() > 50
            return {
                "database": kind,
                "status": "passed",
                "build_commit": health["commit"],
                "pool": pool,
                "restarted_transcript": "one user and one assistant message",
                "product_source_digest": digest,
                "product_passages": product.count(),
            }
        finally:
            try:
                stop()
            finally:
                client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixture)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        receipt = {
            "scope": "two frozen-Core async database journeys: authentication, chat, concurrent listings, restart persistence and current Chroma product guidance",
            "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
            "candidate_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "journeys": [],
        }
        for kind in ("sqlite", "postgresql"):
            receipt["journeys"].append(
                journey(
                    args.binary.resolve(),
                    args.output,
                    kind,
                    f"http://127.0.0.1:{server.server_port}/v1",
                )
            )
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
    finally:
        server.shutdown()
        worker.join(timeout=10)
        server.server_close()


if __name__ == "__main__":
    main()
