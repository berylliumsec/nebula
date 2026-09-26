"""A long tool turn keeps an early finding after its output is folded away.

A turn's checkpoint folds older steps into receipts that say what each call
acted on. Once enough folded output accumulates, the context compactor turns
it into a progress memory that cites the steps it came from, and the next
checkpoint carries it, so the model still sees what an early step found
without reading its output again. The memory changes only when the
checkpoint advances, is summarised incrementally, is charged like
conversation compaction, and never stops the turn when it fails.
"""

import asyncio
import json

import pytest

from nebula.v3 import context as context_module
from nebula.v3.context import ContextCompactionError, ContextCompactor, ContextSource
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatTokenUsage,
    ChatTurn,
    ContextMemory,
    ContextMemoryItem,
    ContextOwnerType,
    ContextSegment,
    ContextSnapshot,
    ContextSnapshotStatus,
    ContextSourceReference,
)
from nebula.v3.providers import ModelRequest, ToolChoice
from nebula.v3.turn_progress import (
    DIGEST_TOKEN_TRIGGER,
    PROGRESS_SCHEMA,
    STEP_OUTPUT_EXCERPT_CHARS,
    TurnProgress,
    progress_block,
    step_source,
)
from tests.v3.test_chat_tool_loop import _response
from tests.v3.test_in_turn_context_pruning import (
    ANSWER,
    LongTurnProvider,
    ScanBroker,
    _long_turn,
    _turn_requests,
)
from tests.v3.test_turn_prompt_cache import _ledger_turn

FLAG = "/srv/app/flag-1.conf"
CHECKPOINT = "EARLIER TOOL HISTORY CHECKPOINT"


class PlantedScanBroker(ScanBroker):
    """The first scan names a configuration path no later step repeats."""

    async def execute(self, invocation, scope, *, approval=None):
        result = await super().execute(invocation, scope, approval=approval)
        if len(self.calls) == 1:
            result.receipt.summary = f"Scan 1 finished; its config is {FLAG}."
        return result


class DigestingProvider(LongTurnProvider):
    """Answers compaction with a memory citing the first step it is shown."""

    def __init__(self, calls: int) -> None:
        super().__init__(calls)
        self.compactions: list[ModelRequest] = []

    async def complete(self, request: ModelRequest):
        if request.metadata.get("operation") != "context_compaction":
            return await super().complete(request)
        self.requests.append(request)
        self.compactions.append(request)
        payload = json.loads(str(request.messages[0].content))
        ids = [source["id"] for source in payload["sources"] if "id" in source]
        if not ids:
            # A roll-up of earlier memories keeps the first one's finding.
            earlier = json.loads(payload["sources"][0]["text"])
            return _response(text=json.dumps(earlier))
        facts = [{"text": f"Scan {ids[0][1:]} completed.", "sources": [ids[0]]}]
        if "t0" in ids:
            # The first scan (step 0) named the path.
            facts.insert(0, {"text": f"The config path is {FLAG}.", "sources": ["t0"]})
        return _response(
            text=json.dumps(
                {"confirmed_facts": facts, "summary": "Ports were scanned."}
            )
        )


def _checkpoint(request: ModelRequest) -> dict | None:
    content = str(request.messages[-1].content)
    if CHECKPOINT not in content:
        return None
    return json.loads(content.split(CHECKPOINT, 1)[1].split("\n", 1)[1])


def _routing(provider) -> list[ModelRequest]:
    return [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO
    ]


def test_an_early_finding_survives_folding_as_cited_progress_memory(tmp_path):
    broker = PlantedScanBroker()
    provider = DigestingProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert provider.compactions
    # The steps go to the compactor as short step ids with what each did.
    first = json.loads(str(provider.compactions[0].messages[0].content))
    assert first["sources"][0]["id"].startswith("t")
    assert first["sources"][0]["text"].startswith("Step ")
    routing = _routing(provider)
    last = _checkpoint(routing[-1])
    assert last is not None
    progress = last["progress"]
    assert progress["schema"] == PROGRESS_SCHEMA
    # The finding of the first scan, with the step it came from, although
    # that step's output left the request long ago.
    first_call = broker.calls[0]
    assert first_call is not None
    assert f"The config path is {FLAG}. (step " in json.dumps(progress)
    assert FLAG not in json.dumps(
        [result.output for result in routing[-1].tool_results]
    )
    folded = {
        step
        for first_step, last_step in last["covered_steps"]
        for step in range(first_step, last_step + 1)
    }
    covered = {
        step
        for first_step, last_step in progress["covered_steps"]
        for step in range(first_step, last_step + 1)
    }
    assert covered <= folded
    # Compaction is accounted like conversation compaction.
    assert completion.context_usage is not None
    assert completion.context_usage.total_tokens == 3 * len(provider.compactions)


