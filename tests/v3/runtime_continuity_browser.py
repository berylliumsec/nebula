"""LAN acceptance controller; each browser case gets a fresh real installation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile

if __package__ in {None, ""}:
    checkout = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(checkout), str(checkout / "src")]

from fastapi import FastAPI, HTTPException, Request
import uvicorn

from nebula.v3.domain import ChatSession
from nebula.v3.release_store import ReleaseStore
from tests.v3.runtime_continuity_bundle import build_bundle, remove_owned_tree, retained_bundle
from tests.v3.runtime_continuity_fixture import ContinuityFixture


async def serve(root: Path):
    host = os.environ.get("NEBULA_MODEL_TEST_HOST", "127.0.0.1")
    port = int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19489"))
    auth_file = Path(os.environ["NEBULA_MODEL_TEST_AUTH_FILE"])
    token = secrets.token_urlsafe(32)
    source = Path(os.environ.get("NEBULA_ACCEPTANCE_SOURCE", str(Path(__file__).resolve().parents[2] / "src")))
    ui = Path(os.environ.get("NEBULA_ACCEPTANCE_UI", str(Path(__file__).resolve().parents[2] / "ui" / "dist")))
    dependencies = Path(os.environ.get("NEBULA_ACCEPTANCE_DEPENDENCIES", "/workspace/nebula/.venv/lib/python3.12/site-packages"))
    retained = ReleaseStore(root / "retained")
    bundles = []
    for version in ("acceptance-v1", "acceptance-v2"):
        replaceable = root / ("replaceable-" + version)
        built = await asyncio.to_thread(build_bundle, replaceable, version=version,
                                        source=source, ui=ui, dependencies=dependencies,
                                        runtime_template=bundles[0].path if bundles else None)
        bundles.append(await asyncio.to_thread(retained_bundle, built, retained))
        await asyncio.to_thread(remove_owned_tree, replaceable)
    fixture = None
    serial = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if fixture is not None:
                await fixture.close()
            auth_file.unlink(missing_ok=True)

    app = FastAPI(lifespan=lifespan)

    def authorize(request):
        if request.headers.get("authorization") != "Bearer " + token:
            raise HTTPException(401)

    @app.get("/health")
    async def health():
        return {"ready": True, "fixture": "source-runtime-continuity"}

    @app.post("/reset")
    async def reset(request: Request):
        nonlocal fixture
        authorize(request)
        body = await request.json()
        async with serial:
            if fixture is not None:
                await fixture.close()
            selected = root / ("installation-" + secrets.token_hex(8))
            selected.mkdir()
            fixture = ContinuityFixture(selected / "data", selected / "core.db", bundles,
                                        host=host, port=port)
            await fixture.initialize(cold=body.get("cold", False), pairing=True)
            await fixture.store.create(ChatSession(
                id="continuity-chat", engagement_id="continuity-project",
                title="Synthetic continuity conversation", provider_profile_id="continuity-provider",
                model="model-a", metadata={"initial_title_state": "operator"},
            ))
            if not body.get("cold", False):
                await fixture.ready()
            return {"token": fixture.token, "index_sha256": bundles[0].index_sha256,
                    "source_sha256": bundles[0].source_sha256,
                    "session_id": "continuity-chat", "origin": fixture.origin}

    @app.post("/control")
    async def control(request: Request):
        authorize(request)
        if fixture is None:
            raise HTTPException(409, "reset the isolated fixture first")
        body = await request.json()
        operation = body.pop("operation")
        if operation in {"hold", "release"}:
            if body.get("name") not in {"hold-provider"}:
                raise HTTPException(400, "unknown fixture barrier")
            getattr(fixture, operation)(body["name"])
            return {"done": True}
        if operation == "restart-supervisor":
            await fixture.kill_supervisor()
            await fixture.start()
            return {"restarted": True}
        return await fixture.control(operation, **body)

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps({"token": token, "control_origin": f"http://{host}:{port + 1}"}))
    auth_file.chmod(0o600)
    await uvicorn.Server(uvicorn.Config(app, host=host, port=port + 1, log_level="warning")).serve()


def main():
    with tempfile.TemporaryDirectory(prefix="nebula-continuity-lan-") as directory:
        def stop(_signum, _frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        asyncio.run(serve(Path(directory)))


if __name__ == "__main__":
    main()
