"""Subagent and peer-message delivery.

Every result a model receives fits the model-delivery bound, a report or
message counts as received only once it reached the receiver whole, finished
children are listed without reading their histories, and a Core update does
not strand a parent parked on its subagents.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from nebula.v3.chat import ChatHistoryConflict, _runtime_digest_matches
from nebula.v3.chat_agent_messages import AgentMessageService
from nebula.v3.chat_subagents import (
    HARNESS_REPORT_CONTEXT_CHARACTERS,
    PROVIDER_RESULT_BYTES,
    SUBAGENT_TOOLS_CONTRACT,
    SubagentService,
    subagent_specs,
)
from nebula.v3.domain import (
    ChatAgentMessage,
    ChatAgentMessageStatus,
    ChatBackend,
    ChatSession,
    ChatSubagent,
    ChatSubagentMessage,
    ChatSubagentMessageStatus,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessTurn,
    HarnessTurnStatus,
    ProviderProfile,
    ToolCallOrigin,
    utc_now,
)
from nebula.v3.providers import ModelRequest, ModelResponse, ToolChoice
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import model_result_bytes
from nebula.v3.tools import ToolInvocation
from tests.v3.test_chat_agent_messages import _invocation, _session
from tests.v3.test_chat_subagents import (
    CHILD_MARKER,
    RoutedProvider,
    _call,
    _finish,
    _request,
    _response,
    _setup,
    _until,
)
from tests.v3.test_harness_provider_subagents import (
    ScriptedConnection,
    _payload,
    _prepare,
)
from tests.v3.test_harness_provider_subagents import _setup as _harness_setup

ROUTES = "ROUTES-BEGIN " + "route handler mapped; " * 240 + "ROUTES-END"
AUTH = "AUTH-BEGIN " + "session cookie lacks SameSite; " * 360 + "AUTH-END"
REPORTS = {"Map the API routes.": ROUTES, "Audit the auth module.": AUTH}


class ReportingProvider(RoutedProvider):
    """Children answer with the long report their task asks for."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation") == "conversation_naming":
            return _response(text="Named")
        if CHILD_MARKER not in (request.instructions or ""):
            return await super().complete(request)
        self.child_requests.append(request)
        if self.child_gate is not None:
            await self.child_gate.wait()
        if request.tool_choice != ToolChoice.NONE:
            return _finish(f"child-finish-{len(self.child_requests)}")
        text = " ".join(str(message.content) for message in request.messages)
        return _response(text=next(v for k, v in REPORTS.items() if k in text))


def _outputs(request: ModelRequest, *names: str) -> list[dict]:
    outputs = []
    for result in request.tool_results:
        if result.name in names:
            output = result.output
            outputs.append(json.loads(output) if isinstance(output, str) else output)
    return outputs


def _parent(store: NebulaStore, project: Engagement, profile, **session_fields):
    session = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Supervisor",
            provider_profile_id=profile.id,
            model="model-a",
            **session_fields,
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    return session, turn


def _finished(
    store: NebulaStore,
    project: Engagement,
    session: ChatSession,
    turn: ChatTurn,
    name: str,
    result: str,
    *,
    reported: bool = False,
    backend: ChatBackend = ChatBackend.PROVIDER,
    child_turn_id: str | None = None,
) -> ChatSubagent:
    return store.create(
        ChatSubagent(
            engagement_id=project.id,
            parent_session_id=session.id,
            parent_turn_id=turn.id,
            parent_backend=backend,
            child_session_id=f"child-of-{name}",
            child_turn_id=child_turn_id,
            provider_profile_id="provider",
            model="model-a",
            name=name,
            task=f"{name} task.",
            status=ChatSubagentStatus.COMPLETED,
            finished_at=utc_now(),
            reported_at=utc_now() if reported else None,
            result=result,
        )
    )


