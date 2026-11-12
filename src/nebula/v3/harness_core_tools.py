"""Shared Core brokers exposed through a managed harness's authenticated gateway.

Vendor-native tools stay at the adapter boundary. These capabilities use the
same implementations, invocation ownership and policy as provider-chat tools.
"""

from __future__ import annotations

from .async_utils import resolve_workspace

import asyncio
import hashlib
import json
from typing import Any

from .automation_tools import command_specs
from .browser_tools import BrowserToolPlatform, combine_tool_components
from .butler_reporting_tools import report_components
from .chat_goal_tools import goal_completion_components
from .chat_subagents import (
    SubagentWaitPending,
    is_subagent_session,
    subagent_child_components,
)
from .domain import (
    ChatSession,
    ChatTurn,
    Engagement,
    HarnessProfile,
    McpServerProfile,
    ScopePolicy,
    SshEnvironment,
    BrowserSession,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
)
from .research_results import ResearchResultService
from .runtime_platform import (
    RuntimeToolComponents,
    conversation_search_components,
    notes_components,
)
from .sandbox import AnalysisOnlyRunner
from .policy import PolicyEngine
from .skill_catalog import SkillSnapshot, skill_resource_components
from .tool_catalog import catalog_components
from .tools import (
    ApprovalRequired,
    AmbiguousToolState,
    InvocationAnalysisTool,
    StoreToolLedger,
    ToolBroker,
    ToolInvocation,
    ToolRegistry,
    ToolExecutionResult,
    ToolNotPermitted,
    InvalidToolArguments,
)
from .tool_results import MAX_EXCERPT_BYTES, serialize_model_result


async def core_components(
    runtime: Any, session: Any, harness_turn: Any
) -> RuntimeToolComponents | None:
    if (
        not harness_turn.chat_turn_id
        or session.metadata.get("analysis_only")
        or session.metadata.get("control_assistant")
    ):
        return None
    store = runtime.store
    profile = await store.get(HarnessProfile, session.harness_profile_id)
    if not profile.privacy.local_only and not profile.privacy.permits_sensitive_data:
        return None
    turn = await store.get(ChatTurn, harness_turn.chat_turn_id)
    snapshot = turn.request_snapshot
    if not profile.privacy.local_only and not snapshot.get("remote_mcp_confirmed"):
        return None
    project = await store.get(Engagement, turn.engagement_id)
    scope = (
        (await store.get(ScopePolicy, project.scope_policy_id))
        if project.scope_policy_id
        else ScopePolicy(id=f"scope:{project.id}", engagement_id=project.id)
    )
    workspace = await resolve_workspace(runtime.workspace_resolver, project.id)
    components = notes_components(store, scope, workspace)
    components = combine_tool_components(
        components,
        conversation_search_components(
            store,
            scope,
            workspace,
            session_id=turn.session_id,
            text_of=lambda message: message.content,
        ),
    )
    registry = ToolRegistry()
    for name, spec in command_specs().items():
        if not name.startswith("research_result_"):
            continue

        async def query(
            invocation: ToolInvocation,
            action: str = name.removeprefix("research_result_"),
        ) -> dict[str, Any]:
            return await asyncio.to_thread(
                ResearchResultService().query, action, invocation.arguments
            )

        registry.register(InvocationAnalysisTool(spec, query))
    components = combine_tool_components(
        components,
        RuntimeToolComponents(
            broker=ToolBroker(
                registry=registry,
                policy_engine=PolicyEngine(),
                runner=AnalysisOnlyRunner(),
                ledger=StoreToolLedger(store),
                workspace_resolver=lambda _: workspace,
            ),
            scope=scope,
            workspace=workspace,
            specs={spec.name: spec for spec in registry.specs()},
            runtime_digest="",
        ),
    )
    if runtime.tool_platform is not None:
        catalog_profiles = tuple(
            McpServerProfile.model_validate(item)
            for item in snapshot.get("mcp_catalog_snapshot", [])
        )
        ssh = tuple(
            SshEnvironment.model_validate(item)
            for item in snapshot.get("ssh_environment_snapshot", [])
        )
        extra = await runtime.tool_platform.chat_components(
            engagement_id=project.id,
            turn_id=turn.id,
            provider=None,
            model=session.model,
            mcp_profiles=catalog_profiles,
            ssh_environments=ssh,
            allow_empty=True,
        )
        components = combine_tool_components(components, extra)
        allowed = snapshot.get("allowed_tool_names")
        deferred = {
            name
            for name, spec in extra.specs.items()
            if spec.source_id and (allowed is None or name in allowed)
        }
        catalog = catalog_components(components, deferred=deferred)
        if catalog is not None:
            components = combine_tool_components(components, catalog)
    if turn.goal_id:
        components = combine_tool_components(
            components,
            report_components(
                store, engagement_id=project.id, workspace=workspace, scope=scope
            ),
        )
        components = combine_tool_components(
            components,
            goal_completion_components(
                store, engagement_id=project.id, workspace=workspace, scope=scope
            ),
        )
    skills = [
        SkillSnapshot.model_validate(item)
        for item in snapshot.get("skill_snapshots", [])
    ]
    selected = skill_resource_components(
        skills, engagement_id=project.id, workspace=workspace, scope=scope
    )
    if selected is not None:
        components = combine_tool_components(components, selected)
    browser_id = snapshot.get("browser_session_id")
    if browser_id:
        browser = await store.get(BrowserSession, browser_id)
        if browser.metadata.get("browser_companion_version") != 1:
            components = combine_tool_components(
                components,
                (
                    await BrowserToolPlatform(store).chat_components(
                        engagement_id=project.id, browser_session_id=browser_id
                    )
                ),
            )
    chat = await store.get(ChatSession, turn.session_id)
    if runtime.provider_subagents is not None and is_subagent_session(chat):
        components = combine_tool_components(
            components,
            subagent_child_components(
                runtime.provider_subagents,
                engagement_id=project.id,
                workspace=workspace,
                scope=scope,
            ),
        )
    return components


