import asyncio
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from nebula.v3.storage import NebulaStore
from nebula.v3.domain import (
    Engagement,
    BrowserSession,
    BrowserIdentity,
    BrowserTrafficExchange,
    CompanionRequest,
)
from nebula.v3.browser_companion import BrowserCompanion
from nebula.v3.browser_engine import BrowserEngineRegistry
from nebula.v3.database import ApplicationModelOutboxRow
from nebula.v3.application_model.domain import Value, Formula, KnowledgeState
from nebula.v3.application_model.service import ApplicationModelService
from nebula.v3.application_model.solver import solve, isolated_solve


@pytest.fixture
def model(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Recorded model"))
    identity = store.create(BrowserIdentity(engagement_id=project.id, name="Reader"))
    browser = store.create(
        BrowserSession(
            engagement_id=project.id, identity_id=identity.id, name="Fixture"
        )
    )
    service = ApplicationModelService(store)
    collection = service.create(project.id, browser.id)
    return service, project, browser, collection


def exchange(service, project, browser, tab="a", **kwargs):
    return service.store.create(
        BrowserTrafficExchange(
            engagement_id=project.id,
            session_id=browser.id,
            identity_id=browser.identity_id,
            tab_id=tab,
            method="GET",
            url="https://example.test/records/1?token=secret",
            scope_state="in_scope",
            scope_policy_id="fixture",
            scope_policy_revision=1,
            **kwargs,
        )
    )


def test_projection_deduplicates_history_and_retains_branches(model):
    service, project, browser, collection = model
    first = exchange(service, project, browser, status_code=200)
    exchange(service, project, browser, tab="b", status_code=204)
    service.import_history(project.id, collection.id)
    service.process_batch()
    service.import_history(project.id, collection.id)
    service.process_batch()
    workspace = service.workspace(project.id, collection.id)
    assert len(workspace["observations"]) == 2
    assert len(workspace["states"]) == 2
    assert all(not state.parent_state_ids for state in workspace["states"])
    assert "secret" not in str(workspace)
    before = workspace["states"][0].model_dump()
    exchange(service, project, browser, status_code=201)
    service.process_batch()
    assert service.store.get(KnowledgeState, before["id"]).model_dump() == before
    service.remove(project.id, collection.id)
    assert service.store.get(BrowserTrafficExchange, first.id).status_code == 200


def test_shared_chromium_interaction_projects_without_page_secrets(model):
    service, project, browser, collection = model
    companion = BrowserCompanion(service.store, BrowserEngineRegistry([]))
    companion._record_interaction(
        browser,
        CompanionRequest(
            operation="capture",
            tab_id="shared-tab",
            url="https://example.test/account?token=private",
        ),
        {
            "url": "https://example.test/account?token=private",
            "page_revision": "revision-1",
            "elements": [{"id": "secret-control"}],
            "text": "private page body",
        },
        assistant=True,
        chat_turn_id="turn-1",
    )
    service.process_batch()
    workspace = service.workspace(project.id, collection.id)
    assert len(workspace["observations"]) == 1
    facts = workspace["observations"][0].facts
    assert facts["operation"].value == "capture"
    assert facts["route"].value == "https://example.test/account"
    assert facts["element_count"].value == 1
    assert "private page body" not in str(workspace)


def test_pause_and_resume(model):
    service, project, browser, collection = model
    service.transition(project.id, collection.id, "paused")
    exchange(service, project, browser, status_code=200)
    service.process_batch()
    assert not service.workspace(project.id, collection.id)["states"]
    service.transition(project.id, collection.id, "active")
    service.import_history(project.id, collection.id)
    service.process_batch()
    assert len(service.workspace(project.id, collection.id)["states"]) == 1


def test_cross_project_reference_rejected(model):
    service, project, browser, collection = model
    other = service.store.create(Engagement(name="Other"))
    with pytest.raises(Exception, match="does not belong"):
        service.create(other.id, browser.id)
    with pytest.raises(Exception, match="does not belong"):
        service.workspace(other.id, collection.id)


def test_outbox_rollback_is_atomic(model):
    service, project, browser, collection = model
    with pytest.raises(RuntimeError):
        with service.store.transaction() as tx:
            tx.add(
                BrowserTrafficExchange(
                    engagement_id=project.id,
                    session_id=browser.id,
                    identity_id=browser.identity_id,
                    tab_id="x",
                    method="GET",
                    url="https://example.test",
                    scope_state="in_scope",
                    scope_policy_id="fixture",
                    scope_policy_revision=1,
                )
            )
            raise RuntimeError("rollback")
    with service.store.database.session() as db:
        assert not db.scalars(select(ApplicationModelOutboxRow)).all()


def condition(op="eq", value=7):
    return {
        "op": op,
        "args": [{"op": "field", "field": "count"}, {"op": "literal", "value": value}],
    }


def test_solver_consistency_unknowns_and_types():
    fields = {"count": Value(kind="concrete", type="integer", value=7).model_dump()}
    assert solve({"fields": fields, "formula": condition()})["result"] == "SAT"
    result = solve({"fields": fields, "formula": condition(value=8)})
    assert result["result"] == "UNSAT" and result["base_result"] == "SAT"
    result = solve(
        {
            "fields": fields,
            "formula": condition(),
            "assumptions": [{"id": "contradiction", "formula": condition(value=8)}],
        }
    )
    assert result["base_result"] == "UNSAT"
    assert "contradiction" in result["unsat_core"]
    fields["count"] = Value(
        kind="unknown", type="integer", reason="unobserved"
    ).model_dump()
    assert (
        solve({"fields": fields, "formula": condition(value=8)})["assignments"]["count"]
        == 8
    )
    with pytest.raises(ValueError, match="same type"):
        solve({"fields": fields, "formula": condition(value="eight")})


def test_isolated_worker():
    result = asyncio.run(
        isolated_solve({"fields": {}, "formula": {"op": "literal", "value": True}})
    )
    assert result["result"] == "SAT"


def test_value_and_formula_validation():
    with pytest.raises(ValidationError):
        Value(kind="concrete", type="integer", value=True)
    with pytest.raises(ValidationError):
        Value(kind="unknown", type="string")
    with pytest.raises(ValidationError):
        Formula(op="eval", value="anything")
    with pytest.raises(ValidationError):
        Formula(op="eq", args=[])
    original = Value(kind="unknown", type="integer", reason="redacted")
    assert Value.model_validate_json(original.model_dump_json()) == original


def test_aliases_domains_and_typed_assignments():
    fields = {
        "count": Value(kind="concrete", type="integer", value=7).model_dump(),
        "copy": Value(kind="alias", type="integer", reference="count").model_dump(),
        "mode": Value(
            kind="unknown", type="enum", reason="unobserved", domain=["draft", "saved"]
        ).model_dump(),
    }
    result = solve({"fields": fields, "formula": condition()})
    assert result["assignments"]["copy"] == 7
    assert result["assignment_types"]["copy"] == "integer"
    assert result["assignments"]["mode"] in {"draft", "saved"}
    fields["copy"]["reference"] = "missing"
    with pytest.raises(ValueError, match="existing field"):
        solve({"fields": fields, "formula": condition()})
    with pytest.raises(ValidationError):
        Value(kind="unknown", type="integer", reason="unobserved", domain=["7"])


def test_projection_retry_rolls_back_checkpoint(model, monkeypatch):
    from nebula.v3.storage import StoreTransaction

    service, project, browser, collection = model
    exchange(service, project, browser, status_code=200)
    original = StoreTransaction.add_all

    def fail(*args, **kwargs):
        raise RuntimeError("fixture projector interruption")

    monkeypatch.setattr(StoreTransaction, "add_all", fail)
    for _ in range(4):
        service.process_batch()
    workspace = service.workspace(project.id, collection.id)
    assert not workspace["states"]
    assert workspace["session"].processed_count == 0
    assert workspace["projection_errors"]
    monkeypatch.setattr(StoreTransaction, "add_all", original)
    service.retry_projection(project.id, collection.id)
    service.process_batch()
    workspace = service.workspace(project.id, collection.id)
    assert len(workspace["states"]) == 1
    assert workspace["session"].processed_count == 1
    assert not workspace["projection_errors"]


def test_late_revision_preserves_earlier_state(model):
    service, project, browser, collection = model
    record = exchange(service, project, browser, status_code=200)
    service.process_batch()
    first = service.workspace(project.id, collection.id)["states"][0]
    service.store.update(
        BrowserTrafficExchange,
        record.id,
        {"status_code": 201},
        expected_revision=record.revision,
    )
    service.process_batch()
    states = service.workspace(project.id, collection.id)["states"]
    assert len(states) == 2
    assert states[1].parent_state_ids == [first.id]
    assert service.get(KnowledgeState, project.id, first.id) == first
    with pytest.raises(ValueError, match="immutable"):
        service.store.update(
            KnowledgeState,
            first.id,
            {"branch_key": "mutated"},
            expected_revision=first.revision,
        )


def test_cancel_before_worker_start_is_durable(model):
    from nebula.v3.application_model.api import QueryRequest
    from nebula.v3.application_model.domain import SolverQuery

    service, project, browser, collection = model
    exchange(service, project, browser, status_code=200)
    service.process_batch()
    state = service.workspace(project.id, collection.id)["states"][0]

    async def run():
        query = await service.submit(
            project.id,
            collection.id,
            QueryRequest(state_id=state.id, formula=Formula(op="literal", value=True)),
        )
        result = service.cancel_query(project.id, collection.id, query.id)
        assert result.status == "cancelled"
        await asyncio.sleep(0)
        assert query.id not in service.queries
        assert service.get(SolverQuery, project.id, query.id).status == "cancelled"

    asyncio.run(run())


def test_metadata_adapters_and_project_scoped_parents(model):
    from nebula.v3.application_model.ingestion import envelope

    service, project, browser, collection = model
    record = exchange(service, project, browser, status_code=200)
    raw = {
        "id": "ws-fixture",
        "engagement_id": project.id,
        "session_id": browser.id,
        "exchange_id": record.id,
        "revision": 1,
        "created_at": record.created_at.isoformat(),
        "opcode": "text",
        "payload_preview": "private",
        "payload_bytes": 7,
    }
    with service.store.database.session() as db:
        normalized = envelope("browser_websocket_frames", raw, db)
        assert normalized["tab_id"] == "a"
        assert normalized["exchange_id"] == record.id
        assert "private" not in str(normalized)
        raw["engagement_id"] = "other-project"
        assert envelope("browser_websocket_frames", raw, db)["tab_id"] is None
    evidence = {
        "id": "evidence-fixture",
        "revision": 1,
        "captured_at": record.created_at.isoformat(),
        "tool_call_id": "tool-1",
        "metadata": {
            "browser_session_id": browser.id,
            "tab_id": "a",
            "browser_command_id": "command-1",
        },
    }
    normalized = envelope("evidence", evidence)
    assert normalized["command_id"] == "command-1"
    assert normalized["tool_call_id"] == "tool-1"
    assert normalized["evidence_ids"] == ["evidence-fixture"]


def test_agent_tools_are_offline_and_idempotent(model, tmp_path):
    from nebula.v3.domain import ScopePolicy, AgentRun
    from nebula.v3.tools import ToolInvocation, InvalidToolArguments
    from nebula.v3.application_model.tools import ModelBroker

    service, project, browser, collection = model
    exchange(service, project, browser, status_code=200)
    service.process_batch()
    broker = ModelBroker(service.store, browser)
    scope = ScopePolicy(engagement_id=project.id)
    service.store.create(
        AgentRun(
            id="fixture-run",
            engagement_id=project.id,
            objective="Inspect recorded state",
        )
    )
    assert all(
        not spec.network_access and spec.filesystem_access == "none"
        for spec in broker.specs.values()
    )
    invocation = ToolInvocation(
        engagement_id=project.id,
        run_id="fixture-run",
        tool_name="model.list_collections",
        arguments={},
        workspace=tmp_path,
        idempotency_key="model-read-1",
    )

    async def run():
        first = await broker.execute(invocation, scope)
        second = await broker.execute(invocation, scope)
        assert first.output == second.output
        assert first.output["collections"][0]["id"] == collection.id
        updates = await broker.execute(
            invocation.model_copy(
                update={
                    "tool_name": "model.get_updates",
                    "arguments": {"collection_id": collection.id},
                    "idempotency_key": "model-updates-1",
                }
            ),
            scope,
        )
        assert len(updates.output["states"]) == 1
        assert len(updates.output["observations"]) == 1
        checkpoint = updates.output["checkpoint_state_id"]
        empty = await broker.execute(
            invocation.model_copy(
                update={
                    "tool_name": "model.get_updates",
                    "arguments": {
                        "collection_id": collection.id,
                        "after_state_id": checkpoint,
                    },
                    "idempotency_key": "model-updates-2",
                }
            ),
            scope,
        )
        assert empty.output["states"] == []
        assert empty.output["checkpoint_state_id"] == checkpoint
        foreign = invocation.model_copy(update={"engagement_id": "other-project"})
        with pytest.raises(InvalidToolArguments, match="another project"):
            await broker.execute(foreign, scope)

    asyncio.run(run())