def test_long_reports_reach_a_working_parent_whole_and_within_the_bound(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        seen: dict[str, ModelRequest] = {}
        provider = ReportingProvider([], [])
        store, project, profile, chat = _setup(tmp_path, provider)
        # A real model's window, so no earlier result is cleared to fit.
        store.update(
            ProviderProfile,
            profile.id,
            {
                "metadata": {
                    **profile.metadata,
                    "model_descriptors": [{"id": "model-a", "context_window": 128_000}],
                }
            },
            expected_revision=profile.revision,
        )

        async def wait_once_both_finished(request: ModelRequest) -> ModelResponse:
            del request
            assert provider.child_gate is not None
            provider.child_gate.set()
            await _until(
                lambda: (
                    [item.status for item in store.list_entities(ChatSubagent)]
                    == [ChatSubagentStatus.COMPLETED] * 2
                )
            )
            return _call("p3", "wait_subagents", subagent_ids=None, mode=None)

        async def read_reports(request: ModelRequest) -> ModelResponse:
            seen["request"] = request
            return _finish("p4")

        provider.parent = [
            _call("p1", "start_subagent", task="Map the API routes.", name="Routes"),
            _call("p2", "start_subagent", task="Audit the auth module.", name="Auth"),
            wait_once_both_finished,
            read_reports,
            _response(text="Both reports read."),
        ]
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Split the review.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )

        # Every subagent result the model read fits the bound: none was
        # swapped for the "exceeded the model-delivery bound" placeholder.
        outputs = _outputs(seen["request"], "wait_subagents", "list_subagents")
        assert outputs
        for output in outputs:
            assert output.get("schema") != "nebula.bounded-result/v1"
            assert model_result_bytes(output) <= PROVIDER_RESULT_BYTES
        chunks: dict[str, list[str]] = {"Routes": [], "Auth": []}
        for output in outputs:
            for view in output["subagents"]:
                if view.get("report"):
                    chunks[view["name"]].append(view["report"])
        # Each report arrived exactly once and whole: the short one in the
        # wait result, the long one in numbered parts Core added after it.
        assert "".join(chunks["Routes"]) == ROUTES
        assert "".join(chunks["Auth"]) == AUTH
        waited = outputs[0]
        assert [view["name"] for view in waited["subagents"] if "report" in view] == [
            "Routes"
        ]
        assert any(view.get("report_follows") for view in waited["subagents"])
        parts = [
            view["report_part"]
            for output in outputs[1:]
            for view in output["subagents"]
            if "report_part" in view
        ]
        assert parts == ["1/2", "2/2"]
        assert all(output.get("delivered_by") == "nebula" for output in outputs[1:])
        assert all(item.reported_at for item in store.list_entities(ChatSubagent))
        # Nothing needed resending.
        assert store.list_entities(ChatSubagentMessage) == []
        await chat.shutdown()

    asyncio.run(scenario())


def test_core_delivers_only_news_and_lists_stay_within_the_bound(
    tmp_path: Path,
) -> None:
    store, project, profile, chat = _setup(tmp_path, RoutedProvider([], []))
    session, turn = _parent(store, project, profile)
    received = [
        _finished(
            store, project, session, turn, f"Past {index}", "x" * 300, reported=True
        )
        for index in range(60)
    ]
    fresh = _finished(store, project, session, turn, "Fresh", "New finding. " * 200)
    service = chat.subagents

    delivery = service.routing_delivery(turn, {"list_subagents"})
    assert delivery is not None
    ((output, summary),) = delivery.results
    assert [view["subagent_id"] for view in output["subagents"]] == [fresh.id]
    assert output["subagents"][0]["report"] == fresh.result.strip()
    assert summary == "1 subagent report received"
    # Received once Core saved the step, not while building it.
    assert store.get(ChatSubagent, fresh.id).reported_at is None
    delivery.commit()
    assert store.get(ChatSubagent, fresh.id).reported_at is not None
    assert service.routing_delivery(turn, {"list_subagents"}) is None

    listed = service.list_output(session.id)
    assert model_result_bytes(listed) <= PROVIDER_RESULT_BYTES
    assert "more_updates" in listed
    assert all(
        view.get("report_received_earlier") and "report" not in view
        for view in listed["subagents"]
    )
    # A wait on a report already received does not send it again.
    (view,) = service.wait_output([received[0].id])["subagents"]
    assert view["report_received_earlier"] is True
    assert "report" not in view
    # With nothing running, waiting without ids no longer means every past
    # child whose result was never posted.
    assert service.resolve_wait(session.id, None) == []


