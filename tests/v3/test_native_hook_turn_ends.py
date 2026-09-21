"""Turn-ending native hooks: once per end, one payload shape, never blamed on Stop."""

import asyncio
import contextlib
import json
import time

import pytest

from nebula.v3 import diagnostic_sensitive, diagnostics
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.domain import ChatTurn, ChatTurnStatus, Engagement
from nebula.v3.native_hooks import discover_native_hooks, snapshot_native_hook
from nebula.v3.providers import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    StreamEventType,
    ToolCall,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider, _profile, _write_native_hook
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response


# Echoes its event envelope and, like an operator's stop hook that reads
# ``finish_reason``, fails when a turn-ending payload does not carry it.
PAYLOAD_HOOK = (
    "#!/bin/sh\n"
    "python3 -c 'import json,sys; envelope=json.load(sys.stdin); "
    "print(json.dumps(envelope)); "
    'ending = envelope["event"] != "chat.turn.started"; '
    'raise SystemExit(1 if ending and "finish_reason" not in envelope["payload"] else 0)\'\n'
)


def _failing_hook(event_name: str, exit_code: int) -> str:
    return (
        "#!/bin/sh\n"
        "cat > /dev/null\n"
        f'if [ "$NEBULA_HOOK_EVENT" = "{event_name}" ]; then\n'
        "  echo 'rule-guard: refused the turn end, token=hunter2-secret-value' >&2\n"
        f"  exit {exit_code}\n"
        "fi\n"
    )


class BlockingProvider(FakeProvider):
    """Starts an answer and never finishes it, so the test decides the end."""

    def __init__(self, provider_id: str, *, local: bool) -> None:
        super().__init__(provider_id, local=local)
        self.started = asyncio.Event()

    async def stream(self, request: ModelRequest):
        del request
        self.started.set()
        yield ModelStreamEvent(type=StreamEventType.STARTED)
        yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="Working. ")
        await asyncio.Event().wait()
        raise AssertionError("stopped provider stream resumed unexpectedly")


class FailingProvider(FakeProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation"):
            return await super().complete(request)
        self.requests.append(request)
        raise RuntimeError("provider billing exhausted")


@pytest.fixture
def diagnostic_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic_sensitive.keyring, "get_keyring", lambda: None)
    directory = tmp_path / "diagnostics"
    directory.mkdir()
    (directory / "diagnostics-settings.json").write_text(
        json.dumps(
            {
                "schema": diagnostics.SETTINGS_SCHEMA,
                "global_level": "warning",
                "feature_levels": {},
                "sensitive_detail_capture": False,
            }
        ),
        encoding="utf-8",
    )
    manager = diagnostics.DiagnosticManager(directory, watch_settings=False)
    monkeypatch.setattr(diagnostics, "_manager", manager)
    yield manager
    manager.close()


def _chat_records(manager) -> list[dict]:
    assert manager.flush()
    log = manager.log_dir / "chat.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _service(tmp_path, provider_type, *, hook_id, events, script, **hook):
    store = NebulaStore(tmp_path / "turn-ends.db")
    workspace = tmp_path / "workspace"
    hook_dir = _write_native_hook(
        workspace, hook_id, events=events, script=script, **hook
    )
    engagement = store.create(Engagement(id="eng-turn-ends", name="Turn ends"))
    profile = store.create(_profile(local=True))
    provider = provider_type(profile.id, local=True)
    service = ChatService(
        store,
        provider_factory=lambda _: provider,
        workspace_resolver=lambda _: workspace,
    )
    request = ChatCompletionRequest(
        provider_id=profile.id,
        engagement_id=engagement.id,
        hook_ids=[hook_id],
        messages=[{"role": "user", "content": "Answer, then end the turn."}],
        include_knowledge=False,
        stream=provider_type is BlockingProvider,
    )
    return store, service, provider, request, hook_dir


