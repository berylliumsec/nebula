"""Retained browser adapter against the real provider chat pipeline."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from types import SimpleNamespace

from nebula.v3.domain import ChatSession
from tests.v3.provider_tools_context_fixture import initialize, configure_app
from tests.v3.test_provider_tools_context import _next, _service


async def test_retained_adapter_uses_real_catalog_broker_and_compaction(
    tmp_path, monkeypatch
):
    service, request, _ = await _service(tmp_path, monkeypatch)
    owner = await service.store.create(
        ChatSession(
            id="adapter-chat",
            engagement_id=request.engagement_id,
            provider_profile_id=request.provider_id,
            model="model-a",
            title="Synthetic adapter",
        )
    )
    root = tmp_path / "retained-adapter"
    config = await initialize(
        service.store, root=root, project_id=owner.engagement_id, session_id=owner.id
    )
    app = SimpleNamespace(
        state=SimpleNamespace(control_assistant_work=SimpleNamespace(chat=service))
    )
    configure_app(
        app,
        root=root,
        lease=SimpleNamespace(project_id=owner.engagement_id),
        version="fixture-v1",
    )
    request = request.model_copy(update={"mcp_server_ids": config["mcp_server_ids"]})
    prepared = await service.prepare_async(
        _next(request, owner.id, "[TOOLS_CONTEXT:LEARN]")
    )
    assert (
        await service.complete(prepared)
    ).message.content == "TOOLS_CONTEXT_LEARNED ABC"
    used = await service.prepare_async(_next(request, owner.id, "[TOOLS_CONTEXT:USE]"))
    assert (await service.complete(used)).message.content == "TOOLS_CONTEXT_USED ABC"
    compacted = False
    for index in range(6):
        padding = await service.prepare_async(
            _next(request, owner.id, f"[TOOLS_CONTEXT:FILL] {index}")
        )
        await service.complete(padding)
        compacted = compacted or padding.context_snapshot is not None
        if compacted:
            break
    assert compacted
    events = [
        json.loads(line)
        for line in (root / "acceptance-events.jsonl").read_text().splitlines()
    ]
    requests = [item for item in events if item["event"] == "provider-tools-request"]
    assert sum(item["stage"] == "load" for item in requests) == 1
    assert all(item["canonical_sections"] == 1 for item in requests)
    assert all(
        item["complete_definition_count"] == 1
        for item in requests
        if item["stage"] != "load"
    )
    assert all(item["load_result_schema_count"] == 0 for item in requests)