def test_listing_finished_subagents_reads_no_child_turn_or_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project, profile, chat = _setup(tmp_path, RoutedProvider([], []))
    session, turn = _parent(store, project, profile)
    records = []
    for index in range(20):
        child_turn = store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=f"child-session-{index}",
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.COMPLETE,
                next_step=6,
                reasoning="r" * 50_000,
            )
        )
        for step in range(6):
            chat.turn_ledger.append(
                child_turn.id,
                {
                    "step": step,
                    "name": "run_command",
                    "arguments": {"command": f"scan {index}-{step}"},
                    "status": "complete",
                    "provider_result": "y" * 2_000,
                },
            )
        records.append(
            _finished(
                store,
                project,
                session,
                turn,
                f"Child {index}",
                "done",
                reported=True,
                child_turn_id=child_turn.id,
            )
        )
    turn_loads: list[str] = []
    histories: list[str] = []
    real_get = store.get
    real_history = chat.turn_ledger.history

    def counting_get(model, entity_id):
        if model is ChatTurn:
            turn_loads.append(entity_id)
        return real_get(model, entity_id)

    def counting_history(loaded: ChatTurn):
        histories.append(loaded.id)
        return real_history(loaded)

    monkeypatch.setattr(store, "get", counting_get)
    monkeypatch.setattr(chat.turn_ledger, "history", counting_history)

    views = [
        chat.subagents.view(item) for item in chat.subagents.for_session(session.id)
    ]
    listed = chat.subagents.list_output(session.id)

    assert turn_loads == []
    assert histories == []
    assert {view["step_count"] for view in views} == {6}
    assert views[0]["recent_steps"] == [
        {"tool": "run_command", "detail": f"scan 0-{step}", "status": "complete"}
        for step in range(2, 6)
    ]
    assert {view["steps"] for view in listed["subagents"]} == {6}


def test_parked_parent_resumes_across_a_core_update_that_changed_tool_specs(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call("p1", "start_subagent", task="Count routes.", name="Routes"),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p3"),
                _response(text="Three routes."),
            ],
            child=[_response(text="Found 3 routes.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Count.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status
                == ChatTurnStatus.WAITING_CALLBACK
            )
        )
        parked = store.get(ChatTurn, parent_turn_id)
        assert (
            parked.request_snapshot["automation_runtime_digest"]
            == SUBAGENT_TOOLS_CONTRACT
        )
        # What the Core before this one recorded: a hash of every ToolSpec
        # field, here without one a later Core added.
        legacy = hashlib.sha256(
            json.dumps(
                {
                    name: {
                        key: value
                        for key, value in spec.model_dump(mode="json").items()
                        if key != "parallelism"
                    }
                    for name, spec in subagent_specs().items()
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        store.update(
            ChatTurn,
            parked.id,
            {
                "request_snapshot": {
                    **parked.request_snapshot,
                    "automation_runtime_digest": f"subagents-{legacy[:16]}",
                }
            },
            expected_revision=parked.revision,
        )

        provider.child_gate.set()
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status
                in {ChatTurnStatus.COMPLETE, ChatTurnStatus.FAILED}
            )
        )
        finished = store.get(ChatTurn, parent_turn_id)
        assert finished.error is None
        assert finished.status == ChatTurnStatus.COMPLETE
        await chat.shutdown()

    asyncio.run(scenario())


def test_resume_still_refuses_a_changed_command_runtime() -> None:
    legacy = "sha256:aa+subagents-0123456789abcdef+agent-messages-fedcba9876543210"
    assert _runtime_digest_matches(legacy, "sha256:aa+subagents-v1+agent-messages-v1")
    assert not _runtime_digest_matches(
        legacy, "sha256:bb+subagents-v1+agent-messages-v1"
    )
    assert not _runtime_digest_matches("subagents-v1", "subagents-v2")
    assert not _runtime_digest_matches(None, "subagents-v1")
    assert _runtime_digest_matches(None, None)


def test_prepare_resume_refuses_only_a_real_runtime_change(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call("p1", "start_subagent", task="Count routes.", name="Routes"),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Count.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status
                == ChatTurnStatus.WAITING_CALLBACK
            )
        )
        parked = store.get(ChatTurn, parent_turn_id)
        store.update(
            ChatTurn,
            parked.id,
            {
                "request_snapshot": {
                    **parked.request_snapshot,
                    "automation_runtime_digest": "sha256:other+subagents-v1",
                }
            },
            expected_revision=parked.revision,
        )
        with pytest.raises(ChatHistoryConflict):
            chat.prepare_resume(parent_turn_id)
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_wait_after_its_turn_ended_leaves_reports_for_the_next_prompt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, project, profile, chat = _setup(tmp_path, RoutedProvider([], []))
        # The service reads the backend from each record.
        session, turn = _parent(store, project, profile)
        record = _finished(
            store,
            project,
            session,
            turn,
            "Checker",
            "Found it.",
            backend=ChatBackend.HARNESS,
        )
        service = chat.subagents

        ended = await service.wait_for(
            session.id, None, "all", 1.0, still_waiting=lambda: False
        )
        assert ended["subagents"][0]["report"] == "Found it."
        assert store.get(ChatSubagent, record.id).reported_at is None
        assert [item.id for item in service.harness_update(session.id).records] == [
            record.id
        ]

        read = await service.wait_for(
            session.id, None, "all", 1.0, still_waiting=lambda: True
        )
        assert read["subagents"][0]["report"] == "Found it."
        assert store.get(ChatSubagent, record.id).reported_at is not None
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_prompt_carries_what_fits_and_marks_only_that(tmp_path: Path) -> None:
    store, project, profile, chat = _setup(tmp_path, RoutedProvider([], []))
    session, turn = _parent(store, project, profile)
    records = [
        _finished(
            store,
            project,
            session,
            turn,
            f"Child {index}",
            f"Report {index}: " + "detail " * 1_700,
            backend=ChatBackend.HARNESS,
        )
        for index in range(5)
    ]
    service = chat.subagents

    first = service.harness_update(session.id)
    context = SubagentService.harness_report_context(first)
    assert len(context) <= HARNESS_REPORT_CONTEXT_CHARACTERS + 200
    assert 0 < len(first.records) < len(records)
    assert "subagent.list returns them" in context
    for record in first.records:
        assert record.result.strip() in context
    service.mark_delivered(first)

    second = service.harness_update(session.id)
    assert {item.id for item in second.records}.isdisjoint(
        item.id for item in first.records
    )
    assert second.records


