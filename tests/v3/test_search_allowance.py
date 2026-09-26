"""A turn's ``conversation.search`` and ``knowledge.search`` calls have allowances.

The tools ask the model to stop once searching stops finding anything, and
models still searched twenty to a hundred times in one turn to re-confirm facts
they already had, each search another provider round trip over a growing
history. Past the allowance a search does not run: Core answers it with an
ordinary result telling the model to answer from what it has. Nothing fails,
nothing waits for approval, and the turn goes on. The count comes from the
turn's own ledger, so a turn resumed after a restart keeps it.
"""

import asyncio
import json

from nebula.v3.chat import ChatService
from nebula.v3.conversation_search import (
    CONVERSATION_SEARCH_TOOL_NAME,
    conversation_search_spec,
)
from nebula.v3.knowledge_search import KNOWLEDGE_SEARCH_TOOL_NAME, knowledge_search_spec
from nebula.v3.search_allowance import (
    TURN_EMPTY_SEARCH_LIMIT,
    TURN_SEARCH_BUDGET,
    turn_search_allowance_spent,
)
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.providers import ToolCall
from nebula.v3.tools import ToolExecutionResult
from tests.v3.test_chat_tool_loop import _prepared, _response

ANSWER = "The ticket is OMEGA-7465; the other details are in my notes above."


def _search_entry(
    step: int, found: int = 1, name: str = CONVERSATION_SEARCH_TOOL_NAME, **fields
) -> dict:
    return {
        "step": step,
        "tool_call_id": f"search-{step}",
        "model_call_id": f"call-{step}",
        "name": name,
        "arguments": {"query": f"fact {step}"},
        "budget_class": "artifact_query",
        "status": "complete",
        "provider_result": json.dumps(
            {"result_count": found, "results": [{"content": "x"}] * found}
        ),
        **fields,
    }


def test_the_allowance_is_counted_from_the_searches_that_ran():
    history = [_search_entry(step) for step in range(TURN_SEARCH_BUDGET - 1)]
    assert (
        turn_search_allowance_spent(history, CONVERSATION_SEARCH_TOOL_NAME, "q") is None
    )

    # Calls Core answered itself never ran and do not count; other tools
    # between searches do not either.
    history += [
        _search_entry(90, budget_class="refused"),
        {"step": 91, "name": "safe_read", "budget_class": "execution"},
    ]
    assert (
        turn_search_allowance_spent(history, CONVERSATION_SEARCH_TOOL_NAME, "q") is None
    )

    history.append(_search_entry(TURN_SEARCH_BUDGET))
    spent = turn_search_allowance_spent(
        history, CONVERSATION_SEARCH_TOOL_NAME, "next fact"
    )
    assert spent is not None
    assert spent["search_budget_spent"] is True
    assert spent["searches_this_turn"] == TURN_SEARCH_BUDGET
    assert spent["query"] == "next fact"
    # Nothing was searched, so no empty result list says nothing was found.
    assert "results" not in spent
    assert "did not run" in spent["detail"]
    assert "Answer the operator's request as they asked" in spent["detail"]
    # A result cleared from the model's context is pointed to, not re-searched.
    assert "tool_output.search" in spent["detail"]

    # The searches one response asks for run together: the allowance counts
    # only the searches of earlier responses.
    grouped = [
        _search_entry(step, response_group="first")
        for step in range(TURN_SEARCH_BUDGET)
    ]
    assert (
        turn_search_allowance_spent(
            grouped, CONVERSATION_SEARCH_TOOL_NAME, "q", response_group="first"
        )
        is None
    )
    assert turn_search_allowance_spent(
        grouped, CONVERSATION_SEARCH_TOOL_NAME, "q", response_group="second"
    )


