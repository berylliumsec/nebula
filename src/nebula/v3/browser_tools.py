"""Inert, session-bound AI capabilities for the durable security browser.

The model can create a reviewable proposal. It cannot approve or execute the
proposal, read cookies, or broaden the Project scope through this broker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .browser_security import BrowserActionProposalRequest, BrowserSecurityService
from .browser_automation import (
    COMMAND_RISK,
    BrowserAutomationService,
    BrowserCommandCreateRequest,
    BrowserProxyRuleRequest,
)
from .domain import (
    Approval,
    BrowserActionKind,
    BrowserCaptureMode,
    BrowserCommandStatus,
    BrowserSession,
    BrowserTrafficExchange,
    Engagement,
    RiskClass,
    ScopePolicy,
    ToolCallStatus,
)
from .agent_tooling import BrokeredToolSpecialist, ToolMissionSupervisor
from .missions import MissionComponents, MissionConfigurationError
from .orchestration import SpecialistRole
from .providers import ModelProvider
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

AUTONOMOUS_BROWSER_TOOLS = tuple(COMMAND_RISK)


def autonomous_browser_specs() -> dict[str, ToolSpec]:
    """Describe the bounded commands available inside an active lease."""

    common = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tab_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "page_url": {"type": "string", "minLength": 1, "maxLength": 16384},
        },
        "required": ["tab_id"],
    }

    def spec(
        name: str,
        description: str,
        schema: dict[str, Any],
        *,
        risk: RiskClass,
        network: bool = False,
        target: str | None = None,
    ) -> ToolSpec:
        return ToolSpec(
            name=name,
            version="1",
            description=description,
            input_schema=schema,
            output_schema={"type": "object", "additionalProperties": True},
            risk_class=risk,
            network_access=network,
            target_argument=target,
            credential_classes=["credential_ref"]
            if risk == RiskClass.CREDENTIAL_USE
            else [],
            filesystem_access="none",
            timeout_seconds=60,
            idempotency="safe",
        )

    observe = dict(common)
    observe["properties"] = {
        **common["properties"],
        "page_url": common["properties"]["page_url"],
    }
    observe["required"] = ["tab_id"]
    navigate = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tab_id": common["properties"]["tab_id"],
            "url": {"type": "string", "minLength": 1, "maxLength": 16384},
            "page_url": common["properties"]["page_url"],
        },
        "required": ["tab_id", "url"],
    }
    interact = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tab_id": common["properties"]["tab_id"],
            "page_url": common["properties"]["page_url"],
            "operation": {
                "type": "string",
                "enum": ["click", "fill", "select", "press", "extract", "wait"],
            },
            "locator": {
                "type": "object",
                "maxProperties": 8,
                "additionalProperties": {"type": "string", "maxLength": 1000},
            },
            "non_secret_text": {"type": "string", "maxLength": 4000},
            "credential_ref": {"type": "string", "maxLength": 200},
            "value": {"type": "string", "maxLength": 4000},
            "key": {"type": "string", "maxLength": 80},
        },
        "required": ["tab_id", "page_url", "operation", "locator"],
    }
    replay = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tab_id": common["properties"]["tab_id"],
            "page_url": common["properties"]["page_url"],
            "url": {"type": "string", "minLength": 1, "maxLength": 16384},
            "method": {"type": "string", "pattern": "^[A-Za-z]{1,16}$"},
            "body": {"type": ["string", "null"], "maxLength": 65536},
            "credential_ref": {"type": "string", "maxLength": 200},
        },
        "required": ["tab_id", "url", "method"],
    }
    rule = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "match": {
                "type": "object",
                "maxProperties": 16,
                "additionalProperties": True,
            },
            "action": {
                "type": "object",
                "maxProperties": 16,
                "additionalProperties": True,
            },
            "priority": {"type": "integer", "minimum": 0, "maximum": 10000},
            "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
        },
        "required": ["action"],
    }
    return {
        "browser.observe": spec(
            "browser.observe",
            "Read a bounded, untrusted page and tab snapshot from the active native browser.",
            observe,
            risk=RiskClass.PASSIVE,
        ),
        "browser.navigate": spec(
            "browser.navigate",
            "Navigate one selected browser tab to an in-scope URL.",
            navigate,
            risk=RiskClass.ACTIVE_SCAN,
            network=True,
            target="url",
        ),
        "browser.control": spec(
            "browser.control",
            "Control history, reload, stop, or wait for the selected tab.",
            {
                **common,
                "properties": {
                    **common["properties"],
                    "action": {
                        "type": "string",
                        "enum": ["back", "forward", "reload", "stop", "wait"],
                    },
                },
                "required": ["tab_id", "action"],
            },
            risk=RiskClass.ACTIVE_SCAN,
        ),
        "browser.interact": spec(
            "browser.interact",
            "Interact with one uniquely matched page element; secret values must use credential_ref.",
            interact,
            risk=RiskClass.ACTIVE_SCAN,
            network=True,
            target="page_url",
        ),
        "browser.extract": spec(
            "browser.extract",
            "Extract bounded semantic text from one page element without reading cookies or storage.",
            {
                **interact,
                "properties": {
                    **interact["properties"],
                    "operation": {"type": "string", "const": "extract"},
                },
                "required": ["tab_id", "operation", "locator"],
            },
            risk=RiskClass.PASSIVE,
        ),
        "browser.replay": spec(
            "browser.replay",
            "Replay an in-scope request through the selected identity without reusable secret headers.",
            replay,
            risk=RiskClass.ACTIVE_SCAN,
            network=True,
            target="url",
        ),
        "browser.capture_evidence": spec(
            "browser.capture_evidence",
            "Persist a bounded semantic browser capture as evidence.",
            observe,
            risk=RiskClass.PASSIVE,
        ),
        "proxy.observe": spec(
            "proxy.observe",
            "Read proxy status, rules, and bounded traffic summaries.",
            {"type": "object", "additionalProperties": False, "properties": {}},
            risk=RiskClass.PASSIVE,
        ),
        "proxy.configure": spec(
            "proxy.configure",
            "Change only the active session proxy configuration within the lease; upstream credentials remain opaque references.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "capture_mode": {
                        "type": "string",
                        "enum": ["metadata", "headers", "bodies"],
                    },
                    "interception_enabled": {"type": "boolean"},
                    "upstream_proxy": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "url": {"type": "string", "maxLength": 2048},
                            "credential_ref": {"type": "string", "maxLength": 200},
                        },
                        "required": ["enabled"],
                    },
                },
                "required": [],
            },
            risk=RiskClass.ACTIVE_SCAN,
        ),
        "proxy.add_rule": spec(
            "proxy.add_rule",
            "Install a bounded declarative HTTP or WebSocket rule owned by this run.",
            rule,
            risk=RiskClass.ACTIVE_SCAN,
        ),
        "proxy.remove_rule": spec(
            "proxy.remove_rule",
            "Disable a proxy rule owned by this run.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "rule_id": {"type": "string", "minLength": 1, "maxLength": 200}
                },
                "required": ["rule_id"],
            },
            risk=RiskClass.ACTIVE_SCAN,
        ),
        "proxy.replay": spec(
            "proxy.replay",
            "Replay a captured request through the selected browser identity.",
            replay,
            risk=RiskClass.ACTIVE_SCAN,
            network=True,
            target="url",
        ),
        "proxy.emergency_stop": spec(
            "proxy.emergency_stop",
            "Revoke the current browser lease and disable its pending commands and rules.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"reason": {"type": "string", "maxLength": 4000}},
            },
            risk=RiskClass.ACTIVE_SCAN,
        ),
    }


class BrowserAutomationBroker:
    """Queue native work only after the durable tool ledger accepts the call."""

    def __init__(
        self, store: NebulaStore, automation: BrowserAutomationService
    ) -> None:
        self.store = store
        self.automation = automation
        self.specs = autonomous_browser_specs()
        self.ledger = StoreToolLedger(store)

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult:
        del scope
        if approval is not None:
            raise InvalidToolArguments(
                "autonomous browser commands use the run lease, not per-call approval"
            )
        spec = self.specs.get(invocation.tool_name)
        if spec is None:
            raise InvalidToolArguments(
                f"unknown browser automation capability: {invocation.tool_name}"
            )
        errors = sorted(
            Draft202012Validator(spec.input_schema).iter_errors(invocation.arguments),
            key=lambda item: list(item.path),
        )
        if errors:
            raise InvalidToolArguments(errors[0].message)
        call = await self.ledger.reserve(invocation, spec)
        if call.status == ToolCallStatus.COMPLETE and isinstance(call.result, dict):
            return ToolExecutionResult(output=call.result)
        if call.status not in {ToolCallStatus.PROPOSED, ToolCallStatus.FAILED}:
            raise AmbiguousToolState(
                "browser automation has an unfinished durable tool state"
            )
        running = await self.ledger.transition(call, ToolCallStatus.RUNNING)
        try:
            lease = self.automation.active_lease_for_run(invocation.run_id)
            lease_id = lease.id
            if invocation.tool_name == "proxy.observe":
                session = self.store.get(BrowserSession, lease.session_id)
                status = self.automation.status(
                    lease.engagement_id, run_id=lease.run_id
                )
                output = {
                    "status": "complete",
                    "session": {
                        "id": session.id,
                        "capture_mode": session.capture_mode.value,
                        "proxy_enabled": session.proxy_enabled,
                        "interception_enabled": session.interception_enabled,
                        "upstream_proxy_enabled": session.upstream_proxy_enabled,
                        "upstream_proxy_configured": bool(session.upstream_proxy_url),
                    },
                    "traffic_count": len(
                        self.store.list_entities(
                            BrowserTrafficExchange,
                            engagement_id=session.engagement_id,
                            limit=1_000,
                        )
                    ),
                    "rules": [rule.model_dump(mode="json") for rule in status.rules],
                }
            elif invocation.tool_name == "proxy.configure":
                session = self.store.get(BrowserSession, lease.session_id)
                changes: dict[str, Any] = {}
                if "capture_mode" in invocation.arguments:
                    try:
                        changes["capture_mode"] = BrowserCaptureMode(
                            str(invocation.arguments["capture_mode"])
                        )
                    except ValueError as exc:
                        raise InvalidToolArguments(
                            "unsupported proxy capture mode"
                        ) from exc
                if "interception_enabled" in invocation.arguments:
                    changes["interception_enabled"] = bool(
                        invocation.arguments["interception_enabled"]
                    )
                if "upstream_proxy" in invocation.arguments:
                    upstream = invocation.arguments["upstream_proxy"]
                    if upstream is None:
                        changes.update(
                            {
                                "upstream_proxy_enabled": False,
                                "upstream_proxy_url": None,
                                "upstream_proxy_credential_ref": None,
                            }
                        )
                    else:
                        if (
                            not isinstance(upstream, dict)
                            or not upstream.get("enabled")
                            or not upstream.get("url")
                        ):
                            raise InvalidToolArguments(
                                "enabled upstream_proxy configuration requires a URL"
                            )
                        changes.update(
                            {
                                "upstream_proxy_enabled": True,
                                "upstream_proxy_url": str(upstream["url"]),
                                "upstream_proxy_credential_ref": upstream.get(
                                    "credential_ref"
                                ),
                            }
                        )
                if not changes:
                    raise InvalidToolArguments(
                        "proxy.configure requires a capture_mode or interception_enabled change"
                    )
                updated, _ = self.store.update_with_operation_event(
                    BrowserSession,
                    session.id,
                    changes,
                    expected_revision=session.revision,
                    operation_id=session.id,
                    operation_kind="browser_session",
                    engagement_id=session.engagement_id,
                    event_type="browser_session.autonomous_proxy_configured",
                    event_payload={"run_id": lease.run_id, "fields": sorted(changes)},
                    actor_id=invocation.requested_by,
                )
                output = {
                    "status": "complete",
                    "session_id": updated.id,
                    "revision": updated.revision,
                    "capture_mode": updated.capture_mode.value,
                    "interception_enabled": updated.interception_enabled,
                    "upstream_proxy_enabled": updated.upstream_proxy_enabled,
                    "upstream_proxy_configured": bool(updated.upstream_proxy_url),
                    "native_worker": "pending_or_active",
                }
            elif invocation.tool_name == "proxy.add_rule":
                rule = self.automation.add_rule(
                    lease_id,
                    BrowserProxyRuleRequest.model_validate(invocation.arguments),
                    invocation.requested_by,
                )
                output = {
                    "status": "complete",
                    "rule": rule.model_dump(mode="json"),
                    "rule_id": rule.id,
                }
            elif invocation.tool_name == "proxy.remove_rule":
                rule = self.automation.remove_rule(
                    str(invocation.arguments["rule_id"]), invocation.requested_by
                )
                output = {
                    "status": "complete",
                    "rule_id": rule.id,
                    "enabled": rule.enabled,
                }
            elif invocation.tool_name == "proxy.emergency_stop":
                count = self.automation.revoke_run(
                    invocation.run_id,
                    str(
                        invocation.arguments.get("reason")
                        or "stopped by autonomous browser"
                    ),
                    invocation.requested_by,
                )
                output = {"status": "stopped", "revoked_leases": count}
            else:
                command_arguments = dict(invocation.arguments)
                command_arguments.pop("lease_id", None)
                command = self.automation.enqueue_command(
                    lease_id,
                    BrowserCommandCreateRequest.model_validate(command_arguments),
                    invocation.requested_by,
                )
                command = await self.automation.wait_for_command(
                    command.id, min(float(spec.timeout_seconds), 60.0)
                )
                output = {
                    "status": command.status.value,
                    "command_id": command.id,
                    "result": command.result,
                    "evidence_ids": command.evidence_ids,
                    "error": command.error,
                }
                if command.status in {
                    BrowserCommandStatus.FAILED,
                    BrowserCommandStatus.EXPIRED,
                    BrowserCommandStatus.CANCELLED,
                }:
                    output["retryable"] = (
                        command.status != BrowserCommandStatus.CANCELLED
                    )
            await self.ledger.transition(
                running, ToolCallStatus.COMPLETE, result=output
            )
            return ToolExecutionResult(
                output=output, evidence_ids=output.get("evidence_ids", [])
            )
        except Exception as exc:
            await self.ledger.transition(running, ToolCallStatus.FAILED, error=str(exc))
            raise


class BrowserAutomationToolPlatform:
    def __init__(
        self, store: NebulaStore, automation: BrowserAutomationService
    ) -> None:
        self.store = store
        self.automation = automation

    def mission_components(
        self, run: Any, provider: ModelProvider
    ) -> MissionComponents:
        selected = run.metadata.get("tool_names")
        if not isinstance(selected, list) or not selected:
            raise MissionConfigurationError(
                "browser mission has no runtime capabilities"
            )
        specs = autonomous_browser_specs()
        unknown = sorted(set(selected) - specs.keys())
        if unknown:
            raise MissionConfigurationError(
                f"frozen browser capabilities are unavailable: {unknown}"
            )
        engagement = self.store.get(Engagement, run.engagement_id)
        scope = self.store.get(ScopePolicy, engagement.scope_policy_id or "")
        broker = BrowserAutomationBroker(self.store, self.automation)
        workspace = Path(engagement.workspace_path or ".").resolve()
        specialist = BrokeredToolSpecialist(
            provider,
            role=SpecialistRole.NETWORK_SERVICE,
            broker=broker,
            scope=scope,
            workspace=workspace,
            specs={name: specs[name] for name in selected},
            model=run.supervisor_model,
            max_output_tokens=min(2_048, run.budget.max_tokens or 2_048),
        )
        return MissionComponents(
            supervisor=ToolMissionSupervisor({name: specs[name] for name in selected}),
            specialists={SpecialistRole.NETWORK_SERVICE: specialist},
            context={
                "browser_automation": True,
                "scope_policy_revision": scope.revision,
            },
        )


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
            "required": [
                "tab_id",
                "kind",
                "locator",
                "arguments",
                "proposal",
                "page_url",
            ],
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
        # Proposals remain inert, but their metadata must accurately tell
        # mission policy that navigation/replay can cause network effects.
        risk_class=RiskClass.ACTIVE_SCAN,
        network_access=True,
        target_argument="page_url",
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
            raise InvalidToolArguments(
                "browser proposal execution does not accept tool approval"
            )
        if invocation.tool_name != self.spec.name:
            raise InvalidToolArguments(
                f"unknown browser capability: {invocation.tool_name}"
            )
        if invocation.engagement_id != self.session.engagement_id:
            raise InvalidToolArguments("browser session belongs to another Project")
        if scope.engagement_id != invocation.engagement_id:
            raise InvalidToolArguments("browser scope belongs to another Project")
        errors = sorted(
            Draft202012Validator(self.spec.input_schema).iter_errors(
                invocation.arguments
            ),
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
    if (
        primary.scope.id != browser.scope.id
        or primary.scope.revision != browser.scope.revision
    ):
        raise ValueError(
            "browser and command runtimes resolved different scope revisions"
        )
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
            item
            for item in [getattr(primary, "runtime_digest", ""), browser.runtime_digest]
            if item
        ),
    )


__all__ = [
    "BROWSER_PROPOSE_ACTION",
    "BrowserActionProposalBroker",
    "BrowserToolPlatform",
    "browser_action_spec",
    "combine_tool_components",
]
