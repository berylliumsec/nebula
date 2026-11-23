"""Leftover Host and harness processes are reaped; addressable ones are not.

The rule (docs/design/host-command-queue.md, "Leftover processes"): a process
is leftover once the command that started it has exited and the chat turn that
ran the command has ended. These tests run real processes on the unmanaged
Host path and a fake Codex app-server; the protected-runner path is covered in
test_host_memory_runner.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from nebula.v3.automation_runtime import (
    HostRuntimeSession,
    ProcessIORequest,
    RunCommandRequest,
)
from nebula.v3.domain import (
    AutomationApprovalPolicy,
    ChatTurn,
    ChatTurnStatus,
    CommandExecution,
    CommandExecutionStatus,
)
from nebula.v3.diagnostics import configure_diagnostics, shutdown_diagnostics
from nebula.v3.harnesses import _CodexRpc
from nebula.v3.process_identity import process_birth
from nebula.v3.process_reaping import (
    marked_processes,
    marker_supported,
    new_owner_token,
    owner_environment,
)
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_automation_runtime import create_tool_chat, runtime

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not marker_supported(), reason="process markers need Linux /proc"
    ),
]


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


async def _until(predicate, seconds: float = 10.0) -> bool:
    for _ in range(int(seconds * 20)):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def _kill(pids) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.fixture(autouse=True)
def diagnostics(tmp_path):
    """A real manager, so every reap's diagnostic is written, not skipped."""

    root = tmp_path / "diagnostics"
    configure_diagnostics(root, watch_settings=False, level_override="info")
    try:
        yield root
    finally:
        shutdown_diagnostics()


def _recorded(root: Path, event_code: str) -> list[dict]:
    found = []
    for path in root.rglob("*.log"):
        for line in path.read_text(errors="replace").splitlines():
            if event_code in line:
                found.append(json.loads(line))
    return found


@pytest.fixture
async def host(tmp_path, monkeypatch):
    # Unmanaged Host path: no protected runner, the operator override is set.
    monkeypatch.setenv(
        "NEBULA_HOST_MEMORY_RUNNER_SOCKET", str(tmp_path / "absent-runner.sock")
    )
    monkeypatch.setattr(HostRuntimeSession, "DETACHED_CHECK_SECONDS", 0.05)
    manager, store, _artifacts, engagement, _sessions = await runtime(tmp_path)
    monkeypatch.setenv("NEBULA_TEMP_UNMANAGED_COMMAND_PROJECT_IDS", engagement.id)
    await manager.update_project_policy(
        engagement.id,
        approval_policy=AutomationApprovalPolicy.NEVER,
        network_enabled=False,
        runner_profile_id=None,
        max_timeout_ms=60_000,
        execution_mode="host",
        host_access_acknowledged=True,
    )
    await create_tool_chat(store, engagement)
    started: list[int] = []
    try:
        yield manager, store, engagement, started
    finally:
        await manager.shutdown()
        _kill(started)


async def _end_turn(store, status=ChatTurnStatus.COMPLETE) -> None:
    turn = await store.get(ChatTurn, "turn-1")
    await store.update(
        ChatTurn, turn.id, {"status": status}, expected_revision=turn.revision
    )


async def _run(manager, engagement, command: str, **request):
    return await manager.run_command(
        engagement_id=engagement.id,
        owner_kind="chat",
        owner_id="chat-1",
        request=RunCommandRequest(command=command, **request),
        chat_session_id="chat-1",
        chat_turn_id="turn-1",
        chat_origin=True,
    )


async def test_detached_child_is_reaped_when_its_turn_ends(host, diagnostics):
    manager, store, engagement, started = host
    result = await _run(
        manager, engagement, "setsid nohup sleep 600 >/dev/null 2>&1 & echo $!"
    )
    # The foreground command itself is unaffected: it returns its output.
    assert result.exit_code == 0
    pid = int(result.stdout.strip())
    started.append(pid)
    await asyncio.sleep(0.3)
    assert _alive(pid), "a running turn may still use what it started"

    await _end_turn(store)
    assert await _until(lambda: not _alive(pid))
    for _ in range(100):
        execution = await store.get(CommandExecution, result.execution_id)
        if "leftover_processes" in execution.metadata:
            break
        await asyncio.sleep(0.05)
    note = execution.metadata["leftover_processes"]
    assert note["reaped_count"] == 1 and note["reason"] == "turn_ended"
    assert note["chat_turn_id"] == "turn-1"
    assert execution.status == CommandExecutionStatus.COMPLETED
    [event] = _recorded(diagnostics, "runtime.host_leftovers_reaped")
    assert event["execution_id"] == execution.id
    assert event["metadata"]["count"] == 1