def test_searches_that_keep_finding_nothing_end_the_searching():
    history = [_search_entry(0), _search_entry(1)]
    history += [
        _search_entry(step, found=0)
        for step in range(2, 2 + TURN_EMPTY_SEARCH_LIMIT - 1)
    ]
    assert (
        turn_search_allowance_spent(history, CONVERSATION_SEARCH_TOOL_NAME, "q") is None
    )
    history.append(_search_entry(9, found=0))
    spent = turn_search_allowance_spent(history, CONVERSATION_SEARCH_TOOL_NAME, "q")
    assert spent is not None
    assert "most likely not there" in spent["detail"]
    # A search that finds something resets the run of empty ones.
    history.append(_search_entry(10))
    assert (
        turn_search_allowance_spent(history, CONVERSATION_SEARCH_TOOL_NAME, "q") is None
    )


class SearchBroker:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls.append(invocation)
        return ToolExecutionResult(
            output={
                "tool": CONVERSATION_SEARCH_TOOL_NAME,
                "query": invocation.arguments["query"],
                "result_count": 1,
                "results": [{"content": f"Found {invocation.arguments['query']}"}],
                "detail": "1 passage(s), best match first.",
            }
        )


def _search(index: int):
    return _response(
        calls=[
            ToolCall(
                id=f"search-call-{index}",
                name=CONVERSATION_SEARCH_TOOL_NAME,
                arguments={"query": f"fact {index}"},
            )
        ]
    )


def _entries(service, turn_id):
    return service._turn_history(service.store.get(ChatTurn, turn_id))


def test_a_search_past_the_allowance_gets_an_ordinary_result_and_the_turn_answers(
    tmp_path,
):
    broker = SearchBroker()
    # The model keeps searching: the allowance runs, three more are answered
    # by Core, and then the turn stops routing and answers from what it has.
    responses = [_search(index) for index in range(TURN_SEARCH_BUDGET + 3)]
    responses.append(_response(text=ANSWER))
    store, service, prepared, provider = _prepared(
        tmp_path,
        responses,
        broker,
        max_tool_calls=50,
        extra_specs=[conversation_search_spec()],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert len(broker.calls) == TURN_SEARCH_BUDGET
    searches = [
        entry
        for entry in _entries(service, prepared.turn.id)
        if entry["name"] == CONVERSATION_SEARCH_TOOL_NAME
    ]
    assert len(searches) == TURN_SEARCH_BUDGET + 3
    answered = searches[TURN_SEARCH_BUDGET:]
    for entry in answered:
        # Not an error and not an approval: an ordinary, completed result that
        # spends no budget.
        assert entry["status"] == "complete"
        assert entry["budget_class"] == "refused"
        result = json.loads(entry["provider_result"])
        assert result["search_budget_spent"] is True
        assert result["searches_this_turn"] == TURN_SEARCH_BUDGET
        assert "did not run" in entry["result_summary"]
    assert all(entry["status"] == "complete" for entry in searches)
    turn = store.get(ChatTurn, prepared.turn.id)
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.artifact_queries == TURN_SEARCH_BUDGET
    # The model read the answers as ordinary results of its calls, not errors.
    final = provider.requests[-1]
    replayed = [
        result
        for result in final.tool_results
        if result.name == CONVERSATION_SEARCH_TOOL_NAME
        and "search_budget_spent" in str(result.output)
    ]
    assert len(replayed) == 3
    assert not any(result.is_error for result in replayed)


def test_a_resumed_turn_keeps_the_searches_it_already_ran(tmp_path):
    broker = SearchBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [_search(99), _response(), _response(text=ANSWER)],
        broker,
        max_tool_calls=50,
        extra_specs=[conversation_search_spec()],
    )
    # The searches a previous Core process ran before it restarted.
    turn = prepared.turn
    for step in range(TURN_SEARCH_BUDGET):
        turn = service._save_tool_step(turn, _search_entry(step))
    prepared.turn = turn

    restarted = ChatService(store, worker_id="worker")
    completion = asyncio.run(restarted.complete(prepared))

    assert completion.message.content == ANSWER
    assert broker.calls == []
    last = _entries(restarted, turn.id)[-1]
    assert last["name"] == CONVERSATION_SEARCH_TOOL_NAME
    assert last["status"] == "complete"
    assert json.loads(last["provider_result"])["search_budget_spent"] is True


