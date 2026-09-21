"""The mission specialist loop runs every call a model batches into one turn."""

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from nebula.v3.agent_tooling import BrokeredToolSpecialist
from nebula.v3.domain import (
    Approval,
    RiskClass,
    RunBudget,
    ScopePolicy,
    TaskStatus,
)
from nebula.v3.orchestration import (
    call_records,
    MissionPlan,
    MissionRuntime,
    PlannedTask,
    SpecialistApprovalRequired,
    SpecialistContext,
    SpecialistOutcome,
    SpecialistResult,
    SpecialistRole,
    _model_safe_specialist_result,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
    ToolCall,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import (
    ApprovalRequired,
    IdempotencyBehavior,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)

FINISH = "nebula.finish_task"


class ScriptedRoutingProvider(ModelProvider):
    """Answers each routing step with a scripted batch of tool calls."""

    def __init__(self, batches):
        super().__init__(
            ProviderConfig(
                id="provider-batch",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(),
            )
        )
        self.batches = list(batches)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text="",
            tool_calls=self.batches.pop(0),
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason="tool_calls",
        )

    async def health(self):
        return ProviderHealth(provider_id=self.config.id, healthy=True)


class RecordingBroker:
    """A broker that records the order calls arrive in, and can pause."""

    def __init__(self, approvals=frozenset()):
        self.calls = []
        self.approvals = set(approvals)

    async def execute(self, invocation: ToolInvocation, scope: ScopePolicy):
        del scope
        self.calls.append(invocation)
        if invocation.tool_name in self.approvals:
            raise ApprovalRequired(
                Approval(
                    id=f"approval-{invocation.tool_name}",
                    engagement_id=invocation.engagement_id,
                    run_id=invocation.run_id,
                    task_id=invocation.task_id,
                    risk_class=RiskClass.ACTIVE_SCAN,
                    exact_request={
                        "tool_name": invocation.tool_name,
                        "arguments": invocation.arguments,
                    },
                    target="10.0.0.8",
                    policy_rationale="active scanning requires operator approval",
                    requested_by="network-specialist",
                )
            )
        return ToolExecutionResult(
            output={"tool": invocation.tool_name, "ok": True},
            evidence_ids=[f"evidence-{invocation.tool_name}"],
            execution={"command": ["run", invocation.tool_name]},
            exit_code=0,
        )


def _spec(name: str, budget_class: str = "execution") -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=f"{name} capability",
        risk_class=RiskClass.LOCAL_READ,
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        output_schema={"type": "object"},
        idempotency=IdempotencyBehavior.SAFE,
        budget_class=budget_class,
    )


def _specialist(tmp_path, provider, broker, specs=None):
    return BrokeredToolSpecialist(
        provider,
        role=SpecialistRole.NETWORK_SERVICE,
        broker=broker,
        scope=ScopePolicy(id="scope-1", engagement_id="engagement-1"),
        workspace=tmp_path,
        specs=specs
        or {
            "nmap.tcp": _spec("nmap.tcp"),
            "browser.fetch": _spec("browser.fetch"),
            "tool_output.search": _spec("tool_output.search", "artifact_query"),
        },
        model="model-a",
    )


def _context(prior_turns=(), remaining_tool_calls=None, turn_index=1):
    return SpecialistContext(
        engagement_id="engagement-1",
        run_id="run-1",
        task=PlannedTask(
            id="scan",
            role=SpecialistRole.NETWORK_SERVICE,
            title="Inspect the service",
            instructions="Gather the observations the objective needs",
        ),
        objective="Map the exposed service",
        prior_results={},
        turn_index=turn_index,
        remaining_tool_calls=remaining_tool_calls,
        allowed_tools=frozenset({"nmap.tcp", "browser.fetch", "tool_output.search"}),
        prior_turns=list(prior_turns),
    )


def _call(call_id: str, name: str, arguments=None) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