def test_the_progress_memory_changes_only_when_the_checkpoint_advances(tmp_path):
    provider = DigestingProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, PlantedScanBroker())

    asyncio.run(service.complete(prepared))

    checkpoints = [_checkpoint(request) for request in _routing(provider)]
    for earlier, later in zip(checkpoints, checkpoints[1:]):
        if earlier is None or later is None:
            continue
        if earlier["integrity_sha256"] == later["integrity_sha256"]:
            # Between advances the checkpoint, progress included, is the
            # same bytes, so the provider's prefix cache keeps serving it.
            assert earlier == later
    assert any(
        checkpoint is not None and "progress" in checkpoint
        for checkpoint in checkpoints
    )


def test_each_refresh_summarises_only_newly_folded_steps(tmp_path):
    provider = DigestingProvider(calls=60)
    store, service, prepared = _long_turn(tmp_path, provider, PlantedScanBroker())

    asyncio.run(service.complete(prepared))

    snapshots = sorted(
        (
            item
            for item in store.list_entities(ContextSnapshot, limit=100)
            if item.owner_type == ContextOwnerType.CHAT_TURN
        ),
        key=lambda item: item.version,
    )
    assert len(snapshots) >= 3
    assert all(item.status == ContextSnapshotStatus.READY for item in snapshots)
    covered = [
        sorted(int(reference.source_id) for reference in item.source_references)
        for item in snapshots
    ]
    # Each memory covers every step folded so far ...
    for earlier, later in zip(covered, covered[1:]):
        assert earlier == later[: len(earlier)] and len(later) > len(earlier)
    leaf_calls = [
        request
        for request in provider.compactions
        if any(
            "id" in source
            for source in json.loads(str(request.messages[0].content))["sources"]
        )
    ]
    shown = [
        source["id"]
        for request in leaf_calls
        for source in json.loads(str(request.messages[0].content))["sources"]
    ]
    # ... but each refresh is shown only the steps folded since the previous
    # memory, which stands for the rest: every step is summarised once.
    assert len(shown) == len(set(shown))
    assert set(shown) == {f"t{step}" for step in covered[-1]}
    segments = store.list_entities(ContextSegment, limit=100)
    assert all(item.owner_type == ContextOwnerType.CHAT_TURN for item in segments)


def test_a_failed_refresh_leaves_the_receipts_and_the_turn_going(tmp_path, monkeypatch):
    attempts = []

    async def failing(self, **kwargs):
        attempts.append(kwargs)
        raise ContextCompactionError(
            "insufficient mission token budget for context compaction",
            usage=ChatTokenUsage(input_tokens=5, output_tokens=2, total_tokens=7),
        )

    monkeypatch.setattr(ContextCompactor, "compact", failing)
    provider = DigestingProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, PlantedScanBroker())

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert attempts
    checkpoints = [
        checkpoint
        for checkpoint in map(_checkpoint, _routing(provider))
        if checkpoint is not None
    ]
    assert checkpoints and all("progress" not in item for item in checkpoints)
    # A failed attempt is not repeated on every step: only once as much new
    # output has folded again.
    assert len(attempts) < len(_routing(provider)) // 3
    assert completion.context_usage is not None
    assert completion.context_usage.total_tokens == 7 * len(attempts)


def test_progress_summaries_are_charged_to_the_goal(tmp_path):
    provider = DigestingProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, PlantedScanBroker())
    goal = store.create(
        ChatGoal(
            id="goal-progress",
            engagement_id=prepared.engagement_id,
            session_id="session",
            objective="Map every open port",
            completion_criteria=["Each port is listed"],
            status=ChatGoalStatus.RUNNING,
            token_budget=1_000_000,
        )
    )
    turn = store.get(ChatTurn, "turn")
    prepared.turn = store.update(
        ChatTurn, turn.id, {"goal_id": goal.id}, expected_revision=turn.revision
    )
    # A goal's requests name their output allowance, as prepared ones do.
    prepared.model_request = prepared.model_request.model_copy(
        update={"max_output_tokens": 1_000}
    )

    asyncio.run(service.complete(prepared))

    assert provider.compactions
    # The objective is the goal's, framed as context for the compactor.
    assert (
        json.loads(str(provider.compactions[0].messages[0].content))["objective"]
        == "Map every open port"
    )
    charged = store.get(ChatGoal, goal.id).usage.total_tokens
    assert charged >= 3 * len(provider.compactions)


