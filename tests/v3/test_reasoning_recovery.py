"""Asking a reasoning model for the answer it did not write.

A model that spends a turn's budget thinking returns reasoning and no prose.
Nebula's recovery used to answer that by doubling the output budget, which buys
more thinking. These tests pin the two things that replaced it: a request that
asks for the answer alone, and a retry budget a running goal owns.
"""

import asyncio

import pytest

import nebula.v3.chat as chat_module
from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatService,
    ProviderResponseError,
    _final_answer_backoff_seconds,
)
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    Engagement,
)
from nebula.v3.providers import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    build_provider,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider, _profile


def _openrouter(parameters: dict[str, list[str]] | None = None):
    return build_provider(
        ProviderConfig(
            id="openrouter",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="deepseek/deepseek-v4.1-flash",
            model_allowlist=["deepseek/deepseek-v4.1-flash"],
            model_parameters=parameters or {},
        )
    )


def _request(**extra) -> ModelRequest:
    return ModelRequest(
        messages=[{"role": "user", "content": "Answer."}],
        model="deepseek/deepseek-v4.1-flash",
        **extra,
    )


def test_openrouter_carries_a_reasoning_level_a_route_advertises():
    provider = _openrouter({"deepseek/deepseek-v4.1-flash": ["reasoning", "tools"]})
    payload = provider._payload(
        _request(reasoning_effort="none"), "deepseek/deepseek-v4.1-flash"
    )
    # `exclude: False` still asks for the thoughts the transcript shows.
    assert payload["reasoning"] == {"exclude": False, "effort": "none"}

    budgeted = provider._payload(
        _request(reasoning_max_tokens=256), "deepseek/deepseek-v4.1-flash"
    )
    assert budgeted["reasoning"] == {"exclude": False, "max_tokens": 256}

    plain = provider._payload(_request(), "deepseek/deepseek-v4.1-flash")
    assert plain["reasoning"] == {"exclude": False}


def test_a_route_that_does_not_advertise_the_control_is_not_sent_one():
    # Sending a parameter a route does not take costs the request, and with
    # require_parameters it can leave no eligible endpoint at all.
    provider = _openrouter({"deepseek/deepseek-v4.1-flash": ["tools"]})
    payload = provider._payload(
        _request(reasoning_effort="none"), "deepseek/deepseek-v4.1-flash"
    )
    assert payload["reasoning"] == {"exclude": False}

    # An unknown model advertises nothing at all; the level is still sent,
    # because an empty catalog is absence of evidence, not a refusal.
    unknown = _openrouter()
    assert unknown._payload(
        _request(reasoning_effort="low"), "deepseek/deepseek-v4.1-flash"
    )["reasoning"] == {"exclude": False, "effort": "low"}


def test_openai_reasoning_families_take_the_scalar_shorthand():
    provider = build_provider(
        ProviderConfig(
            id="openai",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="https://api.openai.com/v1",
            default_model="gpt-5",
            model_allowlist=["gpt-5", "chatty-1"],
        )
    )
    payload = provider._payload(
        ModelRequest(
            messages=[{"role": "user", "content": "x"}], reasoning_effort="low"
        ),
        "gpt-5",
    )
    assert payload["reasoning_effort"] == "low"
    # A model with no documented reasoning control is left alone.
    other = provider._payload(
        ModelRequest(
            messages=[{"role": "user", "content": "x"}], reasoning_effort="low"
        ),
        "chatty-1",
    )
    assert "reasoning_effort" not in other


def _reasoning_only(provider_id: str, model: str, request_id: str) -> ModelResponse:
    return ModelResponse(
        provider_id=provider_id,
        model=model,
        reasoning="Still planning the answer.",
        usage=ModelUsage(input_tokens=4, output_tokens=512, total_tokens=516),
        finish_reason="stop",
        provider_request_id=request_id,
    )


class ThinkingProvider(FakeProvider):
    """Returns reasoning and no prose until ``answer_on`` normal requests."""

    def __init__(self, provider_id: str, *, answer_on: int) -> None:
        super().__init__(provider_id, local=True)
        self.answer_on = answer_on

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation"):
            return await super().complete(request)
        self.requests.append(request)
        normal = [item for item in self.requests if not item.metadata.get("operation")]
        if len(normal) < self.answer_on:
            return _reasoning_only(
                self.config.id, request.model or "model-a", f"thinking-{len(normal)}"
            )
        return ModelResponse(
            provider_id=self.config.id,
            model=request.model or "model-a",
            text="Recovered visible answer.",
            usage=ModelUsage(input_tokens=4, output_tokens=3, total_tokens=7),
            finish_reason="stop",
            provider_request_id=f"answer-{len(normal)}",
        )


