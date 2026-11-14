"""Shared project graph capabilities for browser assistants and harnesses."""

from pydantic import Field
from fastapi import HTTPException
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
from ..tools import GuidedToolError
from .graph import Contract, GraphTransaction, Identifier
from .service import ApplicationModelService


def _core_literals() -> frozenset[str]:
    """Fixed refusal sentences Core writes in this package, read from its source.

    A refusal is shown to the model only when it is one of these literals, so
    no argument value or graph content is copied into the failure.
    """

    import ast
    from pathlib import Path

    found: set[str] = set()
    for path in Path(__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"ValueError", "HTTPException"}
            ):
                for argument in node.args:
                    for value in ast.walk(argument):
                        if isinstance(value, ast.Constant) and isinstance(
                            value.value, str
                        ):
                            found.add(value.value)
    return frozenset(item for item in found if 8 <= len(item) <= 300)


_CORE_LITERALS: frozenset[str] | None = None
# Core's own words before an interpolated value, e.g. "Unknown relationship: x".
_INTERPOLATED_PREFIXES = (
    "Unknown relationship endpoint type",
    "Unknown relationship",
    "Unknown property",
    "Unknown object type",
    "Unknown identity property",
    "Unknown category",
    "Duplicate identity hint",
    "Cyclic type inheritance",
)


def _refusal(exc: ValueError | HTTPException, tool_name: str) -> GuidedToolError:
    """Turn a model service refusal into a model-safe, actionable tool failure."""

    global _CORE_LITERALS
    if _CORE_LITERALS is None:
        _CORE_LITERALS = _core_literals()
    status = exc.status_code if isinstance(exc, HTTPException) else None
    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
    message = detail.get("message") if isinstance(detail, dict) else detail
    message = message if isinstance(message, str) else ""
    writing = tool_name == "model.transact"
    outcome = "Nothing was saved." if writing else "Nothing changed."
    if message in _CORE_LITERALS:
        problem = message
    elif message.startswith("Invalid ") and " value: " in message:
        problem = message.split(":", 1)[0]
    else:
        problem = next(
            (
                prefix
                for prefix in _INTERPOLATED_PREFIXES
                if message.startswith(prefix + ":")
            ),
            "The project model refused this request.",
        )
    problem = f"{problem.rstrip('.')}. {outcome}"
    current = detail.get("current_revision") if isinstance(detail, dict) else None
    if status == 409 and isinstance(current, int) and not isinstance(current, bool):
        category, field = "conflict", "expected_revision"
        next_action = (
            f"The model is now at revision {current}. Read what changed "
            "(model.get_updates or model.search), then submit a new transaction "
            f"with expected_revision {current} and a new idempotency_key."
        )
    elif status == 409:
        category, field = "conflict", None
        next_action = (
            "Read the current model (model.search or model.get_updates) before "
            "submitting a new transaction with a new idempotency_key."
        )
    elif status == 404:
        category, field = "unavailable_resource", None
        next_action = (
            "Use model.search or model.neighborhood to choose an object or "
            "evidence ID that exists in this project."
        )
    else:
        category, field = "invalid_arguments", "operations" if writing else None
        next_action = (
            "For a relationship, call model.relationship_options with its source "
            "and target IDs and use a listed type; for types, properties and "
            "evidence rules, call model.discover_schema. Then submit a corrected "
            "transaction with a new idempotency_key."
            if writing
            else "Call model.discover_schema or model.search to choose valid input, then call again."
        )
    return GuidedToolError(
        message or type(exc).__name__,
        code=f"application_model.{category}",
        category=category,
        problem=problem,
        next_action=next_action,
        field=field,
        # Refusals come before the graph commit and roll back.
        before_execution=True,
    )


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


class RelationshipOptions(Contract):
    source_id: Identifier
    target_id: Identifier


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
    "model.relationship_options": RelationshipOptions,
}
DESCRIPTIONS = {
    "model.relationship_options": "Discover valid relationship types and directions for two existing project objects. Use this before connecting objects; hosts/exposes are not built-in names. Only choose meanings supported by evidence, or define a project custom. relationship when needed.",
    "model.discover_schema": "Discover project schema categories, inherited typed fields and compatible relationships. The catalog does not instantiate objects.",
    "model.search": "Search meaningful objects in this project graph, independent of browser sessions; returns current revision. Reuse matching objects, keeping authentication contexts distinct.",
    "model.neighborhood": "Read bounded object properties, relationships, claim statuses and evidence references. Cite source evidence when explaining the model; distinguish observation from interpretation.",
    "model.list_evidence": "Browse original recorded evidence metadata, without creating objects or exposing secret values.",
    "model.get_evidence": "Read project-scoped evidence context and allowlisted observed facts. Cookie presence does not prove authentication; a 403 does not prove a firewall; data-related text does not establish database topology.",
    "model.get_updates": "Read incremental durable edit history after a revision. History retains supporting/conflicting evidence, source contexts, producers and timestamps.",
    "model.transact": "Submit application-model objects, relationships, schema definitions, or dismissals using current revisions and evidence.",
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
                output = await self.service.transact(
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
                    "model.relationship_options": "relationship_options",
                }[invocation.tool_name]
                output = await getattr(self.service, method)(
                    self.engagement_id, **body.model_dump()
                )
        except (ValueError, HTTPException) as exc:
            # diagnostic-expected: validation is persisted as a failed tool receipt below.
            message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            recovery = {
                "schema_tool": "model.discover_schema",
                "relationship_tool": "model.relationship_options",
            }
            if invocation.tool_name == "model.transact":
                for operation in body.operations:
                    if operation.op == "put_relationship":
                        try:
                            recovery[
                                "relationship_options"
                            ] = await self.service.relationship_options(
                                self.engagement_id,
                                operation.source,
                                operation.target,
                            )
                        except (ValueError, HTTPException):
                            # diagnostic-expected: unsaved endpoints use the schema recovery above.
                            pass  # New transaction-local endpoints require schema discovery.
                        break
            output = {"status": "failed", "error": message, "recovery": recovery}
            await self.ledger.transition(
                running, ToolCallStatus.FAILED, error=message, result=output
            )
            # Direct callers read the output; a chat loop gives the model this
            # Core-authored refusal instead of a generic failure.
            return ToolExecutionResult(
                output=output,
                exit_code=1,
                failure_error=_refusal(exc, invocation.tool_name),
            )
        except Exception as exc:
            await self.ledger.transition(running, ToolCallStatus.FAILED, error=str(exc))
            raise
        # Outside the refusal handler: the service already committed this result.
        await self.ledger.transition(running, ToolCallStatus.COMPLETE, result=output)
        return ToolExecutionResult(output=output)


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


async def standalone_components(store, engagement_id):
    """Graph-only access needs no command runtime or network scope configuration."""
    from pathlib import Path
    from ..domain import Engagement, ScopePolicy

    project = await store.get(Engagement, engagement_id)
    scope = (
        (await store.get(ScopePolicy, project.scope_policy_id))
        if project.scope_policy_id
        else ScopePolicy(id=f"scope:{project.id}", engagement_id=project.id)
    )
    return project_components(
        store, engagement_id, scope, Path(project.workspace_path or ".").resolve()
    )