def test_harness_prompt_updates_count_once_the_vendor_accepts_the_prompt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _harness_setup(tmp_path)
        child = chat.provider_factory(store.get(ProviderProfile, "provider"))
        child.answers = ["Cookies lack SameSite."]
        child.gate = asyncio.Event()

        async def delegate(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            _payload(await connection.call("subagent.start", task="Review auth."))
            return "Started a review."

        adapter.script = delegate
        parent_chat, _, turn = _prepare(runtime, project, harness, "Review auth.")
        await runtime.start_chat_turn(turn.id)
        (record,) = store.list_entities(ChatSubagent)
        child.gate.set()
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        assert store.get(ChatSubagent, record.id).reported_at is None

        connection = adapter.connections[0]
        working_run_turn = connection.run_turn

        async def refused(prompt: str, *, model: str, images=None):
            del model, images
            connection.prompts.append(prompt)
            raise ConnectionError("the vendor refused the turn")
            yield  # pragma: no cover - makes this an async generator

        connection.run_turn = refused  # type: ignore[method-assign]
        _, _, failing = _prepare(
            runtime, project, harness, "What did it find?", chat_id=parent_chat.id
        )
        assert "Cookies lack SameSite." in failing.prompt
        await runtime.start_chat_turn(failing.id)
        assert store.get(HarnessTurn, failing.id).status in {
            HarnessTurnStatus.FAILED,
            HarnessTurnStatus.INTERRUPTED,
        }
        # The vendor never took the prompt, so the report is still unreceived.
        assert store.get(ChatSubagent, record.id).reported_at is None

        connection.run_turn = working_run_turn  # type: ignore[method-assign]
        adapter.script = None
        connection.script = None
        replacement = await runtime.retry_turn(failing.id)
        await runtime._chat_turn_tasks[replacement.id]
        assert "Cookies lack SameSite." in connection.prompts[-1]
        assert (
            store.get(HarnessTurn, replacement.id).status == HarnessTurnStatus.COMPLETE
        )
        assert store.get(ChatSubagent, record.id).reported_at is not None
        await chat.shutdown()

    asyncio.run(scenario())


def test_long_peer_message_reaches_a_working_agent_in_parts(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = NebulaStore(tmp_path / "peers.db")
        project = store.create(Engagement(name="Project"))
        sender = _session(store, project.id, "Coordinator")
        recipient = _session(store, project.id, "Reviewer")
        service = AgentMessageService(store)
        content = "PEER-BEGIN " + "the retry boundary is idempotent; " * 550 + "END"
        sent = await service.send(_invocation(sender, tmp_path), recipient.id, content)
        turn = ChatTurn(
            engagement_id=project.id,
            session_id=recipient.id,
            provider_profile_id=recipient.provider_profile_id,
            model="model-a",
        )

        # Asked for directly, it does not fit one result and waits whole.
        read = service.read_output(
            _invocation(recipient, tmp_path).model_copy(
                update={"tool_name": "read_agent_messages"}
            )
        )
        assert read["messages"] == []
        assert "more_messages" in read
        assert model_result_bytes(read) <= PROVIDER_RESULT_BYTES

        delivery = service.routing_delivery(turn, {"read_agent_messages"})
        assert delivery is not None and len(delivery.results) > 1
        for output, _ in delivery.results:
            assert model_result_bytes(output) <= PROVIDER_RESULT_BYTES
        assert (
            "".join(
                view["content"]
                for output, _ in delivery.results
                for view in output["messages"]
            )
            == content
        )
        assert (
            store.get(ChatAgentMessage, sent["message_id"]).status
            == ChatAgentMessageStatus.PENDING
        )
        delivery.commit()
        assert (
            store.get(ChatAgentMessage, sent["message_id"]).status
            == ChatAgentMessageStatus.DELIVERED
        )

    asyncio.run(scenario())


def test_long_parent_message_reaches_a_working_subagent_in_parts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, project, profile, chat = _setup(tmp_path, RoutedProvider([], []))
        session, turn = _parent(store, project, profile)
        child_session = store.create(
            ChatSession(
                engagement_id=project.id,
                title="Subagent",
                provider_profile_id=profile.id,
                model="model-a",
                parent_session_id=session.id,
                metadata={"subagent_id": "child-record"},
            )
        )
        child_turn = store.create(
            ChatTurn(
                id="child-turn",
                engagement_id=project.id,
                session_id=child_session.id,
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        store.create(
            ChatSubagent(
                id="child-record",
                engagement_id=project.id,
                parent_session_id=session.id,
                parent_turn_id=turn.id,
                child_session_id=child_session.id,
                child_turn_id=child_turn.id,
                provider_profile_id=profile.id,
                model="model-a",
                name="Reader",
                task="Read the item.",
            )
        )
        content = "PARENT-BEGIN " + "check the staging bucket policy; " * 600 + "END"
        await chat.subagents.send_to_child(
            session.id, "child-record", content, parent_turn_id=turn.id
        )
        invocation = ToolInvocation(
            engagement_id=project.id,
            run_id=child_turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=child_session.id,
            chat_turn_id=child_turn.id,
            tool_name="read_parent_messages",
            arguments={},
            workspace=tmp_path,
        )
        read = chat.subagents.read_parent_messages(invocation)
        assert read["messages"] == [] and "more_messages" in read

        delivery = chat.subagents.routing_delivery(child_turn, {"read_parent_messages"})
        assert delivery is not None and len(delivery.results) > 1
        for output, _ in delivery.results:
            assert model_result_bytes(output) <= PROVIDER_RESULT_BYTES
        assert (
            "".join(
                view["content"]
                for output, _ in delivery.results
                for view in output["messages"]
            )
            == content
        )
        (message,) = store.list_entities(ChatSubagentMessage)
        assert message.status == ChatSubagentMessageStatus.PENDING
        delivery.commit()
        assert (
            store.get(ChatSubagentMessage, message.id).status
            == ChatSubagentMessageStatus.DELIVERED
        )
        await chat.shutdown()

    asyncio.run(scenario())