def _fixture(tmp_path, *, answer_on: int, monkeypatch, with_goal: bool, **goal_fields):
    store = NebulaStore(tmp_path / "reasoning.db")
    store.create(Engagement(id="project", name="Project"))
    profile = store.create(_profile(local=True))
    provider = ThinkingProvider(profile.id, answer_on=answer_on)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    goal = None
    if with_goal:
        store.create(
            ChatSession(
                id="session",
                engagement_id="project",
                title="Goal chat",
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        goal = store.create(
            ChatGoal(
                id="goal-1",
                engagement_id="project",
                session_id="session",
                objective="Answer the operator",
                completion_criteria=["An answer is written"],
                status=ChatGoalStatus.RUNNING,
                **goal_fields,
            )
        )
    return store, profile, provider, goal


def _send(service, profile, **extra):
    return service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id="project",
            messages=[{"role": "user", "content": "Give me a visible answer."}],
            include_knowledge=False,
            stream=True,
            **extra,
        )
    )


def test_recovery_asks_for_the_answer_alone_instead_of_more_room_to_think(
    tmp_path, monkeypatch
):
    store, profile, provider, _ = _fixture(
        tmp_path, answer_on=2, monkeypatch=monkeypatch, with_goal=False
    )
    service = ChatService(store)
    response = asyncio.run(service.complete(_send(service, profile)))

    normal = [item for item in provider.requests if not item.metadata.get("operation")]
    assert response.message.content == "Recovered visible answer."
    # The first ask leaves the model's own default alone; the recovery asks
    # for prose, which is the thing that was missing.
    assert [item.reasoning_effort for item in normal] == [None, "none"]
    assert normal[-1].metadata["final_answer_recovery"] == "reasoning_only"


def test_outside_goal_mode_one_recovery_is_the_automatic_budget(tmp_path, monkeypatch):
    store, profile, provider, _ = _fixture(
        tmp_path, answer_on=99, monkeypatch=monkeypatch, with_goal=False
    )
    service = ChatService(store)
    prepared = _send(service, profile)
    with pytest.raises(ProviderResponseError):
        asyncio.run(service.complete(prepared))

    normal = [item for item in provider.requests if not item.metadata.get("operation")]
    assert len(normal) == 2
    # The turn is left resumable: the operator decides whether to ask again.
    turn = store.get(ChatTurn, prepared.turn.id)
    assert turn.request_snapshot["final_answer_recovery"]["attempts"] == 2
    assert service.recoverable_final_answer_turn(turn.session_id) is not None


def test_a_running_goal_keeps_asking_until_it_gets_an_answer(tmp_path, monkeypatch):
    store, profile, provider, goal = _fixture(
        tmp_path, answer_on=4, monkeypatch=monkeypatch, with_goal=True
    )
    monkeypatch.setattr(chat_module.asyncio, "sleep", _no_delay)
    service = ChatService(store)
    response = asyncio.run(
        service.complete(_send(service, profile, session_id="session", goal_id=goal.id))
    )

    normal = [item for item in provider.requests if not item.metadata.get("operation")]
    assert response.message.content == "Recovered visible answer."
    # Four attempts: the operator asked a goal to keep trying.
    assert len(normal) == 4
    assert [item.reasoning_effort for item in normal] == [None, "none", "none", "none"]
    # Every attempt is charged, so the goal's own budget is what bounds it.
    assert store.get(ChatGoal, goal.id).usage.total_tokens == 516 * 3 + 7


async def _no_delay(_seconds: float) -> None:
    return None


def test_a_goal_that_can_never_finish_is_blocked_rather_than_looping(
    tmp_path, monkeypatch
):
    store, profile, provider, goal = _fixture(
        tmp_path, answer_on=99, monkeypatch=monkeypatch, with_goal=True
    )
    monkeypatch.setattr(chat_module.asyncio, "sleep", _no_delay)
    service = ChatService(store)
    with pytest.raises(ProviderResponseError):
        asyncio.run(
            service.complete(
                _send(service, profile, session_id="session", goal_id=goal.id)
            )
        )

    normal = [item for item in provider.requests if not item.metadata.get("operation")]
    assert len(normal) == chat_module._GOAL_FINAL_ANSWER_STALL_LIMIT + 1
    blocked = store.get(ChatGoal, goal.id)
    assert blocked.status == ChatGoalStatus.BLOCKED
    assert "no answer" in (blocked.blocked_reason or "")
    # Blocked, not cancelled: the operator can resume and it tries again.
    assert blocked.completed_at is None