async def test_background_command_survives_its_turn_and_stays_addressable(host):
    manager, store, engagement, _started = host
    result = await _run(manager, engagement, "sleep 600", background=True)
    await _end_turn(store)
    await asyncio.sleep(0.5)
    polled = await manager.process_io(
        result.process_id,
        ProcessIORequest(action="poll"),
        engagement_id=engagement.id,
        owner_id="chat-1",
    )
    assert polled.status == CommandExecutionStatus.RUNNING
    execution = await store.get(CommandExecution, result.execution_id)
    assert "leftover_processes" not in execution.metadata
    await manager.process_io(
        result.process_id,
        ProcessIORequest(action="terminate"),
        engagement_id=engagement.id,
        owner_id="chat-1",
    )


async def test_session_close_kills_escaped_descendants_without_a_turn(host):
    manager, _store, engagement, started = host
    result = await manager.run_command(
        engagement_id=engagement.id,
        owner_kind="chat",
        owner_id="no-turn",
        request=RunCommandRequest(
            command="setsid nohup sleep 600 >/dev/null 2>&1 & echo $!"
        ),
    )
    pid = int(result.stdout.strip())
    started.append(pid)
    await asyncio.sleep(0.3)
    assert _alive(pid), "without a chat turn leftovers wait for the session"
    await manager.close_session(result.session_id)
    assert await _until(lambda: not _alive(pid))


async def test_startup_kills_only_orphans_of_an_ended_launcher_in_this_core(host):
    manager, _store, engagement, started = host
    known = await _run(manager, engagement, "true")
    # A launcher that has ended: a short-lived process with a known birth.
    launcher = subprocess.Popen(["sleep", "30"])
    birth = process_birth(launcher.pid)
    launcher.kill()
    launcher.wait()

    def spawn(token: str) -> int:
        child = subprocess.Popen(
            ["sleep", "600"],
            env={**os.environ, **owner_environment(token)},
            start_new_session=True,
        )
        started.append(child.pid)
        return child.pid

    orphan = spawn(f"host-command:{known.process_id}@{launcher.pid}:{birth}")
    foreign = spawn(f"host-command:not-in-this-core@{launcher.pid}:{birth}")
    live_owner = spawn(new_owner_token("host-command", known.process_id))
    assert await _until(lambda: all(map(_alive, (orphan, foreign, live_owner))))

    assert await manager._reap_orphaned_host_processes() == 1
    assert await _until(lambda: not _alive(orphan))
    assert _alive(foreign) and _alive(live_owner)


_FAKE_APP_SERVER = """
import subprocess, sys, time
plain = subprocess.Popen(["sleep", "600"])
escaped = subprocess.Popen(["sleep", "600"], start_new_session=True)
print(plain.pid, escaped.pid, flush=True)
sys.stdin.read()
time.sleep(600)
"""


async def test_codex_app_server_close_stops_its_whole_process_tree(
    tmp_path, diagnostics
):
    script = tmp_path / "fake_app_server.py"
    script.write_text(_FAKE_APP_SERVER, encoding="utf-8")
    marker = new_owner_token("harness-codex")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **owner_environment(marker)},
        process_group=0,
    )
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
    plain, escaped = (int(item) for item in line.split())
    try:
        assert _alive(plain) and _alive(escaped)
        rpc = _CodexRpc(process=process, process_marker=marker)
        await asyncio.wait_for(rpc.close(), timeout=15)
        assert process.returncode is not None
        assert await _until(lambda: not _alive(plain) and not _alive(escaped))
        assert marked_processes(marker) == []
        [event] = _recorded(diagnostics, "harnesses.codex.children_reaped")
        assert event["metadata"]["count"] == 2
    finally:
        _kill((plain, escaped))
