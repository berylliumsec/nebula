from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio
import json
from uuid import uuid4

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import (
    Artifact,
    ChatSession,
    OperatorExecution,
    RiskClass,
    ToolCall,
)
from nebula.v3.executions import ExecutionService
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError, GuidedToolError
from ui.tests.fixtures.butler_inspection_core import (
    attach_inert_waiter,
    seed_inspection,
)


async def fixture(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    data = await seed_inspection(store, artifacts)
    service = ExecutionService(
        store=store, artifact_store=artifacts, tool_platform=None, data_root=tmp_path
    )
    app = create_app(
        store,
        artifact_store=artifacts,
        execution_service=service,
        auth_token="inspection-fixture-token",
    )
    gateway = SettingsAssistantApi(app, "inspection-fixture-token")
    capabilities = ButlerCapabilities(
        store, artifacts, data["project"].id, settings_api=gateway
    )
    return store, artifacts, data, service, gateway, capabilities


async def invoke(capabilities, name, arguments=None, *, change=False):
    return await capabilities.invoke(
        {"operation": f"nebula.inspect.{name}", "arguments": arguments or {}},
        change=change,
    )


@pytest.mark.parametrize(
    "kind",
    [
        "context",
        "reasoning",
        "evidence",
        "turn_summary",
        "turn_hooks",
        "session_hooks",
        "results",
    ],
)
async def test_butler_inspection_uses_owned_core_conversation_projections(
    tmp_path, kind
):
    store, _, data, _, _, capabilities = await fixture(tmp_path)

    async def scenario():
        turns = await invoke(capabilities, "turns", {"session_id": data["chat"].id})
        assert turns["turns"][0]["turn_id"] == data["turn"].id
        arguments = {
            "session_id": data["chat"].id,
            "kind": kind,
            **(
                {"message_id": data["message"].id}
                if kind in {"reasoning", "evidence"}
                else {}
            ),
            **(
                {"turn_id": data["turn"].id}
                if kind in {"turn_summary", "turn_hooks"}
                else {}
            ),
        }
        first = await invoke(capabilities, "chat", arguments)
        assert first["session_id"] == data["chat"].id and len(first["text"]) <= 4096
        assert len(first["sha256"]) == 64
        if first["next_offset"] is None:
            assert json.loads(first["text"]) is not None
        else:
            with pytest.raises(ValueError, match="first chunk"):
                await invoke(
                    capabilities, "chat", {**arguments, "offset": first["next_offset"]}
                )
            second = await invoke(
                capabilities,
                "chat",
                {
                    **arguments,
                    "offset": first["next_offset"],
                    "expected_sha256": first["sha256"],
                },
            )
            assert second["sha256"] == first["sha256"] and len(second["text"]) <= 4096
        assert (await store.get(ChatSession, data["chat"].id)).revision == data[
            "chat"
        ].revision

    (await scenario())


@pytest.mark.parametrize(
    "private_kind", ["control_assistant", "side_chat", "temporary_assistant"]
)
async def test_butler_inspection_rejects_private_owners_before_core_dispatch(
    tmp_path, private_kind, monkeypatch
):
    store, _, data, _, _, capabilities = await fixture(tmp_path)
    chat = data["chat"]
    (
        await store.update(
            ChatSession,
            chat.id,
            {"metadata": {private_kind: True}},
            expected_revision=chat.revision,
        )
    )

    async def scenario():
        for operation, arguments in [
            ("turns", {"session_id": chat.id}),
            ("chat", {"session_id": chat.id, "kind": "context"}),
            ("calls", {"session_id": chat.id}),
            ("artifacts", {"tool_call_id": data["call"].id}),
            (
                "output",
                {"tool_call_id": data["call"].id, "artifact_id": data["output"].id},
            ),
            ("search_output", {"tool_call_id": data["call"].id, "query": "progress"}),
        ]:
            with pytest.raises(ValueError, match="visible chat"):
                await invoke(capabilities, operation, arguments)

    (await scenario())


async def test_butler_tool_output_is_scoped_redacted_and_artifact_metadata_is_paged(
    tmp_path,
):
    store, artifacts, data, _, _, capabilities = await fixture(tmp_path)
    for i in range(51):
        (
            await store.create(
                artifacts.put_bytes(
                    f"file {i}\n".encode(),
                    engagement_id=data["project"].id,
                    filename=f"result-{i}.txt",
                    metadata={
                        "tool_call_id": data["call"].id,
                        "kind": "generated_file",
                        "searchable": True,
                    },
                )
            )
        )
    foreign = await store.create(
        ToolCall(
            engagement_id=data["foreign"].id,
            run_id="foreign",
            tool_name="foreign-private",
            risk_class=RiskClass.LOCAL_READ,
        )
    )

    async def scenario():
        calls = await invoke(capabilities, "calls", {"session_id": data["chat"].id})
        assert [row["tool_call_id"] for row in calls["calls"]] == [data["call"].id]
        first = await invoke(
            capabilities, "artifacts", {"tool_call_id": data["call"].id}
        )
        assert len(first["artifacts"]) == 50 and first["next_offset"] == 50
        last = await invoke(
            capabilities, "artifacts", {"tool_call_id": data["call"].id, "offset": 50}
        )
        assert len(last["artifacts"]) == 2 and last["next_offset"] is None
        assert "storage_path" not in str(first) and "RAW_INSPECTION_SECRET" not in str(
            first
        )
        read = await invoke(
            capabilities,
            "output",
            {
                "tool_call_id": data["call"].id,
                "artifact_id": data["output"].id,
                "line_count": 1,
            },
        )
        assert read["truncated"] is True
        read = await invoke(
            capabilities,
            "output",
            {
                "tool_call_id": data["call"].id,
                "artifact_id": data["output"].id,
                "starting_line": 2,
            },
        )
        assert "RAW_INSPECTION_SECRET" not in str(read) and "[REDACTED]" in str(read)
        search = await invoke(
            capabilities,
            "search_output",
            {"tool_call_id": data["call"].id, "query": "password"},
        )
        assert search["matches"] and "RAW_INSPECTION_SECRET" not in str(search)
        with pytest.raises(ValueError, match="selected project"):
            await invoke(capabilities, "artifacts", {"tool_call_id": foreign.id})
        with pytest.raises(CapabilityInputError):
            await invoke(
                capabilities,
                "output",
                {
                    "tool_call_id": data["call"].id,
                    "artifact_id": data["output"].id,
                    "raw": True,
                },
            )

    (await scenario())


async def test_butler_execution_reads_redacted_pages_and_cancels_only_fresh_owned_work(
    tmp_path, monkeypatch
):
    store, _, data, service, _, capabilities = await fixture(tmp_path)

    async def scenario():
        rows = await invoke(capabilities, "executions")
        assert {row["execution_id"] for row in rows["executions"]} == {
            data["completed"].id,
            data["waiting"].id,
        }
        detail = await invoke(
            capabilities, "execution", {"execution_id": data["completed"].id}
        )
        artifact = await invoke(
            capabilities,
            "execution_artifact",
            {
                "execution_id": data["completed"].id,
                "artifact_id": detail["artifacts"]["stdout_artifact_id"],
            },
        )
        assert (
            artifact["size"] == len(data["raw_bytes"])
            and "storage_path" not in artifact
        )
        output = await invoke(
            capabilities, "execution_output", {"execution_id": data["completed"].id}
        )
        assert output["redacted"] is True and len(output["text"].encode()) <= 4096
        assert (
            output["next_offset"] > 0 and "RAW_INSPECTION_SECRET" not in output["text"]
        )
        waiting = data["waiting"]
        task = await attach_inert_waiter(store, service, waiting)
        try:
            (
                await store.update(
                    OperatorExecution,
                    waiting.id,
                    {"error_detail": "Operator inspected this job"},
                    expected_revision=waiting.revision,
                )
            )
            with pytest.raises(ValueError, match="current revision"):
                await invoke(
                    capabilities,
                    "cancel_execution",
                    {"execution_id": waiting.id, "expected_revision": waiting.revision},
                    change=True,
                )
            assert not task.done()
            current = await store.get(OperatorExecution, waiting.id)
            receipt = await invoke(
                capabilities,
                "cancel_execution",
                {"execution_id": waiting.id, "expected_revision": current.revision},
                change=True,
            )
            assert receipt["read_back"] is True and receipt["execution"]["status"] in {
                "cancelling",
                "cancelled",
            }
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (
                await store.get(OperatorExecution, waiting.id)
            ).status.value == "cancelled"
            assert receipt["client_action"]["kind"] == "execution_refresh"
            assert await service.cancel(data["completed"].id) == data["completed"]
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    (await scenario())


async def test_butler_execution_core_cancel_fences_a_racing_revision(
    tmp_path, monkeypatch
):
    store, _, data, service, _, capabilities = await fixture(tmp_path)
    from nebula.v3.butler_inspection import ButlerInspection

    request = ButlerInspection._request

    async def concurrent_request(self, method, path, body=None):
        if "/cancel?" in path:
            current = await store.get(OperatorExecution, data["waiting"].id)
            (
                await store.update(
                    OperatorExecution,
                    current.id,
                    {"error_detail": "Concurrent operator update"},
                    expected_revision=current.revision,
                )
            )
        return await request(self, method, path, body)

    monkeypatch.setattr(ButlerInspection, "_request", concurrent_request)

    async def scenario():
        task = await attach_inert_waiter(store, service, data["waiting"])
        try:
            with pytest.raises(GuidedToolError):
                await invoke(
                    capabilities,
                    "cancel_execution",
                    {
                        "execution_id": data["waiting"].id,
                        "expected_revision": data["waiting"].revision,
                    },
                    change=True,
                )
            assert (
                not task.done()
                and (
                    await store.get(OperatorExecution, data["waiting"].id)
                ).status.value
                == "running"
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    (await scenario())


async def test_butler_original_download_prepares_only_linked_owned_artifacts(tmp_path):
    store, _, data, _, _, capabilities = await fixture(tmp_path)
    large = await store.create(
        Artifact(
            engagement_id=data["project"].id,
            sha256="a" * 64,
            size=20 * 1024 * 1024 + 1,
            storage_path="inert",
            metadata={"tool_call_id": data["call"].id},
        )
    )
    foreign = await store.create(
        data["waiting"].model_copy(
            update={"id": str(uuid4()), "engagement_id": data["foreign"].id}
        )
    )

    async def scenario():
        arguments = {
            "owner_kind": "tool_call",
            "owner_id": data["call"].id,
            "artifact_id": data["output"].id,
            "expected_revision": data["output"].revision,
        }
        prepared = await invoke(
            capabilities, "download_artifact", arguments, change=True
        )
        assert (
            prepared["state"] == "pending_browser_apply"
            and prepared["client_action"]["sha256"] == data["output"].sha256
        )
        assert "RAW_INSPECTION_SECRET" not in str(prepared)
        with pytest.raises(ValueError, match="20 MiB"):
            await invoke(
                capabilities,
                "download_artifact",
                {**arguments, "artifact_id": large.id},
                change=True,
            )
        with pytest.raises(ValueError, match="current identity"):
            await invoke(
                capabilities,
                "download_artifact",
                {**arguments, "expected_revision": 99},
                change=True,
            )
        with pytest.raises(ValueError, match="selected project"):
            await invoke(capabilities, "execution", {"execution_id": foreign.id})
        with pytest.raises(ValueError, match="selected execution"):
            await invoke(
                capabilities,
                "download_artifact",
                {
                    **arguments,
                    "owner_kind": "execution",
                    "owner_id": data["waiting"].id,
                },
                change=True,
            )

    (await scenario())


pytestmark = pytest.mark.anyio