def _retry_decision(store, goal_id: str | None, attempts: int, tmp_path):
    """Ask the retry budget directly; the loop around it is covered above."""

    service = ChatService(store)
    turn = ChatTurn(
        id="turn-1",
        engagement_id="project",
        session_id="session",
        provider_profile_id="provider-a",
        model="model-a",
        goal_id=goal_id,
    )
    prepared = type("Prepared", (), {"turn": turn})()
    return service._may_retry_final_answer(prepared, attempts)


def test_the_retry_budget_honours_what_the_operator_did_to_the_goal(
    tmp_path, monkeypatch
):
    store, _profile_row, _provider, goal = _fixture(
        tmp_path, answer_on=99, monkeypatch=monkeypatch, with_goal=True
    )

    # Inside the automatic budget, every conversation gets one more ask.
    assert _retry_decision(store, None, 1, tmp_path)[0] is True
    # Past it, a conversation without a goal stops and leaves the turn resumable.
    assert _retry_decision(store, None, 2, tmp_path)[0] is False
    # A running goal keeps going, after a wait.
    allowed, delay = _retry_decision(store, goal.id, 2, tmp_path)
    assert allowed is True and delay > 0

    for status in (
        ChatGoalStatus.PAUSED,
        ChatGoalStatus.BLOCKED,
        ChatGoalStatus.COMPLETED,
        ChatGoalStatus.CANCELLED,
    ):
        current = store.get(ChatGoal, goal.id)
        store.update(
            ChatGoal,
            goal.id,
            {
                "status": status,
                "active_since": None,
                "blocked_reason": "held",
                "completion_summary": "done",
                "completion_evidence": [{"kind": "operator_confirmation"}],
            },
            expected_revision=current.revision,
        )
        # A goal the operator stopped is not retried on their behalf.
        assert _retry_decision(store, goal.id, 2, tmp_path)[0] is False

    # A goal whose own time budget ran out stops too, without being touched.
    current = store.get(ChatGoal, goal.id)
    store.update(
        ChatGoal,
        goal.id,
        {
            "status": ChatGoalStatus.RUNNING,
            "time_budget_seconds": 60,
            "elapsed_seconds": 90,
            "active_since": None,
        },
        expected_revision=current.revision,
    )
    assert _retry_decision(store, goal.id, 2, tmp_path)[0] is False
    assert store.get(ChatGoal, goal.id).status == ChatGoalStatus.RUNNING


def test_a_goal_that_is_gone_stops_retrying(tmp_path, monkeypatch):
    store, _profile_row, _provider, _goal = _fixture(
        tmp_path, answer_on=99, monkeypatch=monkeypatch, with_goal=True
    )
    assert _retry_decision(store, "goal-that-never-existed", 2, tmp_path)[0] is False


def test_backoff_grows_and_then_holds():
    # A provider that just failed is not asked again immediately, and a long
    # run of failures does not stretch the wait without limit.
    assert _final_answer_backoff_seconds(1) == 1
    assert _final_answer_backoff_seconds(3) == 4
    assert _final_answer_backoff_seconds(30) == 30


def test_the_operators_reasoning_level_is_sent_and_remembered(tmp_path, monkeypatch):
    store, profile, provider, _ = _fixture(
        tmp_path, answer_on=1, monkeypatch=monkeypatch, with_goal=False
    )
    service = ChatService(store)
    response = asyncio.run(
        service.complete(_send(service, profile, reasoning_effort="low"))
    )

    normal = [item for item in provider.requests if not item.metadata.get("operation")]
    assert normal[0].reasoning_effort == "low"
    session = store.get(ChatSession, response.session_id)
    assert session.metadata["reasoning_effort"] == "low"

    # Returning to the model's default is a choice, so it round-trips as one.
    asyncio.run(
        service.complete(_send(service, profile, session_id=response.session_id))
    )
    assert (
        store.get(ChatSession, response.session_id).metadata["reasoning_effort"] is None
    )


def test_an_unknown_reasoning_level_is_refused_at_the_boundary():
    with pytest.raises(ValueError):
        ChatCompletionRequest(
            provider_id="provider",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="ludicrous",
        )
