from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nebula.v3.api import create_app
from nebula.v3.debugger import (
    DebugService,
    DebugStartRequest,
    DebugStartResponse,
    DebuggerError,
)
from nebula.v3.domain import utc_now
from nebula.v3.sandbox import SandboxContainerUser, SandboxWorkspaceAccess
from nebula.v3.storage import NebulaStore


class _Workspace:
    def __init__(self, source: bytes) -> None:
        self.source = source

    def download(self, engagement_id: str, path: str):
        assert engagement_id == "engagement-1"
        assert path == "probe.py"
        return SimpleNamespace(stream=io.BytesIO(self.source))


class _Process:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.writes: list[bytes] = []
        self.terminated = False

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def wait(self) -> int:
        return 0

    async def terminate(self) -> None:
        self.terminated = True


class _Backend:
    def __init__(self) -> None:
        self.process = _Process()
        self.closed = False

    async def run(
        self, process_id: str, command: str, cwd: str, extra_env=None
    ) -> _Process:
        assert process_id.startswith("adapter-")
        assert command == "exec /usr/bin/python3 -m debugpy.adapter"
        assert cwd == "."
        return self.process

    async def close(self) -> None:
        self.closed = True


def _request(source: bytes) -> DebugStartRequest:
    return DebugStartRequest(
        path="probe.py",
        expected_sha256=hashlib.sha256(source).hexdigest(),
        arguments=["--safe"],
    )


def test_debug_request_rejects_non_python_and_escaping_paths() -> None:
    digest = "0" * 64
    for path in ("../probe.py", "/probe.py", "probe.sh", "a\\probe.py"):
        with pytest.raises(ValidationError):
            DebugStartRequest(path=path, expected_sha256=digest)