def test_specialist_runs_every_call_a_model_batches_in_one_turn(tmp_path):
    provider = ScriptedRoutingProvider(
        [
            [
                _call("call-1", "nmap.tcp", {"ports": [80]}),
                _call("call-2", "browser.fetch"),
                _call("call-3", "nmap.tcp", {"ports": [443]}),
            ]
        ]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    result = asyncio.run(specialist.run(_context()))

    assert [invocation.tool_name for invocation in broker.calls] == [
        "nmap.tcp",
        "browser.fetch",
        "nmap.tcp",
    ]
    assert [invocation.arguments for invocation in broker.calls] == [
        {"ports": [80]},
        {},
        {"ports": [443]},
    ]
    # One routing round trip carried the whole batch.
    assert len(provider.requests) == 1
    assert provider.requests[0].parallel_tool_calls is True
    assert result.outcome is SpecialistOutcome.CONTINUE
    assert result.tool_calls == 3
    assert [record["model_call_id"] for record in result.output["calls"]] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert result.output["status"] == "complete"
    assert result.evidence_ids == ["evidence-nmap.tcp", "evidence-browser.fetch"]
    # Each call keeps its own durable invocation and idempotency key.
    assert len({invocation.id for invocation in broker.calls}) == 3
    assert len({invocation.idempotency_key for invocation in broker.calls}) == 3
    # The routing spend is charged once for the batch.
    assert (result.input_tokens, result.output_tokens) == (10, 5)


def test_specialist_replays_every_batched_result_to_the_next_turn(tmp_path):
    provider = ScriptedRoutingProvider(
        [
            [_call("call-1", "nmap.tcp"), _call("call-2", "browser.fetch")],
            [
                _call(
                    "call-3",
                    FINISH,
                    {
                        "status": "complete",
                        "summary": "The service is mapped",
                        "rationale": "Both observations are in hand",
                    },
                )
            ],
        ]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    first = asyncio.run(specialist.run(_context()))
    asyncio.run(
        specialist.run(
            _context(
                prior_turns=[first],
                turn_index=2,
            )
        )
    )

    replayed = provider.requests[1].tool_results
    assert [item.call_id for item in replayed] == ["call-1", "call-2"]
    assert [item.name for item in replayed] == ["nmap.tcp", "browser.fetch"]
    assert not any(item.is_error for item in replayed)


def test_specialist_finishes_when_finish_is_the_whole_response(tmp_path):
    provider = ScriptedRoutingProvider(
        [
            [
                _call(
                    "call-1",
                    FINISH,
                    {
                        "status": "complete",
                        "summary": "The service is mapped",
                        "rationale": "Every observation the objective needs is in hand",
                    },
                )
            ]
        ]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    result = asyncio.run(specialist.run(_context()))

    assert result.outcome is SpecialistOutcome.COMPLETE
    assert broker.calls == []


def test_specialist_drops_a_finish_queued_behind_tool_calls(tmp_path):
    provider = ScriptedRoutingProvider(
        [
            [
                _call("call-1", "nmap.tcp"),
                _call(
                    "call-2",
                    FINISH,
                    {
                        "status": "complete",
                        "summary": "done",
                        "rationale": "done",
                    },
                ),
                _call("call-3", "browser.fetch"),
            ]
        ]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    result = asyncio.run(specialist.run(_context()))

    # The turn cannot finish before it has inspected what it just brokered, and
    # a call queued behind the finish never runs.
    assert [invocation.tool_name for invocation in broker.calls] == ["nmap.tcp"]
    assert result.outcome is SpecialistOutcome.CONTINUE


def test_specialist_stops_a_batch_when_the_tool_call_budget_runs_out(tmp_path):
    provider = ScriptedRoutingProvider(
        [
            [
                _call("call-1", "nmap.tcp"),
                _call("call-2", "tool_output.search"),
                _call("call-3", "browser.fetch"),
            ]
        ]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    result = asyncio.run(specialist.run(_context(remaining_tool_calls=1)))

    # Retrieval is free; the second real call is dropped rather than spent.
    assert [invocation.tool_name for invocation in broker.calls] == [
        "nmap.tcp",
        "tool_output.search",
    ]
    assert result.tool_calls == 1


def test_specialist_runs_a_batched_call_that_reuses_an_id_under_a_core_id(
    tmp_path,
):
    provider = ScriptedRoutingProvider(
        [[_call("call-1", "nmap.tcp"), _call("call-1", "browser.fetch")]]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    result = asyncio.run(specialist.run(_context()))

    # A different call under a reused id is a new call: it runs under an id
    # Core makes unique, so history still pairs each call with its result.
    assert [invocation.tool_name for invocation in broker.calls] == [
        "nmap.tcp",
        "browser.fetch",
    ]
    call_ids = [record["model_call_id"] for record in call_records(result.output)]
    assert call_ids[0] == "call-1"
    assert len(set(call_ids)) == 2


def test_specialist_answers_an_unavailable_tool_and_still_runs_the_batch(
    tmp_path,
):
    provider = ScriptedRoutingProvider(
        [[_call("call-1", "nmap.tcp"), _call("call-2", "browser.fetch")]]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)
    context = _context()
    context = context.model_copy(
        update={"allowed_tools": frozenset({"nmap.tcp", "tool_output.search"})}
    )

    result = asyncio.run(specialist.run(context))

    # The unavailable call never reaches the broker; it is answered with a
    # failed observation the model reads on its next turn.
    assert [invocation.tool_name for invocation in broker.calls] == ["nmap.tcp"]
    records = call_records(result.output)
    assert [record["status"] for record in records] == ["complete", "failed"]
    assert "not available" in records[1]["provider_result"]["detail"]
    assert result.tool_calls == 1


def test_specialist_keeps_completed_calls_when_a_batch_pauses_for_approval(tmp_path):
    provider = ScriptedRoutingProvider(
        [
            [
                _call("call-1", "browser.fetch"),
                _call("call-2", "nmap.tcp"),
                _call("call-3", "browser.fetch"),
            ]
        ]
    )
    broker = RecordingBroker(approvals={"nmap.tcp"})
    specialist = _specialist(tmp_path, provider, broker)

    with pytest.raises(SpecialistApprovalRequired) as paused:
        asyncio.run(specialist.run(_context()))

    partial = paused.value.partial_result
    assert partial is not None
    assert [record["model_call_id"] for record in call_records(partial.output)] == [
        "call-1"
    ]
    assert partial.tool_calls == 1
    # The call queued behind the checkpoint never reached the broker.
    assert [invocation.tool_name for invocation in broker.calls] == [
        "browser.fetch",
        "nmap.tcp",
    ]


def test_mission_persists_a_partial_batch_when_a_task_pauses_for_approval(tmp_path):
    task = PlannedTask(
        id="scan",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Scan approved service",
        instructions="Perform only the approved operation",
        risk_class=RiskClass.ACTIVE_SCAN,
    )

    class BatchPausingSpecialist:
        role = SpecialistRole.NETWORK_SERVICE
        allowed_tools = frozenset({"nmap.tcp", "browser.fetch"})

        def __init__(self):
            self.contexts = []

        async def run(self, context):
            self.contexts.append(context)
            if context.approval_response is None:
                raise SpecialistApprovalRequired(
                    Approval(
                        id="approval-batch",
                        engagement_id=context.engagement_id,
                        run_id=context.run_id,
                        task_id=context.task.id,
                        risk_class=RiskClass.ACTIVE_SCAN,
                        exact_request={
                            "tool_name": "nmap.tcp",
                            "arguments": {"ports": [443]},
                        },
                        target="10.0.0.8",
                        policy_rationale="active scanning requires operator approval",
                        requested_by="network-specialist",
                    ),
                    partial_result=SpecialistResult(
                        summary="browser.fetch completed",
                        outcome=SpecialistOutcome.CONTINUE,
                        output={
                            "status": "complete",
                            "calls": [
                                {
                                    "model_call_id": "call-1",
                                    "tool": "browser.fetch",
                                    "arguments": {},
                                    "status": "complete",
                                    "provider_result": {"ok": True},
                                    "trusted_result": True,
                                }
                            ],
                        },
                        tool_calls=1,
                    ),
                )
            return SpecialistResult(summary="approved scan analyzed")

    class Supervisor:
        async def plan(self, objective, context, budget):
            del objective, context, budget
            return MissionPlan(
                summary="Approval mission",
                rationale="Operator gate",
                tasks=[task],
            )

        async def synthesize(self, objective, plan, results):
            del plan, results
            return f"complete: {objective}"

    store = NebulaStore(tmp_path / "nebula.db")
    specialist = BatchPausingSpecialist()
    runtime = MissionRuntime(
        store=store,
        checkpointer=InMemorySaver(),
        supervisor=Supervisor(),
        specialists={SpecialistRole.NETWORK_SERVICE: specialist},
    )

    waiting = asyncio.run(
        runtime.start(
            engagement_id="engagement-approval",
            objective="Run one bounded scan",
            budget=RunBudget(max_retries=0),
        )
    )

    assert waiting["task_status"] == {task.id: TaskStatus.WAITING_APPROVAL.value}
    history = waiting["task_history"][task.id]
    assert [record["tool"] for record in history[0]["output"]["calls"]] == [
        "browser.fetch"
    ]
    assert waiting["tool_calls"] == 1

    completed = asyncio.run(
        runtime.resume(waiting["run_id"], {"status": "approved", "operator": "alice"})
    )

    assert completed["task_status"] == {task.id: TaskStatus.COMPLETE.value}
    # The resumed turn sees what the paused batch already brokered.
    assert [turn.summary for turn in specialist.contexts[-1].prior_turns] == [
        "browser.fetch completed"
    ]


def test_model_safe_result_sanitizes_every_call_in_a_batch():
    payload = _model_safe_specialist_result(
        SpecialistResult(
            summary="two calls",
            outcome=SpecialistOutcome.CONTINUE,
            output={
                "status": "complete",
                "calls": [
                    {
                        "model_call_id": "call-1",
                        "tool": "nmap.tcp",
                        "status": "complete",
                        "provider_result": {"ports": [443]},
                        "trusted_result": False,
                        "stdout": "raw bytes",
                    },
                    {
                        "model_call_id": "call-2",
                        "tool": "browser.fetch",
                        "status": "complete",
                        "provider_result": {"hosts": ["10.0.0.8"]},
                        "trusted_result": False,
                        "stderr": "raw bytes",
                    },
                ],
            },
        )
    )

    records = payload["output"]["calls"]
    assert "stdout" not in records[0]
    assert "stderr" not in records[1]
    # Every record in the batch goes through the same untrusted-result envelope.
    for record in records:
        assert record["provider_result"]["schema"] == "nebula.tool-result/v2"
        assert record["provider_result"]["tool_call_id"] == record["model_call_id"]