async def _poll(read, *, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while not (value := read()) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return value


def test_stop_hook_failure_is_a_warning_naming_the_hook_not_the_stop(
    tmp_path, diagnostic_manager
):
    async def scenario():
        _, service, provider, request, _ = _service(
            tmp_path,
            BlockingProvider,
            hook_id="rule-guard-stop",
            events=["chat.turn.started", "chat.turn.cancelled"],
            script=_failing_hook("chat.turn.cancelled", 3),
            failure_policy="block",
        )
        prepared = await service.prepare_async(request)
        turn_id = service.start_provider_turn(prepared)
        await asyncio.wait_for(provider.started.wait(), 5)
        stopped = await service.stop_provider_turn(turn_id)
        await service.shutdown()
        return stopped, service.list_turn_hook_executions(turn_id)

    stopped, executions = asyncio.run(scenario())

    assert stopped.status == ChatTurnStatus.CANCELLED
    ended = executions[-1]
    assert (ended.event_name, ended.status, ended.exit_code) == (
        "chat.turn.cancelled",
        "failed",
        3,
    )
    records = _chat_records(diagnostic_manager)
    # A turn the operator already stopped cannot be blocked by its stop hook,
    # and the hook's own exit is not a cancellation fault.
    assert [
        item["event_code"] for item in records if item["level"] in {"ERROR", "CRITICAL"}
    ] == []
    warning = next(
        item
        for item in records
        if item["event_code"] == "chat.native_hook.terminal_hook_failed"
    )
    assert warning["level"] == "WARNING"
    assert warning["stage"] == "chat.turn.cancelled"
    assert warning["execution_id"] == ended.id
    assert warning["metadata"]["hook_id"] == "rule-guard-stop"
    assert warning["metadata"]["exit_code"] == 3
    assert "exited with code 3" in warning["operator_detail"]
    assert "rule-guard: refused the turn end" in warning["operator_detail"]
    assert "CancelledError" not in json.dumps(warning)
    assert "hunter2-secret-value" not in json.dumps(records)


def test_failed_turn_hook_failure_is_a_warning_and_keeps_the_turn_error(
    tmp_path, diagnostic_manager
):
    async def scenario():
        _, service, _, request, _ = _service(
            tmp_path,
            FailingProvider,
            hook_id="rule-guard-failed",
            events=["chat.turn.failed"],
            script=_failing_hook("chat.turn.failed", 4),
            failure_policy="block",
        )
        prepared = await service.prepare_async(request)
        with pytest.raises(RuntimeError, match="billing exhausted"):
            await service.complete(prepared)
        await service.shutdown()
        return service.list_turn_hook_executions(prepared.turn.id)

    executions = asyncio.run(scenario())

    assert [(item.event_name, item.status, item.exit_code) for item in executions] == [
        ("chat.turn.failed", "failed", 4)
    ]
    records = _chat_records(diagnostic_manager)
    assert "chat.native_hook.terminal_failure" not in {
        item["event_code"] for item in records
    }
    warning = next(
        item
        for item in records
        if item["event_code"] == "chat.native_hook.terminal_hook_failed"
    )
    assert warning["level"] == "WARNING"
    assert warning["stage"] == "chat.turn.failed"
    assert warning["execution_id"] == executions[0].id
    assert warning["metadata"] == {
        "exit_code": 4,
        "hook_id": "rule-guard-failed",
        "policy": "block",
        "status": "failed",
    }
    assert "rule-guard: refused the turn end" in warning["operator_detail"]
    assert "hunter2-secret-value" not in json.dumps(records)


@pytest.mark.parametrize(
    "provider_type,ending,finish_reason,detail",
    [
        (FakeProvider, "completed", "stop", None),
        (FailingProvider, "failed", "failed", "provider billing exhausted"),
        (BlockingProvider, "cancelled", "cancelled", "response stopped"),
    ],
)
def test_every_turn_end_sends_one_payload_shape(
    tmp_path, provider_type, ending, finish_reason, detail
):
    async def scenario():
        _, service, provider, request, _ = _service(
            tmp_path,
            provider_type,
            hook_id="stop-hook",
            events=["chat.turn.completed", "chat.turn.failed", "chat.turn.cancelled"],
            script=PAYLOAD_HOOK,
            failure_policy="block",
        )
        prepared = await service.prepare_async(request)
        turn_id = prepared.turn.id
        if provider_type is BlockingProvider:
            service.start_provider_turn(prepared)
            await asyncio.wait_for(provider.started.wait(), 5)
            await service.stop_provider_turn(turn_id)
        else:
            with contextlib.suppress(RuntimeError):
                await service.complete(prepared)
        await service.shutdown()
        return service.list_turn_hook_executions(turn_id)

    executions = asyncio.run(scenario())

    # One stop hook serves all three ends: each carries the completed keys.
    assert [(item.event_name, item.status) for item in executions] == [
        (f"chat.turn.{ending}", "complete")
    ]
    payload = json.loads(executions[0].stdout)["payload"]
    assert payload == {
        "provider_id": "provider-a",
        "model": "model-a",
        "finish_reason": finish_reason,
        "detail": detail,
    }


def test_tool_turn_runs_the_completed_hook_once(tmp_path):
    workspace = tmp_path / "workspace"
    _write_native_hook(
        workspace,
        "audit",
        events=["chat.turn.started", "chat.turn.completed"],
        script=PAYLOAD_HOOK,
        failure_policy="block",
    )
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(calls=[ToolCall(id="call-2", name="finish_response", arguments={})]),
        _response(text="Final answer."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, RecordingBroker())
    prepared.hook_snapshots = [
        snapshot_native_hook("audit", discover_native_hooks(workspace))
    ]

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Final answer."
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    executions = service.list_turn_hook_executions("turn")
    assert [(item.event_name, item.status) for item in executions] == [
        ("chat.turn.started", "complete"),
        ("chat.turn.completed", "complete"),
    ]
    assert json.loads(executions[1].stdout)["payload"] == {
        "provider_id": "provider",
        "model": "model-a",
        "finish_reason": "stop",
        "detail": None,
    }


@pytest.mark.parametrize("ended_by", ["operator_stop", "core_shutdown"])
def test_a_hook_running_when_the_turn_ends_never_stays_running(tmp_path, ended_by):
    """A stopped turn records the hook's real outcome once its process exits.

    Core shutdown keeps its restart contract instead: the outcome is unknown,
    the effectful hook waits for reconciliation, and a late exit never
    overwrites that.
    """

    async def scenario():
        store, service, provider, request, hook_dir = _service(
            tmp_path,
            BlockingProvider,
            hook_id="slow-start",
            events=["chat.turn.started", "chat.turn.cancelled"],
            script=(
                "#!/bin/sh\n"
                "cat > /dev/null\n"
                'if [ "$NEBULA_HOOK_EVENT" = "chat.turn.started" ]; then\n'
                "  while [ ! -f release ]; do sleep 0.02; done\n"
                "  echo started-hook-finished\n"
                "fi\n"
            ),
            side_effects="workspace",
        )
        prepared = await service.prepare_async(request)
        turn_id = service.start_provider_turn(prepared)

        def started_hook():
            return [
                item
                for item in service.list_turn_hook_executions(turn_id)
                if item.event_name == "chat.turn.started"
            ]

        running = await _poll(started_hook)
        assert [item.status for item in running] == ["running"]
        if ended_by == "operator_stop":
            await service.stop_provider_turn(turn_id)
        else:
            await service.shutdown()
        (hook_dir / "release").touch()
        await _poll(lambda: [item for item in started_hook() if item.exit_code == 0])
        await asyncio.sleep(0.2)
        await service.shutdown()
        return store.get(ChatTurn, turn_id), service.list_turn_hook_executions(turn_id)

    turn, executions = asyncio.run(scenario())

    started = executions[0]
    assert started.event_name == "chat.turn.started"
    if ended_by == "operator_stop":
        assert turn.status == ChatTurnStatus.CANCELLED
        assert (started.status, started.exit_code) == ("complete", 0)
        assert started.stdout.strip() == "started-hook-finished"
        assert started.completed_at is not None
        assert [(item.event_name, item.status) for item in executions[1:]] == [
            ("chat.turn.cancelled", "complete")
        ]
    else:
        assert turn.status == ChatTurnStatus.INTERRUPTED
        assert started.status == "interrupted"
        assert started.exit_code is None
        assert turn.request_snapshot["recovery"]["unknown_hook_execution_ids"] == [
            started.id
        ]
