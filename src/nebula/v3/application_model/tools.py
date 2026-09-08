"""Bounded model tools for the existing browser chat context."""

from ..domain import NebulaModel, RiskClass, ToolCallStatus
from ..tools import (
    ToolSpec,
    IdempotencyBehavior,
    StoreToolLedger,
    ToolExecutionResult,
    InvalidToolArguments,
    AmbiguousToolState,
)
from ..runtime_platform import RuntimeToolComponents
from .domain import ModelSession, KnowledgeState, Observation
from .api import QueryRequest, AssertionRequest
from .service import ApplicationModelService


class ReadState(NebulaModel):
    collection_id: str
    state_id: str


class CollectionsRead(NebulaModel):
    pass


class StatesRead(NebulaModel):
    collection_id: str


class UpdatesRead(NebulaModel):
    collection_id: str
    after_state_id: str | None = None


class CompareStates(NebulaModel):
    collection_id: str
    left: str
    right: str


class EvidenceRead(NebulaModel):
    collection_id: str
    observation_id: str


class Propose(AssertionRequest):
    collection_id: str


class Check(QueryRequest):
    collection_id: str


INPUTS = {
    "model.list_collections": CollectionsRead,
    "model.list_states": StatesRead,
    "model.get_updates": UpdatesRead,
    "model.get_state": ReadState,
    "model.diff_states": CompareStates,
    "model.get_evidence": EvidenceRead,
    "model.propose_assertion": Propose,
    "model.check_constraints": Check,
}


class ModelBroker:
    def __init__(self, store, browser_session=None, *, engagement_id=None):
        self.service = getattr(
            store, "application_model_service", None
        ) or ApplicationModelService(store)
        self.browser_session = browser_session
        self.engagement_id = engagement_id or browser_session.engagement_id
        scope_label = "selected browser context" if browser_session else "project"
        self.ledger = StoreToolLedger(store)
        self.specs = {
            name: ToolSpec(
                name=name,
                version="1",
                description={
                    "model.list_collections": f"List recorded model collections for this {scope_label}.",
                    "model.list_states": "List up to 100 knowledge states in a recorded collection.",
                    "model.get_updates": "Read up to 100 immutable states and normalized observations added after a state checkpoint. Returns the next checkpoint for incremental live-browser analysis.",
                    "model.get_state": "Read a bounded recorded knowledge state and typed properties.",
                    "model.diff_states": "Compare two recorded knowledge states.",
                    "model.get_evidence": "Read the normalized observation and its source evidence references.",
                    "model.propose_assertion": "Persist an untrusted, evidence-linked model assertion proposal.",
                    "model.check_constraints": "Check a condition against recorded facts and explicitly selected assumptions; no interaction execution.",
                }[name],
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
            or scope.engagement_id != invocation.engagement_id
        ):
            raise InvalidToolArguments("Model context belongs to another project")
        schema = INPUTS.get(invocation.tool_name)
        if schema is None:
            raise InvalidToolArguments("Unknown model capability")
        body = schema.model_validate(invocation.arguments)
        project = invocation.engagement_id
        collection = (
            self.service.get(ModelSession, project, body.collection_id)
            if hasattr(body, "collection_id")
            else None
        )
        if (
            collection
            and self.browser_session is not None
            and collection.browser_session_id != self.browser_session.id
        ):
            raise InvalidToolArguments("Collection belongs to another browser session")
        call = await self.ledger.reserve(invocation, self.specs[invocation.tool_name])
        if call.status == ToolCallStatus.COMPLETE:
            return ToolExecutionResult(output=call.result)
        if call.status not in {ToolCallStatus.PROPOSED, ToolCallStatus.FAILED}:
            raise AmbiguousToolState("Model tool call is already in progress")
        running = await self.ledger.transition(call, ToolCallStatus.RUNNING)
        try:
            if invocation.tool_name == "model.list_collections":
                records = [
                    s
                    for s in self.service.list(ModelSession, project)
                    if self.browser_session is None
                    or s.browser_session_id == self.browser_session.id
                ]
                output = {
                    "collections": [r.model_dump(mode="json") for r in records[:100]],
                    "truncated": len(records) > 100,
                }
            elif invocation.tool_name == "model.list_states":
                records = self.service.list(KnowledgeState, project, collection.id)
                output = {
                    "states": [r.model_dump(mode="json") for r in records[-100:]],
                    "truncated": len(records) > 100,
                }
            elif invocation.tool_name == "model.get_updates":
                records = self.service.list(KnowledgeState, project, collection.id)
                start = 0
                if body.after_state_id:
                    self.service.get(
                        KnowledgeState,
                        project,
                        body.after_state_id,
                        collection.id,
                    )
                    start = next(
                        index + 1
                        for index, item in enumerate(records)
                        if item.id == body.after_state_id
                    )
                selected = records[start : start + 100]
                observation_ids = {
                    identifier
                    for item in selected
                    for identifier in item.observation_ids
                }
                observations = [
                    item
                    for item in self.service.list(Observation, project, collection.id)
                    if item.id in observation_ids
                ]
                output = {
                    "states": [item.model_dump(mode="json") for item in selected],
                    "observations": [
                        item.model_dump(mode="json") for item in observations
                    ],
                    "checkpoint_state_id": (
                        selected[-1].id if selected else body.after_state_id
                    ),
                    "truncated": start + len(selected) < len(records),
                }
            elif invocation.tool_name == "model.get_state":
                state = self.service.get(
                    KnowledgeState, project, body.state_id, collection.id
                )
                fields = self.service.fields(project, collection.id, state.id)
                output = {
                    "state": state.model_dump(mode="json"),
                    "fields": dict(list(fields.items())[:100]),
                    "fields_truncated": len(fields) > 100,
                }
            elif invocation.tool_name == "model.diff_states":
                output = self.service.diff(
                    project, collection.id, body.left, body.right
                )
                output["truncated"] = len(output["changes"]) > 100
                output["changes"] = output["changes"][:100]
            elif invocation.tool_name == "model.get_evidence":
                output = self.service.get(
                    Observation, project, body.observation_id, collection.id
                ).model_dump(mode="json")
            elif invocation.tool_name == "model.propose_assertion":
                output = self.service.propose(
                    project,
                    collection.id,
                    AssertionRequest.model_validate(
                        body.model_dump(exclude={"collection_id"})
                    ),
                ).model_dump(mode="json")
            else:
                query = await self.service.submit(
                    project,
                    collection.id,
                    QueryRequest.model_validate(
                        body.model_dump(exclude={"collection_id"})
                    ),
                )
                task = self.service.queries.get(query.id)
                if task:
                    await task
                from .domain import SolverQuery

                output = self.service.get(
                    SolverQuery, project, query.id, collection.id
                ).model_dump(mode="json")
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
        runtime_digest="application-model-v1",
    )


def project_components(store, engagement_id, scope, workspace):
    """Expose project-scoped recorded knowledge through the harness MCP gateway."""
    broker = ModelBroker(store, engagement_id=engagement_id)
    return RuntimeToolComponents(
        broker=broker,
        scope=scope,
        workspace=workspace,
        specs=broker.specs,
        runtime_digest="application-model-project-v1",
    )