def test_the_searches_one_response_asks_for_run_together(tmp_path):
    broker = SearchBroker()
    batch = _response(
        calls=[
            ToolCall(
                id=f"batched-{index}",
                name=CONVERSATION_SEARCH_TOOL_NAME,
                arguments={"query": f"fact {index}"},
            )
            for index in range(TURN_SEARCH_BUDGET + 2)
        ]
    )
    store, service, prepared, provider = _prepared(
        tmp_path,
        [batch, _search(99), _response(), _response(text=ANSWER)],
        broker,
        max_tool_calls=50,
        extra_specs=[conversation_search_spec()],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    # The first response's searches all ran, past the allowance; the next
    # response's search was answered instead.
    assert len(broker.calls) == TURN_SEARCH_BUDGET + 2
    last = _entries(service, prepared.turn.id)[-1]
    assert last["budget_class"] == "refused"
    assert json.loads(last["provider_result"])["searches_this_turn"] == (
        TURN_SEARCH_BUDGET + 2
    )


def test_each_kind_of_search_has_its_own_allowance():
    conversation = [_search_entry(step) for step in range(TURN_SEARCH_BUDGET)]
    assert turn_search_allowance_spent(conversation, CONVERSATION_SEARCH_TOOL_NAME, "q")
    assert (
        turn_search_allowance_spent(conversation, KNOWLEDGE_SEARCH_TOOL_NAME, "q")
        is None
    )
    knowledge = [
        _search_entry(step, name=KNOWLEDGE_SEARCH_TOOL_NAME)
        for step in range(TURN_SEARCH_BUDGET)
    ]
    spent = turn_search_allowance_spent(knowledge, KNOWLEDGE_SEARCH_TOOL_NAME, "q")
    assert spent is not None
    assert spent["tool"] == KNOWLEDGE_SEARCH_TOOL_NAME
    assert "the project's knowledge" in spent["detail"]
    # No excerpts, so nothing is cited from it.
    assert "matches" not in spent
    # Other tools are never answered in their place.
    assert turn_search_allowance_spent(knowledge, "safe_read", "q") is None


def test_a_knowledge_search_past_its_allowance_is_answered_too(tmp_path):
    class KnowledgeBroker:
        def __init__(self) -> None:
            self.calls = []

        async def execute(self, invocation, scope, *, approval=None):
            del scope, approval
            self.calls.append(invocation)
            return ToolExecutionResult(
                output={
                    "tool": KNOWLEDGE_SEARCH_TOOL_NAME,
                    "query": invocation.arguments["query"],
                    "matches": [],
                    "result_count": 1,
                    "detail": "1 excerpt(s).",
                }
            )

    broker = KnowledgeBroker()
    responses = [
        _response(
            calls=[
                ToolCall(
                    id=f"knowledge-{index}",
                    name=KNOWLEDGE_SEARCH_TOOL_NAME,
                    arguments={"query": f"policy {index}"},
                )
            ]
        )
        for index in range(TURN_SEARCH_BUDGET + 1)
    ]
    responses += [_response(), _response(text=ANSWER)]
    store, service, prepared, provider = _prepared(
        tmp_path,
        responses,
        broker,
        max_tool_calls=50,
        extra_specs=[knowledge_search_spec()],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert len(broker.calls) == TURN_SEARCH_BUDGET
    last = [
        entry
        for entry in _entries(service, prepared.turn.id)
        if entry["name"] == KNOWLEDGE_SEARCH_TOOL_NAME
    ][-1]
    assert last["status"] == "complete"
    assert last["budget_class"] == "refused"
    assert json.loads(last["provider_result"])["search_budget_spent"] is True
