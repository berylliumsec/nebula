"""Butler home sessions' tools serve the project they are about.

Memory recall reads the focus project's lessons (plus global memories) from
Butler's home lease; workspace reads serve the focus project's folder without
creating it; every write stays fenced to Butler's own project. See
docs/design/butler-home-worker.md, section 4.
"""

import asyncio
import hashlib
from pathlib import Path
import secrets
from types import SimpleNamespace

import httpx
import pytest

from nebula.v3.butler_memory import MemoryNotFoundError
from nebula.v3.deployment_protocol import BUTLER_HOME_PROJECT, GLOBAL_EXECUTION_PROJECT
from nebula.v3.domain import Engagement, ScopePolicy
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_coordination import LeaseLost
from nebula.v3.runtime_ipc import RuntimeRpcClient, RuntimeRpcServer
from nebula.v3.runtime_platform import (
    RuntimePlatform,
    RuntimePlatformError,
    _ButlerFocusBroker,
)
from nebula.v3.shared_butler_memory import SharedButlerMemory
from nebula.v3.shared_memory_rpc import RemoteButlerMemory, register_shared_memory
from nebula.v3.tools import ToolInvocation, retrieval_workspace
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_shared_butler_memory import remember

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def memory(cluster, tmp_path):
    coordinators, stores, _clock = cluster
    await stores[0].create(Engagement(id=BUTLER_HOME_PROJECT, name="Butler"))
    host = SharedButlerMemory(coordinators[0], tmp_path / "host-index")
    clients = []

    def peer(runtime_id):
        server = RuntimeRpcServer(secrets.token_bytes(32))
        register_shared_memory(server, host, runtime_id=runtime_id)
        client = RuntimeRpcClient(
            "unix:///isolated/host-memory",
            server.secret,
            transport=httpx.ASGITransport(app=server.app),
        )
        clients.append(client)
        return RemoteButlerMemory(client)

    project = await coordinators[0].claim_project("project", "old")
    home = await coordinators[1].claim_project(BUTLER_HOME_PROJECT, "new")
    try:
        yield SimpleNamespace(
            project=project, home=home, own=peer("old"), butler=peer("new")
        )
    finally:
        await asyncio.gather(*(client.close() for client in clients))


async def test_home_lease_reads_the_focus_projects_memories(memory):
    with execution_context(memory.project):
        lesson = await remember(memory.own)
        general = await remember(
            memory.own,
            scope="global_lesson",
            content="Re-read fresh state before reporting a goal as done.",
        )
    with execution_context(memory.home):
        listed = {item.id for item in (await memory.butler.list("project")).items}
        assert {lesson.id, general.id} <= listed
        found = await memory.butler.search("project", "source evidence")
        assert lesson.id in {item.id for item in found.items}
        context = await memory.butler.turn_context("project", "evidence")
        assert "source evidence" in context
        # Reads only: Butler's home cannot edit or delete the project's lessons.
        with pytest.raises((LeaseLost, MemoryNotFoundError)):
            await memory.butler.update(
                "project",
                lesson.id,
                "Replace this with unrelated synthetic content.",
                expected_revision=1,
            )
        with pytest.raises((LeaseLost, MemoryNotFoundError)):
            await memory.butler.delete("project", lesson.id, expected_revision=1)
        # Core's own projects are never a read target.
        with pytest.raises(LeaseLost):
            await memory.butler.list(GLOBAL_EXECUTION_PROJECT)


async def test_other_workers_still_need_exact_ownership(memory):
    with execution_context(memory.project):
        with pytest.raises(LeaseLost):
            await memory.own.list(BUTLER_HOME_PROJECT)
        rejected = await memory.own.client.call(
            "host.memory/v1",
            {
                "lease": {
                    "project_id": "project",
                    "runtime_id": "old",
                    "fencing_token": memory.project.fencing_token,
                },
                "action": "list",
                "arguments": {"target_project": "other"},
            },
        )
        assert rejected["ok"] is False and rejected["error"] == "lease_lost"


class _Recording:
    def __init__(self):
        self.seen = []

    async def execute(self, invocation, scope, *, approval=None):
        self.seen.append((invocation.tool_name, retrieval_workspace(invocation)))
        return "ok"


def _invocation(tmp_path, name):
    return ToolInvocation(
        run_id="run",
        engagement_id=BUTLER_HOME_PROJECT,
        tool_name=name,
        arguments={},
        workspace=tmp_path / "home",
    )


async def test_focus_broker_serves_only_workspace_reads_from_the_focus(tmp_path):
    inner = _Recording()
    focus = tmp_path / "focus"
    broker = _ButlerFocusBroker(inner, "Target", focus)
    scope = ScopePolicy(id="scope:home", engagement_id=BUTLER_HOME_PROJECT)
    for name in ("workspace.read", "workspace.find", "workspace.search"):
        await broker.execute(_invocation(tmp_path, name), scope)
    await broker.execute(_invocation(tmp_path, "tool_output.read"), scope)
    assert inner.seen == [
        ("workspace.read", focus),
        ("workspace.find", focus),
        ("workspace.search", focus),
        ("tool_output.read", tmp_path / "home"),
    ]
    empty = _ButlerFocusBroker(inner, "Target", None)
    with pytest.raises(RuntimePlatformError, match="no workspace files"):
        await empty.execute(_invocation(tmp_path, "workspace.read"), scope)


async def test_focus_components_never_create_the_focus_folder(tmp_path):
    configured = tmp_path / "configured"
    configured.mkdir()
    projects = {
        "configured": Engagement(
            id="configured", name="Configured", workspace_path=str(configured)
        ),
        "default": Engagement(id="default", name="Default"),
    }

    class Store:
        async def get(self, _model, project_id):
            return projects[project_id]

    platform = SimpleNamespace(store=Store(), workspace_root=tmp_path / "workspaces")
    components = SimpleNamespace(broker=_Recording())
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Components:
        broker: object

    chosen = await RuntimePlatform.focus_components(
        platform, Components(broker=_Recording()), "configured"
    )
    assert chosen.broker.workspace == configured.resolve()
    missing = await RuntimePlatform.focus_components(
        platform, Components(broker=_Recording()), "default"
    )
    assert missing.broker.workspace is None
    default = tmp_path / "workspaces" / hashlib.sha256(b"default").hexdigest()
    assert not default.exists()
    default.mkdir(parents=True)
    found = await RuntimePlatform.focus_components(
        platform, Components(broker=_Recording()), "default"
    )
    assert found.broker.workspace == default
    assert components.broker.seen == []
    assert isinstance(found.broker, _ButlerFocusBroker)
    assert Path(found.broker.workspace).stat().st_mode & 0o777 != 0o777
