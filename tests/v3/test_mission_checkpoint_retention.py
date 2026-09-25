"""Mission checkpoints keep only what a restart resumes from (audit MIS-8).

``AsyncSqliteSaver`` kept every superstep's checkpoint, and each one carries
the whole ``MissionState``, so a task's checkpoint storage grew with the
square of its turns (~33 MB per 100 turns of 6 KB results). These tests pin
the bounded storage, and prove restart recovery is unchanged: each crash
scenario yields the same observable mission with the stock saver and with the
pruning saver, and #571's contract (replay the journaled routing response,
never re-run an in-flight effect) holds.
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from nebula.v3.agent_tooling import BrokeredToolSpecialist, ToolMissionSupervisor
from nebula.v3.api import create_app
from nebula.v3.domain import (
    AgentRun,
    Engagement,
    RunBudget,
    RunStatus,
    ScopePolicy,
    ToolCall as LedgerCall,
)
from nebula.v3.missions import MissionService
from nebula.v3.orchestration import (
    LatestCheckpointSqliteSaver,
    MissionError,
    MissionRuntime,
    SpecialistOutcome,
    SpecialistResult,
    SpecialistRole,
    StaticSupervisor,
    call_records,
    sqlite_mission_runtime,
)
from nebula.v3.policy import PolicyEngine
from nebula.v3.providers import ModelResponse, ModelUsage, ToolCall
from nebula.v3.sandbox import AnalysisOnlyRunner
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import AnalysisTool, StoreToolLedger, ToolBroker, ToolRegistry
from tests.v3.test_missions import (
    BlockingProvider,
    _auth,
    _profile,
    _start_payload,
    _wait_for_status,
)
from tests.v3.test_specialist_tool_batches import ScriptedRoutingProvider, _spec

TOOL = "nmap.tcp"
FINISH = "nebula.finish_task"


def _rows(path, thread_id=None):
    """(checkpoints, writes) rows for one thread, or for the whole file."""

    if not path.exists():
        return (0, 0)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        where, parameters = (
            ("WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        )
        return tuple(
            connection.execute(
                f"SELECT count(*) FROM {table} {where}", parameters
            ).fetchone()[0]
            for table in ("checkpoints", "writes")
        )
    finally:
        connection.close()


def _without_ids(text, identifier):
    """Traces of two runs compare equal once their random ids are dropped."""

    return (text or "").replace(str(identifier), "<id>")


def _failure_category(record):
    result = record.get("provider_result")
    return result.get("category") if isinstance(result, dict) else None


def _checkpoint(sequence, values):
    return {
        "v": 4,
        "id": f"1f0a0000-0000-6000-8000-{sequence:012d}",
        "ts": "2026-09-25T00:00:00+00:00",
        "channel_values": values,
        "channel_versions": {name: f"{sequence:032}.0" for name in values},
        "versions_seen": {},
        "updated_channels": sorted(values),
    }


def _config(thread_id, checkpoint_id=None):
    configurable = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


class _TurnSpecialist:
    """Continue with a ~6 KB observation per turn, then finish."""

    allowed_tools = frozenset()

    def __init__(self, path, turns):
        self.path = path
        self.turns = turns
        self.checkpoints_seen = []
        self.prior_turns_seen = []

    async def run(self, context):
        # A real turn awaits its model call; the saver commits meanwhile.
        await asyncio.sleep(0)
        self.checkpoints_seen.append(_rows(self.path, context.run_id)[0])
        self.prior_turns_seen.append(len(context.prior_turns))
        if context.turn_index > self.turns:
            return SpecialistResult(summary="done", outcome=SpecialistOutcome.COMPLETE)
        return SpecialistResult(
            summary=f"turn {context.turn_index}",
            outcome=SpecialistOutcome.CONTINUE,
            output={"status": "complete", "provider_result": "x" * 6_000},
            tool_calls=1,
        )


def test_a_long_task_keeps_one_checkpoint_and_none_once_it_finishes(tmp_path):
    """MIS-8: each superstep supersedes the previous checkpoint, so a running
    task keeps one checkpoint however many turns it takes, and a finished run
    keeps none. Before, every superstep's copy of the growing state was kept,
    during the run and after it."""

    async def scenario():
        store = NebulaStore(tmp_path / "nebula.db")
        engagement = store.create(Engagement(name="Long task"))
        path = tmp_path / "mission-checkpoints.db"
        specialist = _TurnSpecialist(path, turns=30)
        async with sqlite_mission_runtime(
            checkpoint_path=path,
            store=store,
            supervisor=StaticSupervisor(),
            specialists={SpecialistRole.SCOPE_PLANNING: specialist},
        ) as runtime:
            state = await runtime.start(
                engagement_id=engagement.id,
                objective="Keep investigating",
                budget=RunBudget(max_retries=0),
            )
        return state, specialist, path

    state, specialist, path = asyncio.run(scenario())

    assert state["final_summary"]
    # The whole history still reaches every turn.
    assert specialist.prior_turns_seen == list(range(31))
    assert max(specialist.checkpoints_seen) == 1
    assert _rows(path) == (0, 0)


def test_the_pruning_saver_stores_the_row_the_stock_saver_stores(tmp_path):
    """The override changes which rows are kept, never the row itself."""

    async def put(saver_cls, path):
        async with saver_cls.from_conn_string(str(path)) as saver:
            parent = _checkpoint(1, {"task_history": {"scan": [{"summary": "one"}]}})
            child = _checkpoint(2, {"task_history": {"scan": [{"summary": "two"}]}})
            await saver.aput(
                _config("run-1"), parent, {"source": "input", "step": -1}, {}
            )
            saved = await saver.aput(
                _config("run-1", parent["id"]),
                child,
                {"source": "loop", "step": 0, "parents": {}},
                {},
            )
            latest = await saver.aget_tuple(_config("run-1"))
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (child["id"],)
            ).fetchone()
        finally:
            connection.close()
        return saved, latest, row

    stock = asyncio.run(put(AsyncSqliteSaver, tmp_path / "stock.db"))
    pruning = asyncio.run(put(LatestCheckpointSqliteSaver, tmp_path / "pruning.db"))

    assert pruning[0] == stock[0]
    assert pruning[1] == stock[1]
    assert pruning[2] == stock[2]


def test_pruning_keeps_the_latest_checkpoint_and_its_pending_writes(tmp_path):
    """Only rows older than the committed checkpoint are removed: the
    checkpoint and pending writes a restart loads are exactly the stock
    saver's, other threads are untouched, and a write that lands late for a
    superseded checkpoint is removed by the next commit."""

    async def sequence(saver_cls, path):
        async with saver_cls.from_conn_string(str(path)) as saver:
            first = _checkpoint(1, {"task_status": {"scan": "pending"}})
            second = _checkpoint(2, {"task_status": {"scan": "running"}})
            third = _checkpoint(3, {"task_status": {"scan": "complete"}})
            other = _checkpoint(4, {"task_status": {"other": "pending"}})
            loop = {"source": "loop", "step": 0, "parents": {}}
            await saver.aput(_config("run-a"), first, loop, {})
            await saver.aput_writes(
                _config("run-a", first["id"]), [("task_status", {"scan": "x"})], "t1"
            )
            await saver.aput(_config("run-a", first["id"]), second, loop, {})
            await saver.aput_writes(
                _config("run-a", second["id"]),
                [("task_history", {"scan": [{"summary": "pending"}]})],
                "t2",
            )
            await saver.aput(_config("run-b"), other, loop, {})
            interrupted = await saver.aget_tuple(_config("run-a"))
            rows_after_second = _rows(path, "run-a")
            # A task's write for the superseded checkpoint arrives late.
            await saver.aput_writes(
                _config("run-a", first["id"]), [("errors", {"scan": "late"})], "t9"
            )
            await saver.aput(_config("run-a", second["id"]), third, loop, {})
            latest = await saver.aget_tuple(_config("run-a"))
        return (
            interrupted,
            rows_after_second,
            latest,
            _rows(path, "run-a"),
            _rows(path, "run-b"),
        )

    stock = asyncio.run(sequence(AsyncSqliteSaver, tmp_path / "stock.db"))
    pruning = asyncio.run(
        sequence(LatestCheckpointSqliteSaver, tmp_path / "pruning.db")
    )

    # What a restart loads is identical, including the pending writes.
    assert pruning[0] == stock[0]
    assert pruning[0].pending_writes == [
        ("t2", "task_history", {"scan": [{"summary": "pending"}]})
    ]
    assert pruning[2] == stock[2]
    assert stock[1] == (2, 2) and pruning[1] == (1, 1)
    assert stock[3] == (3, 3) and pruning[3] == (1, 0)
    assert pruning[4] == stock[4] == (1, 0)


# -- Restart equivalence ---------------------------------------------------


class _TurnScriptedProvider(ScriptedRoutingProvider):
    """Answer each turn like a real provider: the same action, fresh call ids."""

    SCAN_80 = (TOOL, {"ports": [80]})
    SCAN_443 = (TOOL, {"ports": [443]})
    FINISHED = (
        FINISH,
        {"status": "complete", "summary": "Mapped the service", "rationale": "Scanned"},
    )

    def __init__(self, script, hang_on_turn=None):
        super().__init__([])
        self.script = script
        self.hang_on_turn = hang_on_turn
        self.hanging = asyncio.Event()

    async def complete(self, request):
        self.requests.append(request)
        turn = request.metadata["agent_turn"]
        if turn == self.hang_on_turn:
            # Core stops while the routing request is outstanding.
            self.hang_on_turn = None
            self.hanging.set()
            await asyncio.Event().wait()
        name, arguments = self.script[turn]
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text="",
            tool_calls=[
                ToolCall(id=f"call_{uuid4().hex[:12]}", name=name, arguments=arguments)
            ],
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason="tool_calls",
        )


class _ScanEffects:
    """The tool's side effect: every start is recorded; one can hang."""

    def __init__(self, hang_on_ports=None):
        self.started = []
        self.hang_on_ports = hang_on_ports
        self.hanging = asyncio.Event()

    async def __call__(self, arguments):
        self.started.append(arguments.get("ports"))
        if arguments.get("ports") == self.hang_on_ports:
            # Core stops while the effect is running.
            self.hang_on_ports = None
            self.hanging.set()
            await asyncio.Event().wait()
        return {"open_ports": arguments.get("ports")}


