"""A mission specialist keeps routing when a model deviates in a routing step.

Prose beside a call, a reply with no call, a tool that is not offered, a
response cut off by the output limit and a reused call id are all answered
with an observation the model sees on its next turn, the way Codex, Cline,
pi-mono and the AI SDK answer them, instead of failing the specialist turn.
"""

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from nebula.v3 import agent_tooling
from nebula.v3.agent_tooling import BrokeredToolSpecialist
from nebula.v3.domain import RiskClass, RunBudget, ScopePolicy, TaskStatus
from nebula.v3.orchestration import (
    MissionPlan,
    MissionRuntime,
    PlannedTask,
    SpecialistContext,
    SpecialistOutcome,
    SpecialistResult,
    SpecialistRole,
    call_records,
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
    IdempotencyBehavior,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)

FINISH = "nebula.finish_task"
DONE = {
    "status": "complete",
    "summary": "The service is mapped",
    "rationale": "Every observation the objective needs is in hand",
}


class ScriptedProvider(ModelProvider):
    """Answers each routing step with a scripted text, call batch and stop."""

    def __init__(self, steps):
        super().__init__(
            ProviderConfig(
                id="provider-tolerance",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(),
            )
        )
        self.steps = list(steps)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        step = self.steps.pop(0)
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text=step.get("text", ""),
            tool_calls=step.get("calls", []),
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason=step.get("finish_reason", "tool_calls"),
        )

    async def health(self):
        return ProviderHealth(provider_id=self.config.id, healthy=True)


class RecordingBroker:
    def __init__(self):
        self.calls = []

    async def execute(self, invocation: ToolInvocation, scope: ScopePolicy):
        del scope
        self.calls.append(invocation)
        return ToolExecutionResult(
            output={"tool": invocation.tool_name, "ok": True},
            evidence_ids=[f"evidence-{len(self.calls)}"],
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


def _specialist(tmp_path, provider, broker):
    return BrokeredToolSpecialist(
        provider,
        role=SpecialistRole.NETWORK_SERVICE,
        broker=broker,
        scope=ScopePolicy(id="scope-1", engagement_id="engagement-1"),
        workspace=tmp_path,
        specs={
            "nmap.tcp": _spec("nmap.tcp"),
            "browser.fetch": _spec("browser.fetch"),
            "tool_output.search": _spec("tool_output.search", "artifact_query"),
        },
        model="model-a",
    )


def _context(prior_turns=(), remaining_tool_calls=None):
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
        turn_index=len(prior_turns) + 1,
        remaining_tool_calls=remaining_tool_calls,
        allowed_tools=frozenset({"nmap.tcp", "browser.fetch", "tool_output.search"}),
        prior_turns=list(prior_turns),
    )


def _call(call_id: str, name: str, arguments=None) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


def _turns(specialist, count):
    """Run ``count`` specialist turns, each seeing the turns before it."""

    turns = []
    for _ in range(count):
        turns.append(asyncio.run(specialist.run(_context(prior_turns=turns))))
    return turns


@pytest.fixture
def warnings(monkeypatch):
    recorded = []

    def record(level, feature, event_code, message, **fields):
        recorded.append({"level": level, "feature": feature, "event": event_code})
        return None

    monkeypatch.setattr(agent_tooling, "record_diagnostic", record, raising=False)
    return recorded


def test_prose_beside_routing_calls_runs_the_calls_and_keeps_it_as_commentary(
    tmp_path, warnings
):
    provider = ScriptedProvider(
        [
            {
                "text": "I'll scan the web port first.",
                "calls": [_call("call-1", "nmap.tcp", {"ports": [80]})],
            }
        ]
    )
    broker = RecordingBroker()

    result = asyncio.run(_specialist(tmp_path, provider, broker).run(_context()))

    assert [invocation.tool_name for invocation in broker.calls] == ["nmap.tcp"]
    assert result.outcome is SpecialistOutcome.CONTINUE
    assert result.output["commentary"] == "I'll scan the web port first."
    # The prose is commentary, never the tool result or the turn's finding.
    assert result.output["status"] == "complete"
    assert "scan the web port" not in str(result.output["provider_result"])
    assert "scan the web port" not in result.summary
    assert {
        "level": "warning",
        "feature": "missions",
        "event": "missions.routing.prose_with_tool_calls",
    } in warnings


