"""Async authorization releases its session before bounded file scans."""

import asyncio
import threading

import pytest

from nebula.v3.tool_results import ToolOutputService
from nebula.v3.tools import ToolExecutionResult
from nebula.v3.chat_subagents import subagent_specs
from tests.v3.test_tool_result_recovery import _delivery, _manifest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("operation", ["read", "search"])
async def test_output_scan_runs_off_loop_after_authorization(
    tmp_path, monkeypatch, operation
):
    chat, invocation, store = await _delivery(tmp_path)
    fields, _ = await chat._tool_result_entry(
        ToolExecutionResult(output=_manifest()),
        invocation=invocation,
        spec=subagent_specs()["list_subagent_capabilities"],
        call_id="manifest-call",
    )
    service = ToolOutputService(store, chat.artifact_store)
    original = chat.artifact_store.open
    entered, release = threading.Event(), threading.Event()

    def held(artifact):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        entered.set()
        assert release.wait(5), "output worker was never released"
        return original(artifact)

    monkeypatch.setattr(chat.artifact_store, "open", held)
    common = {"engagement_id": invocation.engagement_id, "owner_id": invocation.run_id}
    if operation == "read":
        work = service.read(**common, artifact_id=fields["result_artifact_id"])
    else:
        work = service.search(
            **common, tool_call_id="manifest-call", query="capability.0001"
        )
    task = asyncio.create_task(work)
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                if task.done():
                    task.result()
                await asyncio.sleep(0.005)
        assert not task.done()
        assert store.database.pool_snapshot()["checked_out"] == 0
        assert (await store.database.health())["database"] == "ok"
        release.set()
        result = await task
        assert result["lines" if operation == "read" else "matches"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
