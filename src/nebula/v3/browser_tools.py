"""Inert, session-bound AI capabilities for the durable security browser.

The model can create a reviewable proposal. It cannot approve or execute the
proposal, read cookies, or broaden the Project scope through this broker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .browser_security import BrowserActionProposalRequest, BrowserSecurityService
from .domain import (
    Approval,
    BrowserActionKind,
    BrowserSession,
    Engagement,
    RiskClass,
    ScopePolicy,
    ToolCallStatus,
)
from .runtime_platform import RuntimeToolComponents
from .storage import NebulaStore
from .tools import (
    AmbiguousToolState,
    InvalidToolArguments,
    StoreToolLedger,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)


BROWSER_PROPOSE_ACTION = "browser.propose_action"


def browser_action_spec(session_id: str) -> ToolSpec:
    kinds = [
        BrowserActionKind.NAVIGATE.value,
        BrowserActionKind.CLICK.value,
        BrowserActionKind.FILL.value,
        BrowserActionKind.SELECT.value,
        BrowserActionKind.PRESS.value,
        BrowserActionKind.EXTRACT.value,
        BrowserActionKind.REPLAY.value,
    ]
    return ToolSpec(
        name=BROWSER_PROPOSE_ACTION,
        version="1",
        description=(
            "Create an inert, scope-checked browser action proposal for the "
            "operator to review. This capability never approves or executes the "
            "action. Use semantic locators such as role/name, label, text, or a "
            "bounded CSS selector. Never place passwords, tokens, cookies, or "
            "other secrets in fill text."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tab_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "kind": {"type": "string", "enum": kinds},
                "locator": {
                    "type": "object",
                    "maxProperties": 8,
                    "additionalProperties": {"type": "string", "maxLength": 1000},
                },
                "arguments": {
                    "type": "object",
                    "maxProperties": 8,
                    "additionalProperties": True,
                },
                "proposal": {"type": "string", "minLength": 1, "maxLength": 4000},
                "page_url": {"type": "string", "minLength": 1, "maxLength": 16384},
            },
            "required": ["tab_id", "kind", "locator", "arguments", "proposal", "page_url"],
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action_id": {"type": "string"},
                "status": {"type": "string", "const": "proposed"},
                "action_sha256": {"type": "string"},
                "expires_at": {"type": "string"},
                "requires_operator_approval": {"type": "boolean", "const": True},
                "browser_session_id": {"type": "string", "const": session_id},
            },
            "required": [
                "action_id",
                "status",
                "action_sha256",
                "expires_at",
                "requires_operator_approval",
                "browser_session_id",
            ],
        },
        risk_class=RiskClass.LOCAL_READ,
        network_access=False,
        filesystem_access="none",
        budget_class="execution",
    )


class BrowserActionProposalBroker:
    """ToolBroker-compatible dispatcher that only persists proposals."""

    def __init__(self, store: NebulaStore, session: BrowserSession) -> None:
        self.session = session
        self.spec = browser_action_spec(session.id)
        self.ledger = StoreToolLedger(store)
        self.service = BrowserSecurityService(store)

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult:
        if approval is not None:
            raise InvalidToolArguments("browser proposal execution does not accept tool approval")
        if invocation.tool_name != self.spec.name:
            raise InvalidToolArguments(f"unknown browser capability: {invocation.tool_name}")
        if invocation.engagement_id != self.session.engagement_id:
            raise InvalidToolArguments("browser session belongs to another Project")
        if scope.engagement_id != invocation.engagement_id:
            raise InvalidToolArguments("browser scope belongs to another Project")
        errors = sorted(
            Draft202012Validator(self.spec.input_schema).iter_errors(invocation.arguments),
            key=lambda item: list(item.path),
        )
        if errors:
            raise InvalidToolArguments(errors[0].message)
        call = await self.ledger.reserve(invocation, self.spec)
        if call.status == ToolCallStatus.COMPLETE and isinstance(call.result, dict):
            return ToolExecutionResult(output=call.result)
        if call.status not in {ToolCallStatus.PROPOSED, ToolCallStatus.FAILED}:
            raise AmbiguousToolState(
                "browser proposal has an unfinished durable tool state and will not be repeated"
            )
        running = await self.ledger.transition(call, ToolCallStatus.RUNNING)
        try:
            action = self.service.propose_action(
                self.session.id,
                BrowserActionProposalRequest(
                    **invocation.arguments,
                    proposed_by=invocation.requested_by,
                ),
            )
            output: dict[str, Any] = {
                "action_id": action.id,
                "status": action.status.value,
                "action_sha256": action.action_sha256,
                "expires_at": action.expires_at.isoformat(),
                "requires_operator_approval": True,
                "browser_session_id": self.session.id,
            }
        except Exception as exc:
            await self.ledger.transition(running, ToolCallStatus.FAILED, error=str(exc))
            raise
        await self.ledger.transition(running, ToolCallStatus.COMPLETE, result=output)
        return ToolExecutionResult(output=output)


class BrowserToolPlatform:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    def chat_components(
        self,
        *,
        engagement_id: str,
        browser_session_id: str,
    ) -> RuntimeToolComponents:
        engagement = self.store.get(Engagement, engagement_id)
        session = self.store.get(BrowserSession, browser_session_id)
        if session.engagement_id != engagement_id:
            raise ValueError("selected browser context belongs to another Project")
        scope = (
            self.store.get(ScopePolicy, engagement.scope_policy_id)
            if engagement.scope_policy_id is not None
            else None
        )
        if scope is None or scope.engagement_id != engagement_id:
            raise ValueError("Project scope is required for AI browser proposals")
        broker = BrowserActionProposalBroker(self.store, session)
        workspace = Path(engagement.workspace_path or ".").resolve()
        return RuntimeToolComponents(
            broker=broker,
            scope=scope,
            workspace=workspace,
            specs={broker.spec.name: broker.spec},
            runtime_digest="browser-native-v1",
        )


def combine_tool_components(
    primary: RuntimeToolComponents | Any | None,
    browser: RuntimeToolComponents,
) -> RuntimeToolComponents:
    if primary is None:
        return browser
    if primary.scope.id != browser.scope.id or primary.scope.revision != browser.scope.revision:
        raise ValueError("browser and command runtimes resolved different scope revisions")
    overlap = set(primary.specs).intersection(browser.specs)
    if overlap:
        raise ValueError(f"duplicate browser capabilities: {sorted(overlap)}")
    from .automation_tools import CompositeBroker

    brokers = {name: primary.broker for name in primary.specs}
    brokers.update({name: browser.broker for name in browser.specs})
    return RuntimeToolComponents(
        broker=CompositeBroker(brokers),
        scope=primary.scope,
        workspace=primary.workspace,
        specs={**primary.specs, **browser.specs},
        runtime_digest="+".join(
            item for item in [getattr(primary, "runtime_digest", ""), browser.runtime_digest] if item
        ),
    )


__all__ = [
    "BROWSER_PROPOSE_ACTION",
    "BrowserActionProposalBroker",
    "BrowserToolPlatform",
    "browser_action_spec",
    "combine_tool_components",
]
