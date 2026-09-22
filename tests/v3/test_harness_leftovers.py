"""Codex and Claude harness protocol gaps left after the harness audits.

Codex behaviour is modelled on the app-server source:
- every pending server request of a thread is aborted when a turn starts,
  completes or is interrupted, and each one then gets ``serverRequest/resolved``
  (``bespoke_event_handling.rs`` ``abort_pending_server_requests`` and
  ``resolve_server_request_on_thread_listener``);
- ``turn/interrupt`` for a turn that is no longer active fails with invalid
  request "no active turn to interrupt" (``turn_processor.rs``);
- ``model/rerouted`` is sent when the server served a request with another model
  (``session/mod.rs`` ``maybe_warn_on_server_model_mismatch``).

The Claude CLI behaviour (checked against the bundled CLI of the pinned SDK):
guidance sent while the model writes its final answer is queued and answered in
a turn of its own after that turn's result; guidance sent while a tool runs is
absorbed into the running turn. With ``--replay-user-messages`` the CLI echoes
each stdin message when a turn takes it, and coalesces queued messages into one.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import claude_agent_sdk
import pytest

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    Approval,
    ApprovalStatus,
    ChatTurn,
    Engagement,
    HarnessInteraction,
    HarnessInteractionStatus,
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnStatus,
    ToolCall,
    ToolCallStatus,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    ClaudeAgentSdkAdapter,
    ClaudeAgentSdkConnection,
    CodexAppServerAdapter,
    CodexAppServerConnection,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessHealth,
    HarnessInteractionRequest,
    HarnessPermissionDecision,
    HarnessProviderError,
    HarnessRuntimeService,
    PermissionTicket,
)
from nebula.v3.storage import NebulaStore

THREAD = "thread-leftovers"
TURN = "turn-leftovers"
DEADLINE_SECONDS = 10.0


# Codex fixtures ----------------------------------------------------------------


class ScriptedCodexRpc:
    """Stand-in for ``_CodexRpc``: ``turn/start`` queues the scripted frames."""

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.script = list(script or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[Any, dict[str, Any]]] = []
        self.errors: list[tuple[Any, int, str]] = []
        self.running_turns: dict[str, str] = {}
        self.connection_state = "connected"
        self.interrupt_error: dict[str, Any] | None = None

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "turn/start":
            for frame in self.script:
                await self.events.put(frame)
            return {"turn": {"id": TURN, "status": "inProgress", "items": []}}
        if method == "turn/interrupt" and self.interrupt_error is not None:
            raise HarnessProviderError("Codex app-server", self.interrupt_error)
        return {}

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        self.errors.append((request_id, code, message))

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        return None

    async def close(self) -> None:
        return None


def _notification(method: str, **params: Any) -> dict[str, Any]:
    params.setdefault("threadId", THREAD)
    return {"method": method, "params": params}


def _agent_completed(text: str) -> dict[str, Any]:
    return _notification(
        "item/completed",
        turnId=TURN,
        item={"id": "msg-1", "type": "agentMessage", "text": text},
    )


def _turn_completed(status: str = "completed") -> dict[str, Any]:
    return _notification(
        "turn/completed", turn={"id": TURN, "status": status, "items": []}
    )


def _command_approval(request_id: int, turn_id: str = TURN) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": THREAD,
            "turnId": turn_id,
            "itemId": f"cmd-{request_id}",
            "command": "nmap -sV 10.0.0.5",
            "cwd": "/workspace",
        },
    }


def _user_input(request_id: int) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": THREAD,
            "turnId": TURN,
            "itemId": f"input-{request_id}",
            "questions": [{"id": "scope", "question": "Which subnet?"}],
        },
    }


def _elicitation(request_id: int) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": THREAD,
            "turnId": TURN,
            "serverName": "scanner",
            "mode": "form",
            "message": "Confirm the target",
            "requestedSchema": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
            },
        },
    }


async def _allow(request: Any) -> PermissionTicket:
    future: asyncio.Future[HarnessPermissionDecision] = (
        asyncio.get_running_loop().create_future()
    )
    future.set_result(HarnessPermissionDecision(allowed=True))
    return PermissionTicket("approval-auto", request.vendor_request_id, future)


async def _collect(stream: Any) -> list[HarnessEvent]:
    return [event async for event in stream]


async def _until(predicate: Any, *, timeout: float = DEADLINE_SECONDS) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


def _fake_app_server(tmp_path: Path, body: str) -> HarnessProfile:
    """A Codex stand-in process; ``body`` handles each line with ``state`` kept."""

    executable = tmp_path / "codex-fixture"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "state = {}\n"
        "def out(value):\n"
        "    sys.stdout.write(json.dumps(value) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n" + textwrap.indent(textwrap.dedent(body), "    "),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return HarnessProfile(
        name="Codex fixture",
        kind=HarnessKind.CODEX_APP_SERVER,
        executable=str(executable),
    )


# 1. Server requests that arrive between turns -----------------------------------


def test_codex_requests_left_between_turns_are_answered_not_dropped():
    async def scenario() -> None:
        rpc = ScriptedCodexRpc([_agent_completed("Done."), _turn_completed()])
        presented: list[Any] = []

        async def permission(request: Any) -> PermissionTicket:
            presented.append(request)
            return await _allow(request)

        # Frames that arrived while no Nebula turn was reading this connection.
        for frame in (
            _notification(
                "item/agentMessage/delta",
                turnId="turn-earlier",
                itemId="old",
                delta="stale answer",
            ),
            {"id": 51, "method": "currentTime/read", "params": {"threadId": THREAD}},
            _command_approval(52, turn_id="turn-codex-started"),
            _user_input(53),
            _notification("serverRequest/resolved", requestId=53),
        ):
            rpc.events.put_nowait(frame)
        connection = CodexAppServerConnection(
            rpc,  # type: ignore[arg-type]
            external_session_id=THREAD,
            permission_handler=permission,
        )

        events = await asyncio.wait_for(
            _collect(connection.run_turn("next question", model="gpt-test")),
            DEADLINE_SECONDS,
        )

        # Codex waits for a reply to every request; one it already withdrew
        # (53) needs none. Nothing is presented to the operator for a turn
        # Nebula is not running.
        assert [(request_id, code) for request_id, code, _ in rpc.errors] == [
            (51, -32601),
            (52, -32000),
        ]
        assert "item/commandExecution/requestApproval" in rpc.errors[1][2]
        assert rpc.responses == []
        assert presented == []
        # The declined requests are written before the new turn starts.
        assert rpc.calls[0][0] == "turn/start"
        assert events[-1].type == "completed"
        assert events[-1].message == "Done."
        assert "stale answer" not in "".join(
            event.delta or "" for event in events if event.type == "message_delta"
        )
        declined = [
            event
            for event in events
            if event.type == "notice" and event.title == "Codex request declined"
        ]
        assert [event.payload["method"] for event in declined] == [
            "currentTime/read",
            "item/commandExecution/requestApproval",
        ]

    asyncio.run(scenario())


def test_codex_request_sent_after_a_turn_completed_gets_a_reply(tmp_path):
    profile = _fake_app_server(
        tmp_path,
        """
        if msg.get("method") == "turn/start":
            state["turns"] = state.get("turns", 0) + 1
            turn_id = f"turn-{state['turns']}"
            out({"id": msg["id"], "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
            text = "first" if state["turns"] == 1 else "reply=" + json.dumps(state.get("reply"))
            item = {"type": "agentMessage", "id": f"answer-{turn_id}", "text": text}
            out({"method": "item/completed", "params": {"threadId": "th", "turnId": turn_id, "item": item}})
            done = {"id": turn_id, "status": "completed", "items": []}
            out({"method": "turn/completed", "params": {"threadId": "th", "turn": done}})
            if state["turns"] == 1:
                # A turn Codex started on its own asks for approval while Nebula is idle.
                params = {"threadId": "th", "turnId": "turn-own", "itemId": "c", "command": "id"}
                out({"id": 77, "method": "item/commandExecution/requestApproval", "params": params})
        elif msg.get("id") == 77:
            state["reply"] = msg
        """,
    )

    async def scenario() -> None:
        rpc = await CodexAppServerAdapter()._connect(
            profile, CredentialStore(), (), tmp_path
        )
        try:
            connection = CodexAppServerConnection(
                rpc, external_session_id="th", permission_handler=_allow
            )
            first = await _collect(connection.run_turn("first", model="m"))
            assert first[-1].message == "first"
            await _until(lambda: not rpc.events.empty())
            second = await _collect(connection.run_turn("second", model="m"))
        finally:
            await rpc.close()
        reply = json.loads(second[-1].message.removeprefix("reply="))
        assert reply is not None, "the between-turn request was never answered"
        assert reply["id"] == 77
        assert reply["error"]["code"] == -32000
        assert "result" not in reply

    asyncio.run(asyncio.wait_for(scenario(), 20))


# 2. Requests Codex withdraws while the operator is deciding ----------------------


class PendingDecisions:
    """Operator handlers whose decisions never arrive unless a test sets them."""

    def __init__(self) -> None:
        self.approvals: list[asyncio.Future[HarnessPermissionDecision]] = []
        self.interactions: list[asyncio.Future[dict[str, Any]]] = []

    async def permission(self, request: Any) -> PermissionTicket:
        future: asyncio.Future[HarnessPermissionDecision] = (
            asyncio.get_running_loop().create_future()
        )
        self.approvals.append(future)
        return PermissionTicket(
            f"approval-{request.vendor_request_id}", "call-1", future
        )

    async def interaction(
        self, request: HarnessInteractionRequest
    ) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.interactions.append(future)
        return f"interaction-{request.vendor_request_id}", future


@pytest.mark.parametrize(
    ("request_frame", "event_type"),
    [
        (_command_approval(41), "approval"),
        (_user_input(42), "interaction"),
        (_elicitation(43), "interaction"),
    ],
    ids=["approval", "user-input", "mcp-elicitation"],
)
def test_codex_withdrawn_request_ends_the_wait_instead_of_hanging(
    request_frame: dict[str, Any], event_type: str
):
    async def scenario() -> None:
        request_id = request_frame["id"]
        rpc = ScriptedCodexRpc([request_frame])
        pending = PendingDecisions()
        connection = CodexAppServerConnection(
            rpc,  # type: ignore[arg-type]
            external_session_id=THREAD,
            permission_handler=pending.permission,
            interaction_handler=pending.interaction,
        )
        consumer = asyncio.ensure_future(
            _collect(connection.run_turn("scan", model="gpt-test"))
        )
        await _until(lambda: bool(pending.approvals or pending.interactions))
        # Another client interrupted the turn: Codex ends it, then resolves
        # the request it had pending (bespoke_event_handling.rs TurnAborted).
        rpc.events.put_nowait(
            _notification("turn/plan/updated", turnId=TURN, plan=[], explanation="")
        )
        rpc.events.put_nowait(_turn_completed("interrupted"))
        rpc.events.put_nowait(
            _notification("serverRequest/resolved", requestId=request_id)
        )

        events = await asyncio.wait_for(consumer, DEADLINE_SECONDS)

        withdrawn = [event for event in events if event.payload.get("withdrawn")]
        assert len(withdrawn) == 1
        assert withdrawn[0].type == event_type
        assert withdrawn[0].item_status == "cancelled"
        if event_type == "approval":
            assert withdrawn[0].approval_id == f"approval-{request_id}"
            assert withdrawn[0].tool_call_id == "call-1"
        else:
            assert withdrawn[0].payload["interaction_id"] == (
                f"interaction-{request_id}"
            )
        # Codex no longer waits for this answer, so none is sent.
        assert rpc.responses == []
        assert rpc.errors == []
        # Frames that arrived during the wait are still handled in order.
        assert events[-1].type == "interrupted"
        assert events.index(withdrawn[0]) < len(events) - 1

    asyncio.run(scenario())


def test_codex_answer_still_goes_out_when_another_request_is_resolved():
    async def scenario() -> None:
        rpc = ScriptedCodexRpc([_command_approval(61)])
        pending = PendingDecisions()
        connection = CodexAppServerConnection(
            rpc,  # type: ignore[arg-type]
            external_session_id=THREAD,
            permission_handler=pending.permission,
        )
        consumer = asyncio.ensure_future(
            _collect(connection.run_turn("scan", model="gpt-test"))
        )
        await _until(lambda: bool(pending.approvals))
        # The resolution of an earlier request is not a withdrawal of this one.
        rpc.events.put_nowait(_notification("serverRequest/resolved", requestId=60))
        rpc.events.put_nowait(_agent_completed("Scan finished."))
        rpc.events.put_nowait(_turn_completed())
        await asyncio.sleep(0.05)
        pending.approvals[0].set_result(HarnessPermissionDecision(allowed=True))

        events = await asyncio.wait_for(consumer, DEADLINE_SECONDS)

        assert rpc.responses == [(61, {"decision": "accept"})]
        assert not any(event.payload.get("withdrawn") for event in events)
        assert events[-1].type == "completed"
        assert events[-1].message == "Scan finished."

    asyncio.run(scenario())


class CodexScriptAdapter(HarnessAdapter):
    kind = HarnessKind.CODEX_APP_SERVER

    def __init__(self, rpc: ScriptedCodexRpc) -> None:
        self.rpc = rpc

    async def probe(self, profile: Any, credential_store: Any) -> HarnessHealth:
        raise NotImplementedError

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        return CodexAppServerConnection(
            self.rpc,  # type: ignore[arg-type]
            external_session_id=THREAD,
            permission_handler=request.permission_handler,
            interaction_handler=request.interaction_handler,
        )


def _codex_runtime(
    tmp_path: Path, rpc: ScriptedCodexRpc
) -> tuple[NebulaStore, HarnessRuntimeService, str, str]:
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(id="eng-a", name="Engagement A"))
    profile = store.create(
        HarnessProfile(
            id="codex-a",
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-test",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    adapter = CodexScriptAdapter(rpc)
    service = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: adapter,
    )
    _, owner, turn = service.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Scan the lab subnet",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    session = store.get(HarnessSession, turn.harness_session_id)
    # A host-mode session whose shell capability makes commands ask the operator.
    store.update(
        HarnessSession,
        session.id,
        {
            "metadata": {
                **session.metadata,
                "execution_mode": "host",
                "native_capabilities": HarnessNativeCapabilities(shell=True).model_dump(
                    mode="json"
                ),
            }
        },
        expected_revision=session.revision,
    )
    return store, service, owner.id, turn.id


def test_codex_withdrawn_approval_expires_the_pending_nebula_card(tmp_path):
    async def scenario() -> None:
        rpc = ScriptedCodexRpc([_command_approval(71)])
        store, service, _, turn_id = _codex_runtime(tmp_path, rpc)

        def pending() -> list[Approval]:
            return [
                approval
                for approval in store.list_entities(
                    Approval, engagement_id="eng-a", limit=100
                )
                if approval.status == ApprovalStatus.PENDING
            ]

        task = service.start_chat_turn(turn_id)
        await _until(lambda: bool(pending()))
        card = pending()[0]
        rpc.events.put_nowait(_turn_completed("interrupted"))
        rpc.events.put_nowait(_notification("serverRequest/resolved", requestId=71))

        await asyncio.wait_for(task, DEADLINE_SECONDS)

        expired = store.get(Approval, card.id)
        assert expired.status == ApprovalStatus.EXPIRED
        assert expired.decided_by == "harness"
        assert "Codex withdrew" in (expired.decision_note or "")
        call = store.get(ToolCall, card.tool_call_id or "")
        assert call.status == ToolCallStatus.CANCELLED
        assert store.get(HarnessTurn, turn_id).status == HarnessTurnStatus.INTERRUPTED
        assert rpc.responses == []
        await service.close_session(store.get(HarnessTurn, turn_id).harness_session_id)

    asyncio.run(scenario())


def test_codex_withdrawn_question_expires_the_pending_nebula_interaction(tmp_path):
    async def scenario() -> None:
        rpc = ScriptedCodexRpc([_user_input(72)])
        store, service, owner_id, turn_id = _codex_runtime(tmp_path, rpc)

        def pending() -> list[HarnessInteraction]:
            return [
                item
                for item in store.list_entities(
                    HarnessInteraction, engagement_id="eng-a", limit=100
                )
                if item.status == HarnessInteractionStatus.PENDING
            ]

        task = service.start_chat_turn(turn_id)
        await _until(lambda: bool(pending()))
        question = pending()[0]
        rpc.events.put_nowait(_turn_completed("interrupted"))
        rpc.events.put_nowait(_notification("serverRequest/resolved", requestId=72))

        await asyncio.wait_for(task, DEADLINE_SECONDS)

        assert (
            store.get(HarnessInteraction, question.id).status
            == HarnessInteractionStatus.EXPIRED
        )
        assert store.get(HarnessTurn, turn_id).status == HarnessTurnStatus.INTERRUPTED
        assert store.get(ChatTurn, owner_id).status.value == "interrupted"
        assert rpc.responses == []
        await service.close_session(store.get(HarnessTurn, turn_id).harness_session_id)

    asyncio.run(scenario())


# 3. A second interrupt of a turn that already ended ------------------------------


def test_codex_second_interrupt_of_an_ended_turn_is_not_an_error(tmp_path):
    profile = _fake_app_server(
        tmp_path,
        """
        method = msg.get("method")
        if method == "turn/start":
            state["active"] = "turn-1"
            out({"id": msg["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}})
        elif method == "turn/interrupt":
            state["interrupts"] = state.get("interrupts", 0) + 1
            if msg["params"]["turnId"] == state.get("active"):
                # Codex answers the interrupt once the turn has aborted.
                done = {"id": "turn-1", "status": "interrupted", "items": []}
                out({"method": "turn/completed", "params": {"threadId": "th", "turn": done}})
                state["active"] = None
                out({"id": msg["id"], "result": {}})
            else:
                error = {"code": -32600, "message": "no active turn to interrupt"}
                out({"id": msg["id"], "error": error})
        """,
    )

    async def scenario() -> None:
        rpc = await CodexAppServerAdapter()._connect(
            profile, CredentialStore(), (), tmp_path
        )
        try:
            connection = CodexAppServerConnection(
                rpc, external_session_id="th", permission_handler=_allow
            )
            consumer = asyncio.ensure_future(
                _collect(connection.run_turn("scan", model="m"))
            )
            await _until(lambda: connection.active_turn_id == "turn-1")
            # cancel_turn cancels the turn task, so nothing reads the
            # interrupted turn/completed, then interrupts the vendor turn.
            consumer.cancel()
            await connection.interrupt()
            with suppress(asyncio.CancelledError):
                await consumer
            # The cancelled stream_turn then interrupts the same turn again.
            await connection.interrupt()
        finally:
            await rpc.close()

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_codex_interrupt_of_a_turn_codex_already_ended_is_not_an_error():
    async def scenario() -> None:
        rpc = ScriptedCodexRpc()
        rpc.interrupt_error = {"code": -32600, "message": "no active turn to interrupt"}
        connection = CodexAppServerConnection(
            rpc,  # type: ignore[arg-type]
            external_session_id=THREAD,
            permission_handler=_allow,
        )
        # The turn completed on Codex's side before Nebula read turn/completed.
        connection.active_turn_id = TURN
        await connection.interrupt()
        assert rpc.calls == [("turn/interrupt", {"threadId": THREAD, "turnId": TURN})]

        other = {"code": -32600, "message": "expected active turn id x but found y"}
        rpc.interrupt_error = other
        connection.active_turn_id = "turn-stale"
        with pytest.raises(HarnessProviderError):
            await connection.interrupt()

    asyncio.run(scenario())


# 5. Pricing after model/rerouted ------------------------------------------------


def _token_usage(total_in: int, total_out: int, last_in: int, last_out: int) -> dict:
    return _notification(
        "thread/tokenUsage/updated",
        turnId=TURN,
        tokenUsage={
            "total": {"inputTokens": total_in, "outputTokens": total_out},
            "last": {"inputTokens": last_in, "outputTokens": last_out},
        },
    )


def test_codex_usage_after_a_reroute_is_priced_with_the_serving_model():
    async def scenario() -> None:
        rpc = ScriptedCodexRpc(
            [
                _token_usage(1_000, 100, 1_000, 100),
                _notification(
                    "model/rerouted",
                    turnId=TURN,
                    fromModel="gpt-5.4",
                    toModel="gpt-5.4-mini",
                    reason="highRiskCyberActivity",
                ),
                _token_usage(3_000, 300, 2_000, 200),
                _agent_completed("Done."),
                _turn_completed(),
            ]
        )
        connection = CodexAppServerConnection(
            rpc,  # type: ignore[arg-type]
            external_session_id=THREAD,
            permission_handler=_allow,
        )
        events = await asyncio.wait_for(
            _collect(connection.run_turn("scan", model="gpt-5.4")), DEADLINE_SECONDS
        )

        usage = [event for event in events if event.type == "usage"]
        detailed = usage[-1].detailed_usage
        assert detailed is not None
        assert detailed.input_tokens == 3_000
        assert detailed.output_tokens == 300
        # gpt-5.4: 1,000 x $2.50/M + 100 x $15/M; then gpt-5.4-mini serves
        # 2,000 x $0.75/M + 200 x $4.50/M.
        assert detailed.cost_usd == pytest.approx(0.004 + 0.0024)
        assert detailed.model_usage["gpt-5.4"]["cost_usd"] == pytest.approx(0.004)
        assert detailed.model_usage["gpt-5.4-mini"]["cost_usd"] == pytest.approx(0.0024)
        assert detailed.model_usage["gpt-5.4-mini"]["pricing_model"] == ("gpt-5.4-mini")
        # Before the reroute the requested model priced the first request.
        first = usage[0].detailed_usage
        assert first is not None
        assert first.cost_usd == pytest.approx(0.004)
        assert list(first.model_usage) == ["gpt-5.4"]

    asyncio.run(scenario())


# 6. Claude guidance the CLI answers in a turn of its own --------------------------

SESSION = "claude-session-leftovers"


def _stream_text(text: str) -> list[dict[str, Any]]:
    def event(value: dict[str, Any], suffix: str) -> dict[str, Any]:
        return {
            "type": "stream_event",
            "uuid": f"se-{text[:12]}-{suffix}",
            "session_id": SESSION,
            "parent_tool_use_id": None,
            "event": value,
        }

    return [
        event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            "start",
        ),
        event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
            "delta",
        ),
        event({"type": "content_block_stop", "index": 0}, "stop"),
    ]


def _assistant(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": f"asst-{json.dumps(blocks)[:30]}",
        "session_id": SESSION,
        "parent_tool_use_id": None,
        "message": {"model": "claude-test", "content": blocks, "usage": None},
    }


def _result(text: str | None, subtype: str = "success", **extra: Any) -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": SESSION,
        "result": text,
        "usage": {"input_tokens": 3, "output_tokens": 2},
        **extra,
    }


def _answer(text: str, *, pause: float = 0.0) -> list[Any]:
    """Stream ``text``; ``pause`` keeps the answer streaming for that long."""

    start, delta, stop = _stream_text(text)
    return [
        start,
        delta,
        *([("SLEEP", pause)] if pause else []),
        stop,
        _assistant([{"type": "text", "text": text}]),
        _result(text),
    ]


TOOL = "TOOL"


class QueueingCli(claude_agent_sdk.Transport):
    """The Claude Code CLI's stream-json contract with ``--replay-user-messages``.

    One turn runs at a time. A user message that arrives while a turn runs is
    queued; a ``TOOL`` step absorbs every queued message into the running turn
    (the CLI hands them to the model with the tool result), otherwise they run
    together as the next turn once this one's result is out. Each turn starts
    with ``system/init`` and echoes the stdin message it took with its first
    assistant message, or at once with ``replay_at_start``. An interrupt
    aborts the running turn; queued messages survive it, as in the CLI.
    """

    def __init__(
        self,
        scripts: dict[str, list[Any]],
        *,
        queued_start_delay: float = 0.3,
        replay_at_start: bool = False,
    ) -> None:
        self.scripts = scripts
        self.queued_start_delay = queued_start_delay
        self.replay_at_start = replay_at_start
        self.prompts: list[str] = []
        self.interrupts = 0
        self._inbound: list[dict[str, Any]] = []
        self._wake = asyncio.Event()
        self._interrupted = asyncio.Event()
        self._busy = False
        self._out: asyncio.Queue[Any] = asyncio.Queue()
        self._ready = False
        self._runner: asyncio.Future[None] | None = None

    async def connect(self) -> None:
        self._ready = True
        self._runner = asyncio.ensure_future(self._run())

    def _replay(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "user",
            "uuid": messages[0].get("uuid") or f"replay-{len(self.prompts)}",
            "session_id": SESSION,
            "parent_tool_use_id": None,
            "isReplay": True,
            "message": {
                "role": "user",
                "content": "\n".join(
                    str(message["message"]["content"]) for message in messages
                ),
            },
        }

    async def _run(self) -> None:
        queued_turn = False
        while True:
            while not self._inbound:
                self._wake.clear()
                await self._wake.wait()
            # The CLI takes every queued message as soon as a turn ends; its
            # next turn only reports after hooks and setup.
            batch, self._inbound = self._inbound, []
            self._busy = True
            self._interrupted.clear()
            if queued_turn and self.queued_start_delay:
                await asyncio.sleep(self.queued_start_delay)
            await self._out.put(
                {"type": "system", "subtype": "init", "session_id": SESSION}
            )
            echo: dict[str, Any] | None = self._replay(batch)
            if self.replay_at_start:
                await self._out.put(echo)
                echo = None
            text = str(batch[0]["message"]["content"])
            for step in self.scripts.get(text, [_result("")]):
                if (
                    echo is not None
                    and isinstance(step, dict)
                    and step["type"] in {"assistant", "result"}
                ):
                    await self._out.put(echo)
                    echo = None
                if isinstance(step, tuple) and step[0] == "SLEEP":
                    sleeper = asyncio.ensure_future(asyncio.sleep(step[1]))
                    stopper = asyncio.ensure_future(self._interrupted.wait())
                    await asyncio.wait(
                        {sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED
                    )
                    sleeper.cancel()
                    stopper.cancel()
                elif step == TOOL:
                    tool_result = {
                        "type": "user",
                        "uuid": f"tool-result-{len(self.prompts)}",
                        "session_id": SESSION,
                        "parent_tool_use_id": None,
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_1",
                                    "content": "done",
                                }
                            ],
                        },
                    }
                    await self._out.put(tool_result)
                    absorbed, self._inbound = self._inbound, []
                    for message in absorbed:
                        await self._out.put(self._replay([message]))
                else:
                    await self._out.put(step)
                if self._interrupted.is_set():
                    if echo is not None:
                        await self._out.put(echo)
                    await self._out.put(
                        _result(
                            None,
                            "error_during_execution",
                            terminal_reason="aborted_streaming",
                        )
                    )
                    break
            self._busy = False
            queued_turn = bool(self._inbound)

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message.get("type") == "control_request":
            await self._out.put(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": {},
                    },
                }
            )
            if message["request"].get("subtype") == "interrupt":
                self.interrupts += 1
                if self._busy:
                    self._interrupted.set()
            return
        if message.get("type") == "user":
            self.prompts.append(str(message["message"]["content"]))
            self._inbound.append(message)
            self._wake.set()

    async def read_messages(self) -> Any:
        while True:
            yield await self._out.get()

    async def close(self) -> None:
        self._ready = False
        if self._runner is not None:
            self._runner.cancel()

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        return None


async def _no_permission(_request: Any) -> Any:
    raise AssertionError("no permission request expected")


async def _claude(
    cli: QueueingCli, workspace: Path, **options: Any
) -> ClaudeAgentSdkConnection:
    client = claude_agent_sdk.ClaudeSDKClient(
        options=claude_agent_sdk.ClaudeAgentOptions(), transport=cli
    )
    await client.connect()
    return ClaudeAgentSdkConnection(
        client,
        permission_handler=_no_permission,
        sdk=claude_agent_sdk,
        external_session_id=None,
        workspace=workspace,
        **options,
    )


async def _steered_turn(
    connection: ClaudeAgentSdkConnection, prompt: str, *guidance: str
) -> list[HarnessEvent]:
    events: list[HarnessEvent] = []

    async def consume() -> None:
        async for event in connection.run_turn(prompt, model="m"):
            events.append(event)

    consumer = asyncio.ensure_future(consume())
    await _until(lambda: any(event.type == "message_delta" for event in events))
    for text in guidance:
        await connection.steer(text)
    await asyncio.wait_for(consumer, DEADLINE_SECONDS)
    return events


def test_claude_guidance_answered_in_its_own_cli_turn_stays_in_the_steered_turn(
    tmp_path: Path,
):
    async def scenario() -> None:
        cli = QueueingCli(
            {
                "Summarize the scan": _answer("Two hosts are up.", pause=0.3),
                # Queued guidance runs as one coalesced turn after the first.
                "Also list open ports": _answer("Ports 22 and 443 are open."),
                "Next question": _answer("Answer to the next question."),
            }
        )
        connection = await _claude(cli, tmp_path)

        steered = await _steered_turn(
            connection,
            "Summarize the scan",
            "Also list open ports",
            "and their services",
        )
        # The next prompt is sent as soon as the steered turn ends.
        following = await asyncio.wait_for(
            _collect(connection.run_turn("Next question", model="m")),
            DEADLINE_SECONDS,
        )

        assert steered[-1].type == "completed"
        assert steered[-1].message == (
            "Two hosts are up.\n\nPorts 22 and 443 are open."
        )
        assert following[-1].message == "Answer to the next question."
        # Replayed stdin messages are acknowledgments, not answer content.
        assert not any(
            "Unhandled Claude message block" in (event.summary or "")
            for event in [*steered, *following]
        )
        await connection.close()

    asyncio.run(scenario())


def test_claude_guidance_absorbed_at_a_tool_boundary_ends_with_that_turn(
    tmp_path: Path,
):
    async def scenario() -> None:
        tool_use = _assistant(
            [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}]
        )
        cli = QueueingCli(
            {
                "Read the notes": [
                    *_stream_text("Reading."),
                    tool_use,
                    ("SLEEP", 0.3),
                    TOOL,
                    *_answer("The notes list two hosts.")[:-1],
                    _result("The notes list two hosts.", num_turns=2),
                ],
                "Next question": _answer("Answer to the next question."),
            }
        )
        connection = await _claude(cli, tmp_path)
        loop = asyncio.get_running_loop()
        started = loop.time()

        steered = await _steered_turn(connection, "Read the notes", "Only hosts")
        elapsed = loop.time() - started
        following = await asyncio.wait_for(
            _collect(connection.run_turn("Next question", model="m")),
            DEADLINE_SECONDS,
        )

        assert steered[-1].message == "The notes list two hosts."
        # The turn ends with its own result instead of waiting for another.
        assert elapsed < 2.0
        assert following[-1].message == "Answer to the next question."
        await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "early_echo", [True, False], ids=["echo-at-turn-start", "echo-with-answer"]
)
def test_claude_stop_after_guidance_also_stops_the_guidance_turn(
    tmp_path: Path, early_echo: bool
):
    async def scenario() -> None:
        cli = QueueingCli(
            {
                "Summarize the scan": _answer("Two hosts are up.", pause=2.0),
                "Also list open ports": _answer("Ports 22 and 443 are open.", pause=2),
                "Next question": _answer("Answer to the next question."),
            },
            replay_at_start=early_echo,
        )
        # The real CLI echoes a prompt only with the turn's first assistant
        # message, so a stop before it relies on the adapter's --replay flag.
        connection = await (
            _claude(cli, tmp_path)
            if early_echo
            else _claude(cli, tmp_path, acknowledges_user_messages=True)
        )
        events: list[HarnessEvent] = []

        async def consume() -> None:
            async for event in connection.run_turn("Summarize the scan", model="m"):
                events.append(event)

        consumer = asyncio.ensure_future(consume())
        await _until(lambda: any(event.type == "message_delta" for event in events))
        await connection.steer("Also list open ports")
        # Stop: the runtime cancels the turn task and interrupts the CLI.
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer
        await asyncio.wait_for(connection.interrupt(), DEADLINE_SECONDS)

        following = await asyncio.wait_for(
            _collect(connection.run_turn("Next question", model="m")),
            DEADLINE_SECONDS,
        )

        assert following[-1].message == "Answer to the next question."
        # The CLI ran the queued guidance after the stop; it was stopped too.
        assert cli.interrupts == 2
        assert not any(
            "Ports 22" in (event.summary or "") + (event.message or "")
            for event in following
        )
        await connection.close()

    asyncio.run(scenario())


def test_claude_adapter_asks_the_cli_to_acknowledge_each_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: dict[str, Any] = {}

    class Options:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class Client:
        def __init__(self, *, options: Options) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

    sdk = SimpleNamespace(ClaudeAgentOptions=Options, ClaudeSDKClient=Client)
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))
    opened: list[Any] = []

    async def scenario() -> None:
        profile = HarnessProfile(
            id="claude-a",
            name="Claude",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            default_model="claude-test",
        )
        session = HarnessSession(
            id="session-a",
            engagement_id="eng-a",
            harness_profile_id=profile.id,
            model="claude-test",
        )
        opened.append(
            await ClaudeAgentSdkAdapter().open(
                AdapterOpenRequest(
                    profile=profile,
                    session=session,
                    workspace=tmp_path,
                    mcp_profiles=(),
                    credential_store=CredentialStore(),
                    permission_handler=_no_permission,
                )
            )
        )

    asyncio.run(scenario())
    assert captured["extra_args"] == {"replay-user-messages": None}
    # The connection then expects the echoes even before one arrives.
    assert opened[0]._cli_acknowledges is True