def _entry(step: int, **fields) -> dict:
    return {
        "step": step,
        "response_group": f"group-{step}",
        "model_call_id": f"call-{step}",
        "tool_call_id": f"tool-{step}",
        "name": "run_command",
        "arguments": {"command": f"cat /srv/file-{step}"},
        "status": "complete",
        "provider_result": json.dumps({"status": "complete", "stdout": "x" * 4_000}),
        "result_summary": f"Read file {step}.",
        **fields,
    }


def _turn_with_steps(tmp_path, count: int):
    store, turn, ledger = _ledger_turn(tmp_path)
    for step in range(count):
        ledger.append(turn.id, _entry(step))
    return store, ledger, turn


def test_a_step_source_says_what_the_step_did_within_a_bound():
    secret = "abcdefghijklmnopqrstuvwxyz0123"
    source = step_source(
        _entry(
            4,
            arguments={"command": f"curl -H 'Authorization: Bearer {secret}' x"},
            provider_result=json.dumps({"stdout": f"token={secret} " + "y" * 5_000}),
        )
    )

    assert source.reference == ContextSourceReference(
        source_kind="turn_step", source_id="4"
    )
    lines = source.content.split("\n")
    assert lines[0] == "Step 4: run_command (complete)"
    assert lines[1].startswith("Did: command=curl")
    assert lines[2] == "Summary: Read file 4."
    assert secret not in source.content
    assert len(lines[3]) < STEP_OUTPUT_EXCERPT_CHARS + 60
    assert step_source(_entry(4)) == step_source(_entry(4))


def test_a_checkpoint_carries_progress_only_for_steps_it_folds(tmp_path):
    store, ledger, turn = _turn_with_steps(tmp_path, 30)
    seen: list[set[int]] = []

    def progress(steps: set[int]) -> dict | None:
        seen.append(steps)
        return {"schema": PROGRESS_SCHEMA, "covered_steps": [[min(steps), 3]]}

    checkpoint, _ = ledger.compacted_history(turn, advance=True, progress=progress)

    assert checkpoint is not None
    assert seen and min(seen[0]) == min(
        step
        for first, last in checkpoint.summary["covered_steps"]
        for step in range(first, last + 1)
    )
    assert checkpoint.summary["progress"]["covered_steps"][0][1] == 3
    # A checkpoint already recorded is replayed as recorded.
    again, _ = ledger.compacted_history(turn, progress=lambda steps: {"other": 1})
    assert again == checkpoint

    memory = ContextMemory(
        summary="Work so far.",
        confirmed_facts=[
            ContextMemoryItem(
                text="Found it.",
                sources=[
                    ContextSourceReference(source_kind="turn_step", source_id="2"),
                    ContextSourceReference(source_kind="turn_step", source_id="3"),
                ],
            )
        ],
    )
    snapshot = ContextSnapshot(
        engagement_id="project",
        owner_type=ContextOwnerType.CHAT_TURN,
        owner_id=turn.id,
        status=ContextSnapshotStatus.READY,
        compacted_through=3,
        memory=memory,
        source_references=[
            ContextSourceReference(source_kind="turn_step", source_id=str(step))
            for step in range(4)
        ],
        provider_profile_id="provider",
        model="model-a",
        prompt_version="nebula-context-v2",
        source_sha256="0" * 64,
    )
    block = progress_block(snapshot, set(range(10)))
    assert block is not None
    assert block["covered_steps"] == [[0, 3]]
    assert block["memory"]["confirmed_facts"] == ["Found it. (steps 2, 3)"]
    # A memory of steps the checkpoint does not fold is not carried.
    assert progress_block(snapshot, {0, 1, 2}) is None


def test_turn_memory_sources_must_be_steps_of_that_turn(tmp_path):
    store, ledger, turn = _turn_with_steps(tmp_path, 4)

    with pytest.raises(ValueError, match="not a step of this turn"):
        asyncio.run(
            ContextCompactor(store).compact(
                owner_type=ContextOwnerType.CHAT_TURN,
                owner_id=turn.id,
                engagement_id=turn.engagement_id,
                provider_profile=None,
                provider=None,
                model="model-a",
                sources=[
                    ContextSource(
                        ContextSourceReference(source_kind="turn_step", source_id="99"),
                        "Step 99",
                    )
                ],
                compacted_through=99,
            )
        )