def test_debug_service_freezes_source_and_read_only_runtime(monkeypatch) -> None:
    async def exercise() -> None:
        source = b"print('debug me')\n"
        backend = _Backend()
        launches = []

        async def start_backend(launch):
            launches.append(launch)
            return backend

        monkeypatch.setattr(
            "nebula.v3.debugger.ContainerRuntimeSession.start", start_backend
        )

        async def resolve(_engagement_id: str):
            return SimpleNamespace(
                runner=object(),
                workspace=Path("/tmp/workspace"),
                image=SimpleNamespace(
                    installed_packages=("python3", "python3-debugpy"),
                    resolved_reference="sha256:" + "1" * 64,
                    digest="sha256:" + "2" * 64,
                ),
            )

        service = DebugService(
            workspace_service=_Workspace(source), runtime_resolver=resolve
        )
        started = await service.start("engagement-1", _request(source))
        assert started.workspace_access == "read-only"
        assert started.network == "none"
        assert launches[0].workspace_access is SandboxWorkspaceAccess.READ
        assert launches[0].container_user is SandboxContainerUser.WORKSPACE_OWNER
        assert launches[0].loopback_only is True
        assert not launches[0].egress_rules

        session = await service.attach(started.session_id, started.websocket_ticket)
        with pytest.raises(DebuggerError, match="already attached"):
            await service.attach(started.session_id, started.websocket_ticket)
        with pytest.raises(DebuggerError, match="already has an active debug session"):
            await service.start("engagement-1", _request(source))

        launch = {
            "seq": 1,
            "type": "request",
            "command": "launch",
            "arguments": {
                "program": "/workspace/probe.py",
                "cwd": "/workspace",
                "args": ["--safe"],
            },
        }
        await service.send(session, launch)
        header, encoded = backend.process.writes[0].split(b"\r\n\r\n", 1)
        assert header == f"Content-Length: {len(encoded)}".encode()
        assert json.loads(encoded) == launch

        denied = {
            **launch,
            "arguments": {**launch["arguments"], "program": "/etc/passwd"},
        }
        with pytest.raises(DebuggerError, match="reviewed file"):
            await service.send(session, denied)

        response = {
            "seq": 2,
            "type": "response",
            "request_seq": 1,
            "success": True,
            "command": "launch",
        }
        body = json.dumps(response).encode()
        backend.process.stdout.feed_data(
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        assert await service.receive(session) == response
        await service.close(started.session_id)
        assert backend.process.terminated
        assert backend.closed

    asyncio.run(exercise())


def test_debug_service_rejects_changed_source_before_runtime_resolution() -> None:
    async def exercise() -> None:
        resolved = False

        async def resolve(_engagement_id: str):
            nonlocal resolved
            resolved = True
            raise AssertionError("runtime must not be resolved")

        service = DebugService(
            workspace_service=_Workspace(b"new bytes\n"), runtime_resolver=resolve
        )
        request = DebugStartRequest(
            path="probe.py", expected_sha256=hashlib.sha256(b"old bytes\n").hexdigest()
        )
        with pytest.raises(DebuggerError, match="changed"):
            await service.start("engagement-1", request)
        assert not resolved

    asyncio.run(exercise())


def test_debug_start_api_requires_auth_and_returns_security_boundary(tmp_path) -> None:
    class FakeDebugger:
        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def start(self, engagement_id: str, request: DebugStartRequest):
            assert engagement_id == "engagement-1"
            assert request.path == "probe.py"
            return DebugStartResponse(
                session_id="debug-1",
                websocket_path="/api/v1/debug-sessions/debug-1/ws",
                websocket_ticket="ticket-1",
                path=request.path,
                source_sha256=request.expected_sha256,
                image_digest="sha256:" + "2" * 64,
                expires_at=utc_now(),
            )

    app = create_app(
        NebulaStore(tmp_path / "nebula.db"),
        auth_token="test-token",
        debug_service=FakeDebugger(),  # type: ignore[arg-type]
    )
    body = {
        "path": "probe.py",
        "expected_sha256": "0" * 64,
        "arguments": [],
    }
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/engagements/engagement-1/debug-sessions", json=body
            ).status_code
            == 401
        )
        response = client.post(
            "/api/v1/engagements/engagement-1/debug-sessions",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
    assert response.status_code == 200
    assert response.json()["workspace_access"] == "read-only"
    assert response.json()["network"] == "none"


def _resolver():
    async def resolve(_engagement_id: str):
        return SimpleNamespace(
            runner=object(),
            workspace=Path("/tmp/workspace"),
            image=SimpleNamespace(
                installed_packages=("python3", "python3-debugpy"),
                resolved_reference="sha256:" + "1" * 64,
                digest="sha256:" + "2" * 64,
            ),
        )

    return resolve


def test_debug_session_unattached_past_deadline_is_closed_and_frees_project(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        source = b"print('debug me')\n"
        backends: list[_Backend] = []

        async def start_backend(launch):
            backend = _Backend()
            backends.append(backend)
            return backend

        monkeypatch.setattr(
            "nebula.v3.debugger.ContainerRuntimeSession.start", start_backend
        )
        monkeypatch.setattr("nebula.v3.debugger.DEBUG_ATTACH_TIMEOUT_SECONDS", 0.02)
        service = DebugService(
            workspace_service=_Workspace(source), runtime_resolver=_resolver()
        )

        abandoned = await service.start("engagement-1", _request(source))
        await asyncio.sleep(0.15)
        assert backends[0].closed
        assert backends[0].process.terminated
        with pytest.raises(DebuggerError, match="not found"):
            await service.attach(abandoned.session_id, abandoned.websocket_ticket)

        started = await service.start("engagement-1", _request(source))
        await service.attach(started.session_id, started.websocket_ticket)
        await asyncio.sleep(0.15)
        assert not backends[1].closed
        await service.close(started.session_id)
        assert backends[1].closed

    asyncio.run(exercise())


def test_debug_session_stop_requires_the_ticket(monkeypatch) -> None:
    async def exercise() -> None:
        source = b"print('debug me')\n"
        backend = _Backend()

        async def start_backend(launch):
            return backend

        monkeypatch.setattr(
            "nebula.v3.debugger.ContainerRuntimeSession.start", start_backend
        )
        service = DebugService(
            workspace_service=_Workspace(source), runtime_resolver=_resolver()
        )
        started = await service.start("engagement-1", _request(source))

        with pytest.raises(DebuggerError, match="ticket is invalid") as denied:
            await service.stop(started.session_id, "wrong-ticket")
        assert denied.value.status_code == 401
        assert not backend.closed

        await service.stop(started.session_id, started.websocket_ticket)
        assert backend.closed
        assert backend.process.terminated
        with pytest.raises(DebuggerError, match="not found"):
            await service.stop(started.session_id, started.websocket_ticket)

        again = await service.start("engagement-1", _request(source))
        await service.close(again.session_id)

    asyncio.run(exercise())


def test_debug_stop_api_requires_auth_and_forwards_the_ticket(tmp_path) -> None:
    stops: list[tuple[str, str]] = []

    class FakeDebugger:
        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def stop(self, session_id: str, ticket: str) -> None:
            stops.append((session_id, ticket))
            if session_id != "debug-1":
                raise DebuggerError(
                    "session_not_found", "Debug session was not found.", status_code=404
                )

    app = create_app(
        NebulaStore(tmp_path / "nebula.db"),
        auth_token="test-token",
        debug_service=FakeDebugger(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        assert (
            client.delete(
                "/api/v1/debug-sessions/debug-1",
                headers={"X-Nebula-Debug-Ticket": "ticket-1"},
            ).status_code
            == 401
        )
        stopped = client.delete(
            "/api/v1/debug-sessions/debug-1",
            headers={
                "Authorization": "Bearer test-token",
                "X-Nebula-Debug-Ticket": "ticket-1",
            },
        )
        assert stopped.status_code == 204, stopped.text
        missing = client.delete(
            "/api/v1/debug-sessions/debug-2",
            headers={
                "Authorization": "Bearer test-token",
                "X-Nebula-Debug-Ticket": "ticket-2",
            },
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "session_not_found"
    assert stops == [("debug-1", "ticket-1"), ("debug-2", "ticket-2")]
