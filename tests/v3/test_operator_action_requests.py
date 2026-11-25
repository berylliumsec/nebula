"""Operator action requests: typed "Needs you" records Butler opens and closes."""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from nebula.v3.butler_board import butler_board_router
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.domain import (
    ButlerIncident,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    EngagementStatus,
    ProviderProfile,
    WorkItem,
    utc_now,
)
from nebula.v3.operator_action_requests import (
    CANCEL_SUMMARY_PREFIX,
    CLOSE_TOOL,
    CREATE_TOOL,
    GOAL_WAIT_MARKER,
    LIST_TOOL,
    ButlerOperatorActions,
    CheckResult,
    CheckSpec,
    DefaultCheckEvaluator,
    IncidentNotifier,
    OperatorActionRecord,
    close_request,
    create_request,
    goal_wait_reason,
    list_requests,
    parse_record,
    probe_outcome,
    render_description,
    sweep_operator_actions,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.work import WorkCheckIn, WorkCreate, WorkService
from tests.v3.async_test_support import AsyncTestClient

pytestmark = pytest.mark.anyio


class FakeWork:
    """ControlAssistantWork's goal action without the HTTP hop."""

    def __init__(self, store: NebulaStore) -> None:
        self.goals = ChatGoalService(store)
        self.calls: list[dict[str, Any]] = []

    async def goal_action(self, project_id: str, arguments: dict[str, Any]):
        self.calls.append(arguments)
        await self.goals.write(
            arguments["session_id"],
            GoalWrite.model_validate(
                {k: v for k, v in arguments.items() if k != "session_id"}
            ),
        )


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def opened(self, request_id: str, record: OperatorActionRecord) -> None:
        self.events.append(("opened", request_id, record.status))

    async def closed(self, request_id: str, record: OperatorActionRecord) -> None:
        self.events.append(("closed", request_id, record.status))


class FixedEvaluator:
    """Answers every check with ``passed``."""

    def __init__(self, passed: bool | None) -> None:
        self.passed = passed
        self.calls = 0

    async def evaluate(self, check, record, *, now):
        self.calls += 1
        return CheckResult(passed=self.passed, detail="fixed", checked_at=now)


async def _project(store: NebulaStore, name: str = "Research"):
    project = await store.create(Engagement(name=name))
    profile = await store.create(
        ProviderProfile(
            name=f"Local {name}",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    return project, profile


async def _goal(store, project, profile, *, status=ChatGoalStatus.RUNNING, reason=None):
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Lab goal chat",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    now = utc_now()
    return await store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=session.id,
            objective="Finish the lab run",
            completion_criteria=["Done"],
            status=status,
            started_at=now,
            active_since=now if status == ChatGoalStatus.RUNNING else None,
            blocked_reason=reason,
        )
    )


def _arguments(project_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "action": "restart_host",
        "device_or_host": "lab-host-1",
        "reason": "The host stopped answering and remote restart failed.",
        "project_id": project_id,
        "evidence": ["ssh timed out three times"],
        "verification": ["Remote power control refused the restart"],
        "urgency": "high",
        "check": {"kind": "time_elapsed", "params": {"seconds": 3600}},
        **overrides,
    }


# Record validation ---------------------------------------------------------


def test_record_validation_and_round_trip():
    base = {
        "action": "restart_host",
        "device_or_host": "lab-host-1",
        "reason": "Hung",
        "project_id": "p1",
        "verification": ["Tried remote restart"],
    }
    with pytest.raises(ValidationError, match="needs an auto-check"):
        OperatorActionRecord.model_validate(base)
    with pytest.raises(ValidationError):
        OperatorActionRecord.model_validate({**base, "verification": []})
    with pytest.raises(ValidationError, match="unknown check kind"):
        CheckSpec(kind="ping_everything")
    with pytest.raises(ValidationError, match="needs a path"):
        CheckSpec(kind="path_exists")
    with pytest.raises(ValidationError, match="positive whole seconds"):
        CheckSpec(kind="time_elapsed", params={"seconds": 0})
    with pytest.raises(ValidationError, match="stage or a status"):
        CheckSpec(kind="work_item_stage", params={"project_id": "p", "item_id": "i"})
    with pytest.raises(ValidationError):
        OperatorActionRecord.model_validate(
            {**base, "action": "other", "status": "closed"}
        )
    # A non-host action may omit the check.
    record = OperatorActionRecord.model_validate(
        {**base, "action": "attach_credential", "urgency": "blocking"}
    )
    assert record.check is None
    host = OperatorActionRecord.model_validate(
        {**base, "check": {"kind": "tcp_reachable", "params": {"port": 22}}}
    )
    description = render_description(host)
    assert description.startswith("Hung")
    assert parse_record(description) == host
    assert parse_record("Plain text with no block") is None
    assert parse_record("```nebula-operator-action\n{broken\n```") is None


# Creation ------------------------------------------------------------------


async def test_creation_requires_verification(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, _ = await _project(store)
    for missing in ({"verification": []}, {"verification": None}):
        with pytest.raises(ValueError, match="verify first"):
            await create_request(store, _arguments(project.id, **missing))
    assert await store.find_entities(WorkItem, {}) == []


async def test_creation_refuses_when_check_already_passes(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, _ = await _project(store)
    present = tmp_path / "token.txt"
    present.write_text("x")
    result = await create_request(
        store,
        _arguments(
            project.id,
            action="attach_credential",
            check={"kind": "path_exists", "params": {"path": str(present)}},
        ),
    )
    assert result["created"] is False
    assert result["refusal"] == "check_already_passes"
    assert await store.find_entities(WorkItem, {}) == []


async def test_creation_saves_needs_you_item_notifies_and_dedupes(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(store, project, profile)
    notifier = RecordingNotifier()
    evaluator = FixedEvaluator(False)
    created = await create_request(
        store,
        _arguments(project.id, goal_session_id=goal.session_id),
        evaluator=evaluator,
        notifier=notifier,
    )
    assert created["created"] is True and created["notified"] is True
    assert evaluator.calls == 1
    request_id = created["request"]["id"]
    item = await store.get(WorkItem, request_id)
    assert item.title == "Needs you: Restart host on lab-host-1"
    assert item.priority == "high" and item.status == "ready"
    assert item.source_kind == "chat" and item.source_id == goal.session_id
    # Not assigned to the goal chat, so goal hygiene never treats it as the
    # goal's own Work item.
    assert item.assignee_session_id is None
    record = parse_record(item.description)
    assert record is not None and record.last_check is not None
    assert record.last_check.passed is False
    assert notifier.events == [("opened", request_id, "open")]
    assert f"{GOAL_WAIT_MARKER}{request_id}" in created["goal_wait_reason"]

    # Same action and device (any spelling): the open request comes back.
    duplicate = await create_request(
        store,
        _arguments(
            project.id,
            goal_session_id=goal.session_id,
            device_or_host="  LAB-host-1 ",
        ),
        evaluator=evaluator,
        notifier=notifier,
    )
    assert duplicate["created"] is False and duplicate["refusal"] == "duplicate"
    assert duplicate["request"]["id"] == request_id
    # Without a goal it still matches the open request for that device.
    again = await create_request(store, _arguments(project.id), evaluator=evaluator)
    assert again["refusal"] == "duplicate"
    # Another goal waiting on the same device gets its own request.
    other = await _goal(store, project, profile)
    separate = await create_request(
        store,
        _arguments(project.id, goal_session_id=other.session_id),
        evaluator=evaluator,
    )
    assert separate["created"] is True
    # A different action on the device is a different request.
    power = await create_request(
        store, _arguments(project.id, action="power_cycle_device"), evaluator=evaluator
    )
    assert power["created"] is True
    assert len(notifier.events) == 1


async def test_goal_project_mismatch_and_missing_project(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    elsewhere, _ = await _project(store, "Elsewhere")
    goal = await _goal(store, project, profile)
    with pytest.raises(ValueError, match="another project"):
        await create_request(
            store, _arguments(elsewhere.id, goal_session_id=goal.session_id)
        )
    with pytest.raises(ValueError, match="name the project"):
        await create_request(store, _arguments(""))


# Auto-close and resume -------------------------------------------------------


async def test_sweep_auto_closes_and_resumes_blocked_goal(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(store, project, profile)
    work = FakeWork(store)
    notifier = RecordingNotifier()
    marker = tmp_path / "host-back"
    created = await create_request(
        store,
        _arguments(
            project.id,
            goal_session_id=goal.session_id,
            check={"kind": "path_exists", "params": {"path": str(marker)}},
            block_goal=True,
        ),
        work=work,
        notifier=notifier,
    )
    request_id = created["request"]["id"]
    assert created["goal_block"] == "blocked"
    blocked = await store.get(ChatGoal, goal.id)
    assert blocked.status == ChatGoalStatus.BLOCKED
    assert f"{GOAL_WAIT_MARKER}{request_id}" in (blocked.blocked_reason or "")

    # Not yet: the sweep records the check and leaves the request open.
    first = await sweep_operator_actions(store, work, notifier=notifier)
    assert first["counts"] == {"check:waiting": 1}
    assert (await store.get(WorkItem, request_id)).status == "ready"

    marker.write_text("up")
    second = await sweep_operator_actions(store, work, notifier=notifier)
    assert second["counts"] == {"auto_close:applied": 1}
    assert second["actions"][0]["resume"] == "resumed"
    item = await store.get(WorkItem, request_id)
    assert item.status == "done"
    record = parse_record(item.description)
    assert record is not None
    assert record.status == "closed" and record.closed_by == "auto-check"
    assert record.closed_at is not None and record.resumed_at is not None
    assert record.closing_evidence[0].startswith("Auto-check passed")
    assert record.resume_outcome == "resumed"
    resumed = await store.get(ChatGoal, goal.id)
    assert resumed.status == ChatGoalStatus.RUNNING
    assert [call["action"] for call in work.calls] == ["block", "resume"]
    assert notifier.events[-1] == ("closed", request_id, "closed")

    # Idempotent: nothing more to do.
    third = await sweep_operator_actions(store, work, notifier=notifier)
    assert third["counts"] == {} and third["errors"] == []
    assert len(work.calls) == 2


async def test_sweep_leaves_unrelated_paused_goal_alone(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(
        store, project, profile, status=ChatGoalStatus.BLOCKED, reason="Needs review"
    )
    work = FakeWork(store)
    created = await create_request(
        store,
        _arguments(project.id, goal_session_id=goal.session_id),
        evaluator=FixedEvaluator(False),
    )
    result = await sweep_operator_actions(store, work, evaluator=FixedEvaluator(True))
    assert result["actions"][0]["resume"] == "goal_not_waiting_on_request"
    assert (await store.get(ChatGoal, goal.id)).status == ChatGoalStatus.BLOCKED
    assert work.calls == []
    record = parse_record(
        (await store.get(WorkItem, created["request"]["id"])).description
    )
    assert record is not None and record.status == "closed"


async def test_operator_done_and_cancel_from_work_check_in(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    work = FakeWork(store)
    notifier = RecordingNotifier()
    done_goal = await _goal(store, project, profile)
    cancel_goal = await _goal(store, project, profile)
    done = await create_request(
        store,
        _arguments(project.id, goal_session_id=done_goal.session_id),
        evaluator=FixedEvaluator(False),
        work=work,
    )
    cancel = await create_request(
        store,
        _arguments(project.id, goal_session_id=cancel_goal.session_id),
        evaluator=FixedEvaluator(False),
        work=work,
    )
    for goal, created in ((done_goal, done), (cancel_goal, cancel)):
        await work.goal_action(
            project.id,
            {
                "session_id": goal.session_id,
                "expected_revision": goal.revision,
                "action": "block",
                "reason": created["goal_wait_reason"],
            },
        )
    service = WorkService(store)
    # The board's Done and Cancel buttons post these Work check-ins.
    await service.check_in(
        project.id,
        done["request"]["id"],
        WorkCheckIn(summary="Operator marked this request done.", status="done"),
        actor_kind="operator",
        actor_id="operator",
    )
    await service.check_in(
        project.id,
        cancel["request"]["id"],
        WorkCheckIn(summary=f"{CANCEL_SUMMARY_PREFIX}.", status="done"),
        actor_kind="operator",
        actor_id="operator",
    )
    # A sweep without Butler's Work gateway finishes the records but cannot
    # resume; the next sweep with it does.
    first = await sweep_operator_actions(
        store, None, evaluator=FixedEvaluator(False), notifier=notifier
    )
    assert first["counts"] == {"finish:closed": 1, "finish:cancelled": 1}
    assert (await store.get(ChatGoal, done_goal.id)).status == ChatGoalStatus.BLOCKED
    second = await sweep_operator_actions(
        store, work, evaluator=FixedEvaluator(False), notifier=notifier
    )
    assert second["counts"] == {"resume:resumed": 1}
    assert (await store.get(ChatGoal, done_goal.id)).status == ChatGoalStatus.RUNNING
    # A cancelled request never resumes its goal.
    assert (await store.get(ChatGoal, cancel_goal.id)).status == ChatGoalStatus.BLOCKED
    cancelled = parse_record(
        (await store.get(WorkItem, cancel["request"]["id"])).description
    )
    assert cancelled is not None and cancelled.status == "cancelled"
    assert sorted(event[2] for event in notifier.events) == ["cancelled", "closed"]
    assert await list_requests(store) == []


async def test_butler_close_is_idempotent(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, _ = await _project(store)
    created = await create_request(
        store, _arguments(project.id), evaluator=FixedEvaluator(False)
    )
    request_id = created["request"]["id"]
    closed = await close_request(
        store,
        project.id,
        request_id,
        outcome="cancelled",
        evidence=["The host came back on its own"],
        closed_by="butler",
    )
    assert closed["closed"] is True and closed["status"] == "cancelled"
    again = await close_request(
        store,
        project.id,
        request_id,
        outcome="closed",
        evidence=["again"],
        closed_by="butler",
    )
    assert again["closed"] is False and again["already"] == "cancelled"


# Checks --------------------------------------------------------------------


async def test_native_and_probe_checks(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, _ = await _project(store)
    item = await WorkService(store).create(
        project.id, WorkCreate(title="Wire the lab"), actor_id="operator"
    )
    record = OperatorActionRecord(
        action="other",
        device_or_host="bench",
        reason="r",
        project_id=project.id,
        verification=["v"],
        created_at=utc_now() - timedelta(seconds=120),
    )
    now = utc_now()
    probes: list[tuple[str, dict]] = []

    async def probe(kind, params):
        probes.append((kind, params))
        return {"kind": kind, "reachable": params.get("port") == 22}

    evaluator = DefaultCheckEvaluator(store, probe_runner=probe)
    elapsed = await evaluator.evaluate(
        CheckSpec(kind="time_elapsed", params={"seconds": 60}), record, now=now
    )
    assert elapsed.passed is True
    stage = CheckSpec(
        kind="work_item_stage",
        params={"project_id": project.id, "item_id": item.id, "status": "done"},
    )
    assert (await evaluator.evaluate(stage, record, now=now)).passed is False
    missing = CheckSpec(
        kind="work_item_stage",
        params={"project_id": project.id, "item_id": "missing", "status": "done"},
    )
    assert (await evaluator.evaluate(missing, record, now=now)).passed is None
    gone = CheckSpec(
        kind="path_exists", params={"path": str(tmp_path / "x"), "exists": False}
    )
    assert (await evaluator.evaluate(gone, record, now=now)).passed is True
    up = await evaluator.evaluate(
        CheckSpec(
            kind="tcp_reachable",
            params={"target": "bench", "port": 22, "expect": True},
        ),
        record,
        now=now,
    )
    assert (up.passed, up.detail) == (True, "reachable")
    # Condition keys stay out of the probe's arguments.
    assert probes == [("tcp_reachable", {"target": "bench", "port": 22})]

    def broken(kind, params):
        raise RuntimeError("probe crashed")

    failed = await DefaultCheckEvaluator(store, probe_runner=broken).evaluate(
        CheckSpec(kind="disk_free", params={"path": "/", "min_free_bytes": 1}),
        record,
        now=now,
    )
    assert failed.passed is None and "RuntimeError" in failed.detail


def test_probe_outcomes_read_host_probe_results():
    assert probe_outcome("tcp_reachable", {}, {"reachable": None}) == (
        None,
        "reachability unknown",
    )
    assert probe_outcome("tcp_reachable", {"expect": False}, {"reachable": False})[0]
    assert probe_outcome("http_status", {}, {"status_code": 204})[0] is True
    assert (
        probe_outcome("http_status", {"expect_status": 200}, {"status_code": 503})[0]
        is False
    )
    assert probe_outcome("lsof_port", {}, {"ok": True, "listening": True})[0] is True
    disk = {"disk": {"free_bytes": 5_000}}
    assert probe_outcome("disk_free", {"min_free_bytes": 4_000}, disk)[0] is True
    assert probe_outcome("disk_free", {"min_free_bytes": 9_000}, disk)[0] is False
    memory = {"memory": {"available_bytes": 100}}
    assert probe_outcome("mem_available", {"min_available_bytes": 50}, memory)[0]
    health = {
        "disks": [{"path": "/", "exit_code": 0, "disk": {"free_bytes": 10}}],
        "memory": {"available_bytes": 100},
    }
    assert probe_outcome("host_health", {}, health)[0] is True
    assert probe_outcome("host_health", {"min_free_bytes": 20}, health)[0] is False
    chain = {"handshake": True, "chain": [{"issuer": "CN=Lab CA"}], "verify_code": 0}
    assert probe_outcome("tls_chain", {}, chain)[0] is True
    assert (
        probe_outcome("tls_chain", {"expected_issuer_substring": "Other"}, chain)[0]
        is False
    )
    vpn = {"ok": True, "configs": [{"name": "Lab VPN", "status": "Connected"}]}
    assert probe_outcome("vpn_configs", {"name_substring": "Lab"}, vpn)[0] is True
    assert probe_outcome("vpn_configs", {"name_substring": "Office"}, vpn)[0] is False
    lease = {"ok": True, "state": {"holder": None}}
    assert probe_outcome(
        "lease_state", {"state_key": "holder", "state_value": None}, lease
    )[0]
    assert probe_outcome("lease_state", {}, {"ok": False})[0] is None
    with pytest.raises(ValidationError, match="min_free_bytes"):
        CheckSpec(kind="disk_free", params={"path": "/"})


async def test_probe_checks_use_butler_host_probes(tmp_path, monkeypatch):
    import nebula.v3.butler_host_probes as host_probes

    calls: list[tuple[str, dict]] = []

    class FakeProbes:
        def __init__(self, *, project_id, actor):
            self.project_id = project_id

        async def probe(self, arguments):
            calls.append((self.project_id, arguments))
            return {"kind": arguments["kind"], "status_code": 200}

    monkeypatch.setattr(host_probes, "ButlerHostProbes", FakeProbes)
    store = NebulaStore(tmp_path / "core.db")
    record = OperatorActionRecord(
        action="restart_host",
        device_or_host="bench",
        reason="r",
        project_id="p1",
        verification=["v"],
        check=CheckSpec(
            kind="http_status",
            params={"url": "https://bench.invalid/health", "expect_status": 200},
        ),
    )
    assert record.check is not None
    result = await DefaultCheckEvaluator(store).evaluate(
        record.check, record, now=utc_now()
    )
    assert (result.passed, result.detail) == (True, "HTTP 200")
    assert calls == [
        (
            "p1",
            {
                "url": "https://bench.invalid/health",
                "kind": "http_status",
                "reason": "operator action request auto-check",
            },
        )
    ]


# Board and notifications ------------------------------------------------------


async def test_board_lists_open_requests_first_by_urgency(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    archived = await store.create(
        Engagement(name="Old", status=EngagementStatus.ARCHIVED)
    )
    goal = await _goal(store, project, profile)
    service = WorkService(store)
    # The stopgap: an untyped "Needs you:" item still shows.
    legacy = await service.create(
        project.id,
        WorkCreate(
            title="Needs you: plug the probe back in",
            description="The bench probe is unplugged.",
            priority="low",
        ),
        actor_id="butler",
    )
    await service.create(
        archived.id, WorkCreate(title="Needs you: ignored"), actor_id="butler"
    )
    await service.create(project.id, WorkCreate(title="Ordinary task"), actor_id="x")
    blocking = await create_request(
        store,
        _arguments(
            project.id,
            goal_session_id=goal.session_id,
            action="attach_credential",
            device_or_host="build runner",
            urgency="blocking",
            check=None,
        ),
    )
    high = await create_request(
        store, _arguments(project.id), evaluator=FixedEvaluator(False)
    )

    app = FastAPI()
    app.include_router(butler_board_router(store), prefix="/api/v1")
    async with AsyncTestClient(app) as client:
        answer = await client.get("/api/v1/butler/board")
        assert answer.status_code == 200
        body = answer.json()
        listed = body["operator_actions"]
        assert [item["id"] for item in listed] == [
            blocking["request"]["id"],
            high["request"]["id"],
            legacy.id,
        ]
        top = listed[0]
        assert top["urgency"] == "blocking"
        assert top["action_label"] == "Attach credential"
        assert top["device_or_host"] == "build runner"
        assert top["project"] == {"id": project.id, "name": "Research"}
        assert top["goal"] == {
            "session_id": goal.session_id,
            "title": "Lab goal chat",
            "status": "running",
        }
        assert listed[1]["last_check"]["passed"] is False
        assert listed[1]["check"]["kind"] == "time_elapsed"
        assert listed[2]["typed"] is False
        assert listed[2]["reason"] == "The bench probe is unplugged."
        assert listed[2]["urgency"] == "low"
        # Pages of one section do not carry them.
        page = await client.get("/api/v1/butler/board", params={"section": "idle"})
        assert page.json()["operator_actions"] is None


async def test_incident_notifier_opens_and_resolves(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(store, project, profile)
    tool = ButlerOperatorActions(
        store, FakeWork(store), evaluator=FixedEvaluator(False)
    )
    assert isinstance(tool.notifier, IncidentNotifier)
    assert [action.spec.name for action in tool.actions()] == [
        CREATE_TOOL,
        LIST_TOOL,
        CLOSE_TOOL,
    ]
    create, listing, close = (action._handler for action in tool.actions())
    created = await create(_arguments(project.id, goal_session_id=goal.session_id))
    assert created["notified"] is True
    (incident,) = await store.find_entities(ButlerIncident, {})
    assert incident.status == "open" and incident.source == "operator_action"
    assert incident.goal_ids == [goal.id]
    assert incident.summary.startswith("Needs you: Restart host on lab-host-1")
    listed = await listing({})
    assert [item["id"] for item in listed["requests"]] == [created["request"]["id"]]
    closed = await close(
        {
            "project_id": project.id,
            "request_id": created["request"]["id"],
            "outcome": "closed",
            "evidence": ["Host answered on port 22 again"],
        }
    )
    assert closed["notified"] is True
    assert (await store.get(ButlerIncident, incident.id)).status == "resolved"
    assert (await listing({}))["requests"] == []
    assert len((await listing({"include_closed": True}))["requests"]) == 1


def test_goal_wait_reason_keeps_hygiene_away():
    from nebula.v3.butler_goal_hygiene import classify_reason

    record = OperatorActionRecord(
        action="restart_host",
        device_or_host="lab-host-1",
        reason="r",
        project_id="p",
        verification=["v"],
        check=CheckSpec(kind="time_elapsed", params={"seconds": 5}),
    )
    goal = ChatGoal(
        engagement_id="p",
        session_id="s",
        objective="o",
        completion_criteria=["c"],
        status=ChatGoalStatus.BLOCKED,
        blocked_reason=goal_wait_reason("req-1", record),
    )
    assert classify_reason(goal)[0] == "never"