@pytest.mark.parametrize("text", ["The service looks healthy.", ""])
def test_reply_without_a_routing_action_asks_the_model_again(tmp_path, warnings, text):
    provider = ScriptedProvider(
        [{"text": text, "calls": []}, {"calls": [_call("call-2", FINISH, DONE)]}]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    first, second = _turns(specialist, 2)

    assert first.outcome is SpecialistOutcome.CONTINUE
    assert first.tool_calls == 0
    assert (first.input_tokens, first.output_tokens) == (10, 5)
    assert first.output.get("commentary", "") == text
    # The next routing prompt tells the model what went wrong.
    follow_up = provider.requests[1].messages[0].content
    assert "no routing action" in follow_up
    assert FINISH in follow_up
    assert second.outcome is SpecialistOutcome.COMPLETE
    assert broker.calls == []
    assert "missions.routing.no_action" in {item["event"] for item in warnings}


def test_finish_with_extra_arguments_is_accepted_as_a_finish(tmp_path, warnings):
    provider = ScriptedProvider(
        [{"calls": [_call("call-1", FINISH, {**DONE, "findings": ["none"]})]}]
    )
    broker = RecordingBroker()

    result = asyncio.run(_specialist(tmp_path, provider, broker).run(_context()))

    assert result.outcome is SpecialistOutcome.COMPLETE
    assert result.summary == "The service is mapped"
    assert broker.calls == []
    assert "missions.routing.finish_extra_arguments" in {
        item["event"] for item in warnings
    }


def test_unavailable_tool_is_answered_with_a_failed_observation(tmp_path, warnings):
    provider = ScriptedProvider(
        [
            {
                "calls": [
                    _call("call-1", "nmap.tcp", {"ports": [80]}),
                    _call("call-2", "shell.exec", {"command": "id"}),
                    _call("call-3", "browser.fetch"),
                ]
            },
            {"calls": [_call("call-4", FINISH, DONE)]},
        ]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    first, second = _turns(specialist, 2)

    # The valid calls around the unknown one still ran; the unknown one never
    # reached the broker and spent no tool-call slot.
    assert [invocation.tool_name for invocation in broker.calls] == [
        "nmap.tcp",
        "browser.fetch",
    ]
    assert first.tool_calls == 2
    records = call_records(first.output)
    assert [record["model_call_id"] for record in records] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert records[1]["status"] == "failed"
    detail = records[1]["provider_result"]["detail"]
    assert "'shell.exec' is not available" in detail
    assert "nmap.tcp" in detail and FINISH in detail
    # The observation reaches the model on its next turn as an error result,
    # and a failed routing observation does not block a later completion.
    replayed = {item.call_id: item for item in provider.requests[1].tool_results}
    assert replayed["call-2"].is_error is True
    assert "not available" in replayed["call-2"].output["detail"]
    assert replayed["call-1"].is_error is False
    assert second.outcome is SpecialistOutcome.COMPLETE
    assert "missions.routing.unavailable_tool" in {item["event"] for item in warnings}


def test_consecutive_routing_deviations_block_the_task_after_three(tmp_path):
    provider = ScriptedProvider(
        [{"calls": [_call(f"call-{index}", "shell.exec")]} for index in range(3)]
    )
    broker = RecordingBroker()
    specialist = _specialist(tmp_path, provider, broker)

    turns = _turns(specialist, 3)

    assert [turn.outcome for turn in turns] == [
        SpecialistOutcome.CONTINUE,
        SpecialistOutcome.CONTINUE,
        SpecialistOutcome.BLOCKED,
    ]
    assert "3 routing responses in a row" in turns[-1].summary
    assert broker.calls == []


def test_a_brokered_call_resets_the_deviation_count(tmp_path):
    provider = ScriptedProvider(
        [
            {"calls": [_call("call-1", "shell.exec")]},
            {"calls": [_call("call-2", "shell.exec")]},
            {"calls": [_call("call-3", "nmap.tcp")]},
            {"calls": [_call("call-4", "shell.exec")]},
            {"calls": [_call("call-5", "shell.exec")]},
        ]
    )
    broker = RecordingBroker()

    turns = _turns(_specialist(tmp_path, provider, broker), 5)

    assert all(turn.outcome is SpecialistOutcome.CONTINUE for turn in turns)
    assert [invocation.tool_name for invocation in broker.calls] == ["nmap.tcp"]


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens", "MAX_TOKENS"])
def test_calls_cut_off_by_the_output_limit_are_not_executed(
    tmp_path, warnings, finish_reason
):
    provider = ScriptedProvider(
        [
            {
                "calls": [_call("call-1", "nmap.tcp", {"ports": [8]})],
                "finish_reason": finish_reason,
            },
            # The model re-issues the same call; Core now runs it.
            {"calls": [_call("call-2", "nmap.tcp", {"ports": [8]})]},
        ]
    )
    broker = RecordingBroker()

    first, second = _turns(_specialist(tmp_path, provider, broker), 2)

    assert first.output["status"] == "failed"
    assert first.tool_calls == 0
    assert "complete arguments" in first.output["provider_result"]["detail"]
    assert provider.requests[1].tool_results[0].is_error is True
    assert [invocation.arguments for invocation in broker.calls] == [{"ports": [8]}]
    assert second.output["status"] == "complete"
    assert "missions.routing.output_limit_calls" in {item["event"] for item in warnings}


def test_reused_call_id_for_a_new_call_runs_under_a_core_id(tmp_path, warnings):
    provider = ScriptedProvider(
        [
            {"calls": [_call("call_0", "nmap.tcp", {"ports": [80]})]},
            {"calls": [_call("call_0", "nmap.tcp", {"ports": [443]})]},
            {"calls": [_call("call_1", FINISH, DONE)]},
        ]
    )
    broker = RecordingBroker()

    first, second, third = _turns(_specialist(tmp_path, provider, broker), 3)

    assert [invocation.arguments for invocation in broker.calls] == [
        {"ports": [80]},
        {"ports": [443]},
    ]
    assert first.output["model_call_id"] == "call_0"
    reissued = second.output["model_call_id"]
    # Nine alphanumerics: the strictest provider call-id format (Mistral).
    assert reissued != "call_0"
    assert len(reissued) == 9 and reissued.isalnum()
    assert len({invocation.idempotency_key for invocation in broker.calls}) == 2
    # History pairs every call with its own result.
    replayed = [item.call_id for item in provider.requests[2].tool_results]
    assert replayed == ["call_0", reissued]
    assert third.outcome is SpecialistOutcome.COMPLETE
    assert "missions.routing.repeated_call_id" in {item["event"] for item in warnings}


def test_reused_synthetic_gemini_id_stays_synthetic(tmp_path):
    synthetic = "nebula-gemini-call:call:0"
    provider = ScriptedProvider(
        [
            {"calls": [_call(synthetic, "nmap.tcp", {"ports": [80]})]},
            {"calls": [_call(synthetic, "nmap.tcp", {"ports": [443]})]},
        ]
    )
    broker = RecordingBroker()

    first, second = _turns(_specialist(tmp_path, provider, broker), 2)

    # Core never echoes an id it made up to Gemini, so the replacement keeps
    # the synthetic prefix that marks it as Core's own.
    assert len(broker.calls) == 2
    reissued = second.output["model_call_id"]
    assert reissued != synthetic
    assert reissued.startswith("nebula-gemini-call:")


def test_reused_call_id_for_the_same_call_never_duplicates_its_effect(tmp_path):
    provider = ScriptedProvider(
        [
            {"calls": [_call("call-1", "nmap.tcp", {"ports": [80]})]},
            {"calls": [_call("call-1", "nmap.tcp", {"ports": [80]})]},
            # The id's second use ran under a Core id; repeating that call
            # under the provider's id again is still the same call.
            {"calls": [_call("call-1", "nmap.tcp", {"ports": [443]})]},
            {"calls": [_call("call-1", "nmap.tcp", {"ports": [443]})]},
        ]
    )
    broker = RecordingBroker()

    first, second, third, fourth = _turns(_specialist(tmp_path, provider, broker), 4)

    assert [invocation.arguments for invocation in broker.calls] == [
        {"ports": [80]},
        {"ports": [443]},
    ]
    for repeat in (second, fourth):
        assert repeat.outcome is SpecialistOutcome.CONTINUE
        assert repeat.output["status"] == "failed"
        assert "already ran" in repeat.output["provider_result"]["detail"]
        assert repeat.output["model_call_id"] != "call-1"
    ids = [turn.output["model_call_id"] for turn in (first, second, third, fourth)]
    assert len(set(ids)) == 4


def test_a_repeated_id_inside_one_batch_runs_the_same_call_once(tmp_path):
    provider = ScriptedProvider(
        [
            {
                "calls": [
                    _call("call-1", "nmap.tcp", {"ports": [80]}),
                    _call("call-1", "nmap.tcp", {"ports": [80]}),
                    _call("call-1", "browser.fetch"),
                ]
            }
        ]
    )
    broker = RecordingBroker()

    result = asyncio.run(_specialist(tmp_path, provider, broker).run(_context()))

    assert [invocation.tool_name for invocation in broker.calls] == [
        "nmap.tcp",
        "browser.fetch",
    ]
    records = call_records(result.output)
    assert [record["status"] for record in records] == [
        "complete",
        "failed",
        "complete",
    ]
    assert len({record["model_call_id"] for record in records}) == 3


def test_a_routing_deviation_does_not_push_an_older_tool_failure_out_of_view(
    tmp_path,
):
    def turn(call_id, status, routing_error=None):
        record = {
            "model_call_id": call_id,
            "tool": "nmap.tcp",
            "arguments": {"ports": [int(call_id.split("-")[1])]},
            "status": status,
            "provider_result": {"status": status, "detail": call_id},
            "trusted_result": False,
        }
        if routing_error:
            record["routing_error"] = routing_error
        return SpecialistResult(
            summary=call_id, outcome=SpecialistOutcome.CONTINUE, output=record
        )

    prior = [
        turn("call-1", "failed"),
        *(turn(f"call-{index}", "complete") for index in range(2, 10)),
        turn("call-10", "failed", routing_error="unavailable_tool"),
    ]
    provider = ScriptedProvider([{"calls": [_call("call-11", FINISH, DONE)]}])

    asyncio.run(
        _specialist(tmp_path, provider, RecordingBroker()).run(
            _context(prior_turns=prior)
        )
    )

    # The last eight turns are replayed, plus the latest real tool failure.
    replayed = [item.call_id for item in provider.requests[0].tool_results]
    assert replayed[0] == "call-1"
    assert replayed[-1] == "call-10"


def test_mission_feeds_a_routing_deviation_back_instead_of_failing(tmp_path):
    task = PlannedTask(
        id="scan",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Inspect the service",
        instructions="Gather the observations the objective needs",
        risk_class=RiskClass.LOCAL_READ,
    )

    class Supervisor:
        async def plan(self, objective, context, budget):
            del objective, context, budget
            return MissionPlan(summary="Scan", rationale="Scan", tasks=[task])

        async def synthesize(self, objective, plan, results):
            del plan, results
            return f"complete: {objective}"

    provider = ScriptedProvider(
        [
            {
                "text": "Let me look at the host.",
                "calls": [_call("call-1", "shell.exec", {"command": "id"})],
            },
            {"calls": [_call("call-2", FINISH, DONE)]},
        ]
    )
    broker = RecordingBroker()
    runtime = MissionRuntime(
        store=NebulaStore(tmp_path / "nebula.db"),
        checkpointer=InMemorySaver(),
        supervisor=Supervisor(),
        specialists={
            SpecialistRole.NETWORK_SERVICE: _specialist(tmp_path, provider, broker)
        },
    )

    state = asyncio.run(
        runtime.start(
            engagement_id="engagement-1",
            objective="Map the exposed service",
            # No retries: any specialist error would fail the task outright.
            budget=RunBudget(max_retries=0),
        )
    )

    assert state["task_status"] == {task.id: TaskStatus.COMPLETE.value}
    replayed = provider.requests[1].tool_results
    assert [(item.name, item.is_error) for item in replayed] == [("shell.exec", True)]
    assert broker.calls == []
