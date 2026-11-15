"""Serve one fixture Core in this process.

``nebula-core serve`` starts a supervisor whose workers run the retained release
in their own processes, so a fixture can no longer reach the application by
replacing ``cli.create_app``. Fixtures that install inert adapters need them in
the process that answers the API. This builds the application with the
arguments ``serve`` passed before the supervisor, for the options the
Playwright harness sends, and announces its address the same way.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
from pathlib import Path
from typing import Any, Callable

import uvicorn

from nebula.v3 import cli
from nebula.v3.async_utils import call_async
from nebula.v3.knowledge_index import ChromaKnowledgeIndex
from nebula.v3.knowledge_rerank import CrossEncoderReranker
from nebula.v3.runtime_platform import default_runtime_platform


async def _serve(create_app: Callable[..., Any]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--allow-insecure-device-pairing", action="store_true")
    parser.add_argument("--allow-browser-diagnostics", action="store_true")
    args = parser.parse_args()
    root, store, artifacts = cli._services(args.data_dir, runtime_pool=True)
    await store.database.initialize()
    # No reranker model is downloaded for a fixture; retrieval is unchanged.
    knowledge_index = await asyncio.to_thread(
        ChromaKnowledgeIndex,
        root / "knowledge-index",
        reranker=CrossEncoderReranker(root / "knowledge-models", enabled=False),
    )
    app = await call_async(
        create_app,
        store,
        artifact_store=artifacts,
        auth_token=args.token,
        static_dir=args.static_dir,
        tool_platform=default_runtime_platform(
            store=store, artifact_store=artifacts, data_root=root
        ),
        execution_data_root=root,
        knowledge_index=knowledge_index,
        bootstrap_workspace=True,
        allow_browser_diagnostic_events=cli._browser_diagnostic_ingress_allowed(
            args.host,
            handshake_stdout=False,
            allow_remote=args.allow_remote,
            explicitly_enabled=args.allow_browser_diagnostics,
        ),
        allow_insecure_device_pairing=args.allow_insecure_device_pairing,
    )
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.host, args.port))
        listener.listen(128)
        print(
            json.dumps({"url": f"http://{args.host}:{listener.getsockname()[1]}"}),
            flush=True,
        )
        await uvicorn.Server(uvicorn.Config(app, log_level="warning")).serve(
            sockets=[listener]
        )


def serve_fixture(create_app: Callable[..., Any]) -> None:
    """Run ``create_app`` as the Core the harness asked ``serve`` for."""
    asyncio.run(_serve(create_app))
