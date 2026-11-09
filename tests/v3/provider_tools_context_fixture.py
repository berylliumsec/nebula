"""Explicit disposable-Core adapter; copied into retained acceptance bundles.

Real Core storage, catalog, ToolBroker, policy and transport stay in use. Only
provider/MCP output is inert. No provider, MCP program or Jev service is contacted.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

NATIVE_ID = "acceptance-native"
DEFERRED_ID = "acceptance-deferred"
TOOL_NAME = "precise_read"
MODE_FILE = "acceptance-provider-tools-context.json"
DESCRIPTION = "Return one synthetic value with exact constraints.\n  Keep this description exact. "
SCHEMA = {
    "type": "object",
    "properties": {
        "value": {
            "type": "string",
            "enum": ["ABC", "DEF"],
            "pattern": "^[A-Z]{3}$",
            "minLength": 3,
            "maxLength": 3,
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 4},
    },
    "required": ["value", "limit"],
    "additionalProperties": False,
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record(root: Path, event: str, **values: Any) -> None:
    with (root / "acceptance-events.jsonl").open("a") as output:
        output.write(_json({"event": event, "pid": os.getpid(), **values}) + "\n")


async def initialize(
    store: Any, *, root: Path, project_id: str, session_id: str
) -> dict[str, Any]:
    # Common types only: the manager may run an older read-compatible Core.
    from nebula.v3.domain import (
        ChatMessage,
        ChatSession,
        Engagement,
        McpCapabilitySnapshot,
        McpServerProfile,
        McpToolSnapshot,
        ProviderProfile,
        ScopePolicy,
        utc_now,
    )
    from nebula.v3.mcp import mcp_tool_runtime_name
    from nebula.v3.storage import NotFoundError

    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "provider-tools-workspace"
    workspace.mkdir(exist_ok=True)
    owner = await store.get(ChatSession, session_id)
    project = await store.get(Engagement, project_id)
    if owner.engagement_id != project_id or owner.backend != "provider":
        raise ValueError("fixture requires its own fresh provider chat")
    if await store.list_session_entities(ChatMessage, session_id):
        raise ValueError("fixture initialization requires an empty synthetic chat")
    scope_id = "provider-tools-scope:" + project_id
    try:
        scope = await store.get(ScopePolicy, scope_id)
    except NotFoundError:
        scope = await store.create(
            ScopePolicy(
                id=scope_id,
                engagement_id=project_id,
                on_demand_tools=True,
                tool_suggestions=False,
            )
        )
    await store.update(
        Engagement,
        project.id,
        {"scope_policy_id": scope.id, "workspace_path": str(workspace)},
        expected_revision=project.revision,
    )
    for identifier, name in (
        (NATIVE_ID, "Synthetic-native-source"),
        (DEFERRED_ID, "Synthetic-deferred-source"),
    ):
        try:
            await store.get(McpServerProfile, identifier)
        except NotFoundError:
            await store.create(
                McpServerProfile(
                    id=identifier,
                    name=name,
                    transport="stdio",
                    command="/usr/bin/false",
                    enabled=True,
                    trusted_stdio=True,
                    metadata={"engagement_id": project_id},
                    capabilities=McpCapabilitySnapshot(
                        checked_at=utc_now(),
                        tools=[
                            McpToolSnapshot(
                                name=TOOL_NAME,
                                description=DESCRIPTION,
                                input_schema=SCHEMA,
                            ),
                        ],
                    ),
                )
            )
    profile = await store.get(ProviderProfile, owner.provider_profile_id)
    await store.update(
        ProviderProfile,
        profile.id,
        {
            "provider_type": "vllm",
            "is_local": True,
            "enabled": True,
            "model_allowlist": ["model-a"],
            "capabilities": {"streaming": True, "tool_calling": True},
            "capability_verifications": {
                "model-a": {"model": "model-a", "status": "verified"}
            },
            "metadata": {
                **profile.metadata,
                "default_model": "model-a",
                "model_descriptors": [
                    {
                        "id": "model-a",
                        "context_window": 12_000,
                        "max_output_tokens": 2_048,
                    },
                ],
            },
        },
        expected_revision=profile.revision,
    )
    await store.update(
        ChatSession,
        owner.id,
        {
            "metadata": {
                **owner.metadata,
                "mcp_server_ids": [NATIVE_ID],
                "tools_enabled": False,
                "allow_agent_messaging": False,
                "allow_subagents": False,
                "initial_title_state": "operator",
            }
        },
        expected_revision=owner.revision,
    )
    document = {
        "mode": "provider-tools-context",
        "project_id": project_id,
        "session_id": session_id,
        "provider_id": profile.id,
        "workspace": str(workspace),
    }
    (root / MODE_FILE).write_text(_json(document) + "\n")
    return {
        **document,
        "mcp_server_ids": [NATIVE_ID],
        "deferred_tool": mcp_tool_runtime_name(DEFERRED_ID, TOOL_NAME),
        "input_schema": SCHEMA,
        "description_sha256": hashlib.sha256(DESCRIPTION.encode()).hexdigest(),
    }


def provider_factory(profile: Any, *, root: Path, lease: Any, version: str) -> Any:
    from nebula.v3.context import estimate_model_request, estimate_tokens
    from nebula.v3.mcp import mcp_tool_runtime_name
    from nebula.v3.providers import (
        ModelCapabilities,
        ModelProvider,
        ModelResponse,
        ModelStreamEvent,
        ModelUsage,
        ProviderConfig,
        ProviderHealth,
        ProviderKind,
        StreamEventType,
        ToolCall,
    )

    document = json.loads((root / MODE_FILE).read_text())
    if (
        document["project_id"] != lease.project_id
        or document["provider_id"] != profile.id
    ):
        raise ValueError("fixture identity differs from its admitted project")
    tool_name = mcp_tool_runtime_name(DEFERRED_ID, TOOL_NAME)

    class InertToolsProvider(ModelProvider):
        def __init__(self) -> None:
            super().__init__(
                ProviderConfig(
                    id=profile.id,
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    base_url="http://127.0.0.1:1/v1",
                    default_model="model-a",
                    model_allowlist=["model-a"],
                    local=True,
                    capabilities=ModelCapabilities(
                        streaming=True, tools=True, strict_tools=True
                    ),
                )
            )

        async def health(self):
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=["model-a"]
            )

        async def complete(self, request):
            operation = request.metadata.get("operation")
            if operation == "context_compaction":
                if any(
                    '"input_schema"' in str(message.content)
                    for message in request.messages
                ):
                    raise AssertionError(
                        "tool definitions reached the transcript summarizer"
                    )
                text, calls, stage = (
                    '{"summary":"Synthetic conversation retained with provenance."}',
                    [],
                    "summary",
                )
            elif operation == "conversation_naming":
                text, calls, stage = "Durable tools fixture", [], "title"
            else:
                from nebula.v3.provider_tool_manifest import SECTION_START, SECTION_END

                blocks = (request.instructions or "").split(SECTION_START)
                section = (
                    json.loads(blocks[1].split(SECTION_END)[0])
                    if len(blocks) == 2
                    else {}
                )
                definitions = [
                    item
                    for item in section.get("loaded", [])
                    if item["name"] == tool_name
                ]
                known = (
                    len(definitions) == 1
                    and definitions[0]["input_schema"] == SCHEMA
                    and definitions[0]["description"] == DESCRIPTION
                )
                user = next(
                    (
                        str(message.content)
                        for message in reversed(request.messages)
                        if message.role == "user"
                    ),
                    "",
                )
                # Core appends JSON excerpts of earlier commands to this text.
                # Only the current operator command occupies its own line.
                commands = re.findall(
                    r"(?m)^\[TOOLS_CONTEXT:(LEARN|USE|FILL)\](?:[ \t]|$)", user
                )
                if len(commands) != 1:
                    raise AssertionError(
                        "fixture requires one current operator command"
                    )
                learn, fill = commands[0] == "LEARN", commands[0] == "FILL"
                loaded = any(
                    result.name == "tool_catalog.load"
                    for result in request.tool_results
                )
                used = any(
                    result.name == "tool_catalog.call"
                    for result in request.tool_results
                )
                seed = hashlib.sha256(
                    (document["session_id"] + user).encode()
                ).hexdigest()[:12]
                if learn and not known and not loaded:
                    text, stage = "", "load"
                    calls = [
                        ToolCall(
                            id="load" + seed,
                            name="tool_catalog.load",
                            arguments={"names": [tool_name]},
                        )
                    ]
                elif fill:
                    if not known:
                        raise AssertionError(
                            "Core lost the exact definition before a later turn"
                        )
                    text, calls, stage = (
                        "TOOLS_CONTEXT_PADDING "
                        + "Synthetic retained conversation. " * 140,
                        [],
                        "padding",
                    )
                elif not known:
                    raise AssertionError(
                        "Core did not reconstruct one exact deferred definition"
                    )
                elif not used:
                    text, stage = "", "call"
                    calls = [
                        ToolCall(
                            id="call" + seed,
                            name="tool_catalog.call",
                            arguments={
                                "name": tool_name,
                                "arguments": {"value": "ABC", "limit": 2},
                            },
                        )
                    ]
                else:
                    text, calls, stage = (
                        "TOOLS_CONTEXT_LEARNED ABC"
                        if learn
                        else "TOOLS_CONTEXT_USED ABC",
                        [],
                        "answer",
                    )
                _record(
                    root,
                    "provider-tools-request",
                    version=version,
                    project_id=lease.project_id,
                    stage=stage,
                    revision=request.metadata.get("provider_tool_manifest_revision"),
                    digest=request.metadata.get("provider_tool_manifest_digest"),
                    canonical_sections=len(blocks) - 1,
                    complete_definition_count=len(definitions),
                    canonical_sha256=hashlib.sha256(
                        _json(section).encode()
                    ).hexdigest(),
                    native_names=[item.name for item in request.tools],
                    load_result_schema_count=sum(
                        isinstance(result.output, dict)
                        and any(
                            "input_schema" in item
                            for item in result.output.get("loaded", [])
                            if isinstance(item, dict)
                        )
                        for result in request.tool_results
                    ),
                    estimated_input_tokens=estimate_model_request(request),
                    cached_input_tokens=0,
                )
            if operation:
                _record(
                    root,
                    "provider-tools-operation",
                    version=version,
                    project_id=lease.project_id,
                    stage=stage,
                )
            output = max(1, estimate_tokens(text))
            inputs = estimate_model_request(request)
            return ModelResponse(
                provider_id=self.config.id,
                model=request.model or "model-a",
                text=text,
                tool_calls=calls,
                finish_reason="tool_calls" if calls else "stop",
                usage=ModelUsage(
                    input_tokens=inputs,
                    output_tokens=output,
                    total_tokens=inputs + output,
                ),
            )

        async def stream(self, request):
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            response = await self.complete(request)
            for call in response.tool_calls:
                yield ModelStreamEvent(type=StreamEventType.TOOL_CALL, tool_call=call)
            if response.text:
                yield ModelStreamEvent(
                    type=StreamEventType.TEXT_DELTA, delta=response.text
                )
            yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=response)

    return InertToolsProvider()


def configure_app(app: Any, *, root: Path, lease: Any, version: str) -> None:
    from nebula.v3.domain import Engagement, RiskClass, ScopePolicy
    from nebula.v3.mcp import mcp_tool_runtime_name
    from nebula.v3.policy import PolicyEngine
    from nebula.v3.runtime_platform import RuntimeToolComponents
    from nebula.v3.sandbox import AnalysisOnlyRunner
    from nebula.v3.tools import (
        AnalysisTool,
        StoreToolLedger,
        ToolBroker,
        ToolRegistry,
        ToolSpec,
    )

    document = json.loads((root / MODE_FILE).read_text())
    if document["project_id"] != lease.project_id:
        return
    chat = app.state.control_assistant_work.chat

    class InertToolPlatform:
        async def chat_components(self, *, engagement_id, mcp_profiles=(), **_):
            project = await chat.store.get(Engagement, engagement_id)
            scope = await chat.store.get(ScopePolicy, project.scope_policy_id)
            registry = ToolRegistry()
            for profile in mcp_profiles:
                if profile.id not in {NATIVE_ID, DEFERRED_ID}:
                    continue
                name = mcp_tool_runtime_name(profile.id, TOOL_NAME)

                async def read(arguments, capability=name):
                    _record(
                        root,
                        "provider-tools-effect",
                        version=version,
                        project_id=engagement_id,
                        name=capability,
                        value=arguments["value"],
                        limit=arguments["limit"],
                    )
                    return {
                        "value": arguments["value"],
                        "limit": arguments["limit"],
                        "synthetic": True,
                    }

                registry.register(
                    AnalysisTool(
                        ToolSpec(
                            name=name,
                            source_id="mcp:" + profile.id,
                            display_name=profile.name + " · " + TOOL_NAME,
                            description=DESCRIPTION,
                            input_schema=SCHEMA,
                            output_schema={
                                "type": "object",
                                "additionalProperties": True,
                            },
                            risk_class=RiskClass.LOCAL_READ,
                        ),
                        read,
                    )
                )
            broker = ToolBroker(
                registry=registry,
                policy_engine=PolicyEngine(),
                runner=AnalysisOnlyRunner(),
                ledger=StoreToolLedger(chat.store),
                workspace_resolver=lambda _: Path(document["workspace"]),
            )
            return RuntimeToolComponents(
                broker=broker,
                scope=scope,
                workspace=Path(document["workspace"]),
                specs={item.name: item for item in registry.specs()},
                runtime_digest="acceptance-provider-tools-context-v1",
            )

    chat.tool_platform = InertToolPlatform()
    chat.workspace_resolver = lambda _: Path(document["workspace"])
    chat.knowledge_index = None
    chat.tool_suggestion_client = lambda: None
    chat.provider_factory = lambda profile: provider_factory(
        profile, root=root, lease=lease, version=version
    )
    chat.provider_scheduler.host_load = None