class _GatedSpecialist(BrokeredToolSpecialist):
    """Record every turn's context; optionally stop before a turn starts."""

    def __init__(self, *args, contexts, pause_before_turn=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.contexts = contexts
        self.pause_before_turn = pause_before_turn
        self.paused = asyncio.Event()

    async def run(self, context):
        self.contexts.append(context)
        if context.turn_index == self.pause_before_turn:
            # Core stops after the previous superstep committed, before this
            # turn asks the model anything.
            self.pause_before_turn = None
            self.paused.set()
            await asyncio.Event().wait()
        return await super().run(context)


@asynccontextmanager
async def _runtime(saver_cls, path, store, specialist):
    supervisor = ToolMissionSupervisor({TOOL: _spec(TOOL)})
    specialists = {SpecialistRole.NETWORK_SERVICE: specialist}
    if saver_cls is LatestCheckpointSqliteSaver:
        # The production factory.
        async with sqlite_mission_runtime(
            checkpoint_path=path,
            store=store,
            supervisor=supervisor,
            specialists=specialists,
        ) as runtime:
            yield runtime
        return
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield MissionRuntime(
            store=store,
            checkpointer=saver,
            supervisor=supervisor,
            specialists=specialists,
        )


async def _crash_and_recover(tmp_path, saver_cls, crash):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Restart"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "mission-checkpoints.db"
    script = {
        "1": _TurnScriptedProvider.SCAN_80,
        "2": _TurnScriptedProvider.SCAN_443,
        "3": _TurnScriptedProvider.FINISHED,
    }
    if crash == "tool_effect":
        # A task cannot complete on an unknown result, so the model makes a
        # different, explicit call before finishing.
        script.update({"3": (TOOL, {"ports": [8080]}), "4": script["3"]})
    provider = _TurnScriptedProvider(
        script, hang_on_turn="2" if crash == "routing_call" else None
    )
    effects = _ScanEffects(hang_on_ports=[443] if crash == "tool_effect" else None)
    contexts = []

    def specialist(pause_before_turn=None):
        # A Core restart builds a fresh broker and specialist over the same
        # durable store; only the provider and the effect are shared.
        registry = ToolRegistry()
        registry.register(AnalysisTool(_spec(TOOL), effects))
        broker = ToolBroker(
            registry=registry,
            policy_engine=PolicyEngine(),
            runner=AnalysisOnlyRunner(),
            ledger=StoreToolLedger(store),
            workspace_resolver=lambda _engagement_id: workspace,
        )
        return _GatedSpecialist(
            provider,
            role=SpecialistRole.NETWORK_SERVICE,
            broker=broker,
            scope=ScopePolicy(id="scope", engagement_id=engagement.id),
            workspace=workspace,
            specs={TOOL: _spec(TOOL)},
            model="model-a",
            store=store,
            contexts=contexts,
            pause_before_turn=pause_before_turn,
        )

    run_id = str(uuid4())
    first = specialist(pause_before_turn=2 if crash == "between_supersteps" else None)

    async def first_worker():
        async with _runtime(saver_cls, path, store, first) as runtime:
            try:
                await runtime.start(
                    engagement_id=engagement.id,
                    objective="Map the service",
                    budget=RunBudget(
                        max_concurrency=1, max_delegation_depth=1, max_tool_calls=10
                    ),
                    run_id=run_id,
                    context={"tool_names": [TOOL]},
                )
            except asyncio.CancelledError as exc:
                await MissionService._await_graph_cleanup(exc)
                raise

    worker = asyncio.create_task(first_worker())
    if crash == "after_completion":
        await asyncio.wait_for(worker, 30)
    else:
        stopped = {
            "between_supersteps": first.paused,
            "routing_call": provider.hanging,
            "tool_effect": effects.hanging,
        }[crash]
        await asyncio.wait_for(stopped.wait(), 30)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    checkpoints_at_restart = _rows(path, run_id)[0]

    recovered = None
    async with _runtime(saver_cls, path, store, specialist()) as runtime:
        try:
            recovered = await asyncio.wait_for(runtime.recover(run_id), 30)
        except MissionError as exc:
            recovered = str(exc)

    last = contexts[-1]
    return {
        "checkpoints_at_restart": checkpoints_at_restart,
        "recovered": (
            _without_ids(recovered, run_id) if isinstance(recovered, str) else "resumed"
        ),
        "routing_turns": [
            request.metadata["agent_turn"] for request in provider.requests
        ],
        "effects_started": list(effects.started),
        "ledger": sorted(
            (_without_ids(call.idempotency_key, call.task_id), call.status.value)
            for call in store.iter_readable_entities(LedgerCall)
            if call.run_id == run_id
        ),
        "turns": [
            (context.turn_index, len(context.prior_turns)) for context in contexts
        ],
        "last_turn_observations": [
            (record.get("status"), _failure_category(record))
            for turn in last.prior_turns
            for record in call_records(turn.output)
        ],
        "run_status": store.get(AgentRun, run_id).status,
        "rows_after": _rows(path, run_id),
    }


_EXPECTED = {
    # Resumes at the committed turn-1 checkpoint; turn 2 asks the model once.
    "between_supersteps": {
        "routing_turns": ["1", "2", "3"],
        "effects_started": [[80], [443]],
        "ledger_statuses": ["complete", "complete"],
    },
    # No routing response was journaled, so turn 2 asks the model again; no
    # effect had started.
    "routing_call": {
        "routing_turns": ["1", "2", "2", "3"],
        "effects_started": [[80], [443]],
        "ledger_statuses": ["complete", "complete"],
    },
    # The journaled turn-2 response is replayed (the model is not asked
    # again) and the in-flight scan is refused as an unknown effect, which the
    # next turn reads; the model then chooses a different call.
    "tool_effect": {
        "routing_turns": ["1", "2", "3", "4"],
        "effects_started": [[80], [443], [8080]],
        "ledger_statuses": ["complete", "cancelled", "complete"],
    },
    # Nothing is resumed or executed again.
    "after_completion": {
        "routing_turns": ["1", "2", "3"],
        "effects_started": [[80], [443]],
        "ledger_statuses": ["complete", "complete"],
    },
}


@pytest.mark.parametrize(
    "crash", ["between_supersteps", "routing_call", "tool_effect", "after_completion"]
)
def test_restart_recovery_is_unchanged_by_checkpoint_pruning(tmp_path, crash):
    """MIS-8: a Core stop at each point of a turn recovers the same mission
    with the stock saver (every checkpoint kept) and the pruning saver, and
    #571's contract holds: one execution per effect, a journaled turn is never
    re-asked, an in-flight effect is never run again."""

    stock = asyncio.run(_crash_and_recover(tmp_path / "stock", AsyncSqliteSaver, crash))
    pruning = asyncio.run(
        _crash_and_recover(tmp_path / "pruning", LatestCheckpointSqliteSaver, crash)
    )

    stock_checkpoints = stock.pop("checkpoints_at_restart")
    pruning_checkpoints = pruning.pop("checkpoints_at_restart")
    assert pruning == stock

    expected = _EXPECTED[crash]
    assert pruning["routing_turns"] == expected["routing_turns"]
    assert pruning["effects_started"] == expected["effects_started"]
    assert [status for _key, status in pruning["ledger"]] == expected["ledger_statuses"]
    # One ledger slot per turn position, whatever ids the provider made up.
    assert [key for key, _status in pruning["ledger"]] == [
        f"task:<id>:turn:{turn}:call:0" for turn in range(1, len(pruning["ledger"]) + 1)
    ]
    assert pruning["run_status"] == RunStatus.COMPLETE
    assert pruning["rows_after"] == (0, 0)
    if crash == "after_completion":
        assert stock_checkpoints == pruning_checkpoints == 0
        assert "no checkpoint" in pruning["recovered"]
    else:
        assert pruning["recovered"] == "resumed"
        assert pruning_checkpoints == 1
        assert stock_checkpoints > pruning_checkpoints
        # The finishing turn reads every earlier turn's observations.
        assert pruning["turns"][-1] == (
            len(expected["routing_turns"]) - (crash == "routing_call"),
            len(expected["effects_started"]),
        )
    if crash == "tool_effect":
        assert pruning["last_turn_observations"] == [
            ("complete", None),
            ("failed", "outcome_unknown"),
            ("complete", None),
        ]


# -- Service cleanup -------------------------------------------------------


def _leave_checkpoints(path, thread_ids):
    """Rows a Core before this fix left behind: two checkpoints per thread."""

    async def write():
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            for index, thread_id in enumerate(thread_ids):
                first = _checkpoint(index * 2 + 1, {"task_status": {"scan": "pending"}})
                second = _checkpoint(
                    index * 2 + 2, {"task_status": {"scan": "running"}}
                )
                loop = {"source": "loop", "step": 0, "parents": {}}
                await saver.aput(_config(thread_id), first, loop, {})
                await saver.aput(_config(thread_id, first["id"]), second, loop, {})

    asyncio.run(write())


def test_startup_discards_checkpoints_only_of_finished_runs(tmp_path):
    """A finished run's leftover checkpoints are removed at startup; a run
    that can still continue (interrupted, waiting) or has no readable row
    keeps its checkpoint, and old rows stay readable."""

    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Sweep"))

    def run(status, origin="api"):
        return store.create(
            AgentRun(
                engagement_id=engagement.id,
                objective="sweep",
                status=status,
                metadata={"origin": origin},
            )
        )

    finished = [
        run(RunStatus.COMPLETE),
        run(RunStatus.FAILED),
        run(RunStatus.CANCELLED),
    ]
    # Not API-owned, so startup leaves them for their own owner to continue.
    resumable = [
        run(RunStatus.INTERRUPTED, origin="cli"),
        run(RunStatus.WAITING_APPROVAL, origin="cli"),
    ]
    path = tmp_path / "mission-checkpoints.db"
    _leave_checkpoints(path, [item.id for item in finished + resumable] + ["no-run"])

    async def scenario():
        service = MissionService(store, checkpoint_path=path)
        await service.startup()
        await service.shutdown()
        async with LatestCheckpointSqliteSaver.from_conn_string(str(path)) as saver:
            return {
                thread_id: await saver.aget_tuple(_config(thread_id))
                for thread_id in [item.id for item in resumable] + ["no-run"]
            }

    kept = asyncio.run(scenario())

    for item in finished:
        assert _rows(path, item.id) == (0, 0)
    for item in resumable:
        assert _rows(path, item.id) == (2, 0)
    assert _rows(path, "no-run") == (2, 0)
    assert all(
        latest is not None
        and latest.checkpoint["channel_values"] == {"task_status": {"scan": "running"}}
        for latest in kept.values()
    )


def test_a_stopped_mission_leaves_no_checkpoint(tmp_path):
    """Stopping a running Mission, or a dormant waiting one, removes its
    checkpoints once the run is cancelled."""

    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Stop"))
    profile = _profile(store)
    provider = BlockingProvider(profile)
    path = tmp_path / "mission-checkpoints.db"
    service = MissionService(
        store,
        checkpoint_path=path,
        provider_factory=lambda selected: provider,
        cancellation_timeout_seconds=2,
    )
    app = create_app(store, auth_token="test-token", mission_service=service)

    with TestClient(app) as client:
        started = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )
        assert started.status_code == 202
        run_id = started.json()["id"]
        assert provider.started.wait(timeout=5)
        assert _rows(path, run_id)[0] == 1
        # A run parked on an operator decision has no worker; its checkpoint
        # is all that is left of it.
        waiting = store.create(
            AgentRun(
                engagement_id=engagement.id,
                objective="waiting for an operator",
                status=RunStatus.WAITING_APPROVAL,
                supervisor_provider_id=profile.id,
                supervisor_model="security-model",
                metadata={"origin": "api", "waiting_approval": True},
            )
        )
        _leave_checkpoints(path, [waiting.id])

        for target in (run_id, waiting.id):
            stopped = client.post(
                f"/api/v1/runs/{target}/stop",
                headers=_auth(),
                json={"reason": "Operator ended the review"},
            )
            assert stopped.status_code == 200
            _wait_for_status(client, target, "cancelled")

    assert _rows(path, run_id) == (0, 0)
    assert _rows(path, waiting.id) == (0, 0)
    assert store.get(AgentRun, waiting.id).status == RunStatus.CANCELLED