@pytest.mark.parametrize(("count", "expected"), [(10, False), (16, True)])
def test_a_refresh_is_due_only_after_enough_new_output_folds(tmp_path, count, expected):
    store, ledger, turn = _turn_with_steps(tmp_path, count)
    progress = TurnProgress(store, ledger)
    foldable = ledger.foldable(turn)
    size = context_module.estimate_tokens(
        json.dumps(
            [[entry["arguments"], entry["provider_result"]] for entry in foldable],
            sort_keys=True,
        )
    )
    assert (size >= DIGEST_TOKEN_TRIGGER) == expected

    due = progress.due(turn)

    assert bool(due) == expected
    if due:
        # Every foldable step, so the memory is one account of the turn.
        assert [source.reference.source_id for source in due] == [
            str(entry["step"]) for entry in foldable
        ]


def test_deleting_the_conversation_removes_its_turns_progress_memory(tmp_path):
    provider = DigestingProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, PlantedScanBroker())
    asyncio.run(service.complete(prepared))
    assert [
        item
        for item in store.list_entities(ContextSnapshot, limit=100)
        if item.owner_type == ContextOwnerType.CHAT_TURN
    ]

    store.delete_chat_session("session")

    assert not [
        item
        for item in store.list_entities(ContextSnapshot, limit=100)
        if item.owner_type == ContextOwnerType.CHAT_TURN
    ]
    assert not store.list_entities(ContextSegment, limit=100)


def test_no_refresh_below_the_trigger(tmp_path):
    provider = DigestingProvider(calls=6)
    store, service, prepared = _long_turn(tmp_path, provider, PlantedScanBroker())

    asyncio.run(service.complete(prepared))

    assert provider.compactions == []


def test_a_long_output_keeps_its_head_and_tail():
    from nebula.v3.turn_progress import _excerpt

    text = "HEAD " + "x" * 10_000 + " db_host = 10.44.3.17:5432 TAIL"

    excerpt = _excerpt(text, STEP_OUTPUT_EXCERPT_CHARS, 1_000)

    assert len(excerpt) <= STEP_OUTPUT_EXCERPT_CHARS
    assert excerpt.startswith("HEAD ")
    assert excerpt.endswith("db_host = 10.44.3.17:5432 TAIL")
    assert "characters omitted] …" in excerpt
    assert _excerpt("short", STEP_OUTPUT_EXCERPT_CHARS, 1_000) == "short"


def test_a_small_window_refreshes_sooner_and_carries_a_bounded_memory():
    from nebula.v3.turn_progress import block_budget, digest_trigger

    assert digest_trigger(1_000_000) == DIGEST_TOKEN_TRIGGER
    assert digest_trigger(13_952) == 3_488
    assert block_budget(13_952) == 1_395
    memory = ContextMemory(
        summary="Keys were read.",
        references=[
            ContextMemoryItem(
                text=f"Key {index}: KAPPA-{4700 + index}",
                sources=[
                    ContextSourceReference(source_kind="turn_step", source_id="0")
                ],
            )
            for index in range(200)
        ],
    )
    snapshot = ContextSnapshot(
        engagement_id="project",
        owner_type=ContextOwnerType.CHAT_TURN,
        owner_id="turn",
        status=ContextSnapshotStatus.READY,
        compacted_through=0,
        memory=memory,
        source_references=[
            ContextSourceReference(source_kind="turn_step", source_id="0")
        ],
        provider_profile_id="provider",
        model="model-a",
        prompt_version="nebula-context-v2",
        source_sha256="0" * 64,
    )

    whole = progress_block(snapshot, {0})
    bounded = progress_block(snapshot, {0}, max_tokens=400)

    assert whole is not None and bounded is not None
    assert len(whole["memory"]["references"]) == 200
    assert 0 < len(bounded["memory"]["references"]) < 200
    assert context_module.estimate_tokens(json.dumps(bounded["memory"])) < 600


def test_without_a_goal_the_turn_request_guides_the_memory(tmp_path):
    provider = DigestingProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, PlantedScanBroker())

    asyncio.run(service.complete(prepared))

    assert provider.compactions
    prompt = json.loads(str(provider.compactions[0].messages[0].content))
    assert prompt["objective"] == "Use the safe tool once."
