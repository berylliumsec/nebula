"""Shared project graph capabilities for browser assistants and harnesses."""

import asyncio
from pydantic import Field
from ..domain import RiskClass, ToolCallStatus
from ..tools import (
    ToolSpec,
    IdempotencyBehavior,
    StoreToolLedger,
    ToolExecutionResult,
    InvalidToolArguments,
    AmbiguousToolState,
)
from ..runtime_platform import RuntimeToolComponents
from .graph import Contract, GraphTransaction, Identifier
from .service import ApplicationModelService


class Discover(Contract):
    category: str | None = None
    query: str = Field(default="", max_length=200)


class Search(Discover):
    type: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


class Neighborhood(Contract):
    object_id: Identifier
    depth: int = Field(default=1, ge=0, le=3)
    limit: int = Field(default=100, ge=1, le=100)


class EvidenceRead(Contract):
    kind: str
    identifier: Identifier


class EvidenceList(Contract):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


class Updates(Contract):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


INPUTS = {
    "model.discover_schema": Discover,
    "model.search": Search,
    "model.neighborhood": Neighborhood,
    "model.list_evidence": EvidenceList,
    "model.get_evidence": EvidenceRead,
    "model.get_updates": Updates,
    "model.transact": GraphTransaction,
}
DESCRIPTIONS = {
    "model.discover_schema": "Discover project schema categories, inherited typed fields and compatible relationships. The catalog does not instantiate objects.",
    "model.search": "Search meaningful objects in this project graph, independent of browser sessions; returns current revision. Reuse matching objects, keeping authentication contexts distinct.",
    "model.neighborhood": "Read bounded object properties, relationships, claim statuses and evidence references. Cite source evidence when explaining the model; distinguish observation from interpretation.",
    "model.list_evidence": "Browse original recorded evidence metadata, without creating objects or exposing secret values.",
    "model.get_evidence": "Read project-scoped evidence context and allowlisted observed facts. Cookie presence does not prove authentication; a 403 does not prove a firewall; data-related text does not establish database topology.",
    "model.get_updates": "Read incremental durable edit history after a revision. History retains supporting/conflicting evidence, source contexts, producers and timestamps.",
    "model.transact": "Atomically submit meaningful objects, relationships, project schema definitions or dismissals. Use current expected_revision and a stable idempotency_key for retries. Every property and relationship is an evidence-bearing claim; leave unknowns unspecified and secrets out. Review acceptance never upgrades a hypothesis. For model questions, propose interpretations before applying operator-requested corrections.",
}


class ModelBroker:
    def __init__(self, store, browser_session=None, *, engagement_id=None):
        self.service = getattr(
            store, "application_model_service", None
        ) or ApplicationModelService(store)
        self.engagement_id = engagement_id or browser_session.engagement_id
        self.ledger = StoreToolLedger(store)
        self.specs = {
            name: ToolSpec(
                name=name,
                version="2",
                description=DESCRIPTIONS[name],
                input_schema=schema.model_json_schema(),
                output_schema={"type": "object"},
                risk_class=RiskClass.PASSIVE,
                network_access=False,
                filesystem_access="none",
                timeout_seconds=10,
                idempotency=IdempotencyBehavior.SAFE,
            )
            for name, schema in INPUTS.items()
        }

    async def execute(self, invocation, scope, *, approval=None):
        if (
            invocation.engagement_id != self.engagement_id
            or scope.engagement_id != self.engagement_id
        ):
            raise InvalidToolArguments("Model context belongs to another project")
        schema = INPUTS.get(invocation.tool_name)
        if schema is None:
            raise InvalidToolArguments("Unknown model capability")
        body = schema.model_validate(invocation.arguments)
        call = await self.ledger.reserve(invocation, self.specs[invocation.tool_name])
        if call.status == ToolCallStatus.COMPLETE:
            return ToolExecutionResult(output=call.result)
        if call.status not in {ToolCallStatus.PROPOSED, ToolCallStatus.FAILED}:
            raise AmbiguousToolState("Model tool call is already in progress")
        running = await self.ledger.transition(call, ToolCallStatus.RUNNING)
        try:
            if invocation.tool_name == "model.transact":
                output = await asyncio.to_thread(
                    self.service.transact,
                    self.engagement_id,
                    body,
                    producer="assistant",
                )
            else:
                method = {
                    "model.discover_schema": "schema",
                    "model.search": "search",
                    "model.neighborhood": "neighborhood",
                    "model.list_evidence": "evidence_list",
                    "model.get_evidence": "evidence",
                    "model.get_updates": "history",
                }[invocation.tool_name]
                output = await asyncio.to_thread(
                    getattr(self.service, method),
                    self.engagement_id,
                    **body.model_dump(),
                )
            await self.ledger.transition(
                running, ToolCallStatus.COMPLETE, result=output
            )
            return ToolExecutionResult(output=output)
        except Exception as exc:
            await self.ledger.transition(running, ToolCallStatus.FAILED, error=str(exc))
            raise


def components(store, session, scope, workspace):
    broker = ModelBroker(store, session)
    return RuntimeToolComponents(
        broker=broker,
        scope=scope,
        workspace=workspace,
        specs=broker.specs,
        runtime_digest="application-model-v2",
    )


def project_components(store, engagement_id, scope, workspace):
    broker = ModelBroker(store, engagement_id=engagement_id)
    return RuntimeToolComponents(
        broker=broker,
        scope=scope,
        workspace=workspace,
        specs=broker.specs,
        runtime_digest="application-model-project-v2",
    )


def standalone_components(store, engagement_id):
    """Graph-only access needs no command runtime or network scope configuration."""
    from pathlib import Path
    from ..domain import Engagement, ScopePolicy

    project = store.get(Engagement, engagement_id)
    scope = (
        store.get(ScopePolicy, project.scope_policy_id)
        if project.scope_policy_id
        else ScopePolicy(id=f"scope:{project.id}", engagement_id=project.id)
    )
    return project_components(
        store, engagement_id, scope, Path(project.workspace_path or ".").resolve()
    )