async def call_core_tool(
    runtime: Any,
    session: Any,
    turn: Any,
    components: RuntimeToolComponents,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch through the provider broker, retaining ledger/approval ownership."""
    active = await runtime.store.get(type(turn), turn.id)
    from .domain import HarnessTurnStatus, ChatTurnStatus

    chat_turn = await runtime.store.get(ChatTurn, turn.chat_turn_id)
    if active.status not in {
        HarnessTurnStatus.RUNNING,
        HarnessTurnStatus.WAITING_APPROVAL,
    } or chat_turn.status not in {
        ChatTurnStatus.ROUTING,
        ChatTurnStatus.WAITING_APPROVAL,
    }:
        raise ToolNotPermitted(
            "this harness turn no longer owns tool execution", rule="harness.core_turn"
        )
    catalog_call = name == "tool_catalog.call"
    if name == "tool_catalog.call":
        target_name, target_arguments = (
            arguments.get("name"),
            arguments.get("arguments"),
        )
        if (
            not isinstance(target_name, str)
            or target_name not in components.specs
            or not components.specs[target_name].source_id
            or not isinstance(target_arguments, dict)
        ):
            raise InvalidToolArguments(
                "choose a catalog tool and use its loaded input schema"
            )
        name, arguments = target_name, target_arguments
        if name not in chat_turn.request_snapshot.get("core_loaded_tools", []):
            raise InvalidToolArguments(
                "load this tool's schema with tool_catalog.load before calling it"
            )
    allowed = chat_turn.request_snapshot.get("allowed_tool_names")
    if catalog_call and allowed is not None and name not in allowed:
        raise ToolNotPermitted(
            "this catalog tool was not assigned to the child",
            rule="harness.child_tools",
        )
    spec = components.specs[name]
    from jsonschema import Draft202012Validator

    Draft202012Validator(spec.input_schema).validate(arguments)
    digest = hashlib.sha256(
        json.dumps([name, arguments], sort_keys=True).encode()
    ).hexdigest()
    invocation = ToolInvocation(
        engagement_id=turn.engagement_id,
        run_id=turn.chat_turn_id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=turn.chat_session_id,
        chat_turn_id=turn.chat_turn_id,
        tool_name=name,
        arguments=arguments,
        workspace=components.workspace,
        requested_by="harness-gateway",
        runtime_session_kind="harness",
        runtime_session_id=session.id,
        idempotency_key=f"core:{turn.id}:{digest}"
        if spec.budget_class == "execution"
        or name
        in {
            "notes.write",
            "report_progress",
            "report_attention",
            "message_parent",
        }
        else None,
    )

    async def execute(approval: Any = None) -> Any:
        if spec.budget_class == "execution":
            async with await runtime._gateway_execution_gate(turn):
                return await components.broker.execute(
                    invocation, components.scope, approval=approval
                )
        return await components.broker.execute(
            invocation, components.scope, approval=approval
        )

    ledger = StoreToolLedger(runtime.store)
    call = await ledger.reserve(invocation, spec)
    call = await runtime._attach_gateway_tool_call(turn, call.id)
    if call.status == ToolCallStatus.COMPLETE:
        value = call.result
    else:
        if call.metadata.get("core_dispatch_started"):
            raise AmbiguousToolState(
                "this Core tool already started; inspect its saved outcome before retrying"
            )
        call = await runtime.store.update(
            ToolCall,
            call.id,
            {"metadata": {**call.metadata, "core_dispatch_started": True}},
            expected_revision=call.revision,
        )
        try:
            try:
                result = await execute()
            except SubagentWaitPending as pending:  # diagnostic-expected: durable child question waits without replaying the tool
                # A child can ask its parent without consuming an execution
                # slot. Cancellation still ends the gateway wait.
                service = runtime.provider_subagents
                await service._deliver(service._invoking_child(invocation))
                while not service.wait_ready(pending.wait):
                    await asyncio.sleep(0.05)
                output, _ = service.wait_result(pending.wait)
                result = ToolExecutionResult(output=output)
            except ApprovalRequired as required:  # diagnostic-expected: the broker persisted the approval before yielding execution
                await runtime._wait_for_broker_approval(turn, required.approval)
                from .domain import Approval

                approval = await runtime.store.get(Approval, required.approval.id)
                result = await execute(approval)
            value = (
                result.receipt.as_model_result()
                if result.receipt is not None
                else result.output
            )
            # Provider result bounds also apply to the MCP boundary. Preserve
            # larger outputs as artifacts rather than dropping recovery data.
            if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_EXCERPT_BYTES:
                result = await runtime.evidence_recorder.record(
                    (await runtime.store.get(ToolCall, call.id)),
                    invocation,
                    spec,
                    result,
                )
                value = result.receipt.as_model_result()
            latest = await runtime.store.get(ToolCall, call.id)
            if latest.status != ToolCallStatus.COMPLETE or latest.result != value:
                await ledger.transition(latest, ToolCallStatus.COMPLETE, result=value)
        except BaseException as exc:
            latest = await runtime.store.get(ToolCall, call.id)
            if latest.status not in {
                ToolCallStatus.COMPLETE,
                ToolCallStatus.DENIED,
                ToolCallStatus.CANCELLED,
            }:
                await ledger.transition(
                    latest,
                    ToolCallStatus.CANCELLED
                    if isinstance(exc, asyncio.CancelledError)
                    else ToolCallStatus.FAILED,
                    error=str(exc)[:1000],
                )
            raise
    if (
        name == "goal.complete"
        and isinstance(value, dict)
        and value.get("accepted") is True
    ):
        chat_turn = await runtime.store.get(ChatTurn, turn.chat_turn_id)
        (
            await runtime.store.update(
                ChatTurn,
                chat_turn.id,
                {
                    "request_snapshot": {
                        **chat_turn.request_snapshot,
                        "goal_completion_proposal": arguments,
                    }
                },
                expected_revision=chat_turn.revision,
            )
        )
    if name == "tool_catalog.load":
        current = await runtime.store.get(ChatTurn, turn.chat_turn_id)
        loaded = sorted(
            set(current.request_snapshot.get("core_loaded_tools", []))
            | set(arguments["names"])
        )
        (
            await runtime.store.update(
                ChatTurn,
                current.id,
                {
                    "request_snapshot": {
                        **current.request_snapshot,
                        "core_loaded_tools": loaded,
                    }
                },
                expected_revision=current.revision,
            )
        )
    encoded = serialize_model_result(value)
    return {
        "content": [{"type": "text", "text": encoded}],
        "structuredContent": value,
        "isError": isinstance(value, dict)
        and value.get("schema") == "nebula.tool-result/v2"
        and value.get("status") != "completed",
    }
