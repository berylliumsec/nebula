"""A Nebula-wide scheduled Butler check covers every Act project.

The check runs on the worker of the Act project hosting it. That worker
otherwise sees only its own project, so the check could neither list nor
change the other Act projects its mandate covers. Its reads now see every
project (``global_check_reads``), its prompt lists every Act project's open
main goals, and a change to another project runs on that project's own
worker under that project's current Act authority. Per-project checks keep
their project scope. See docs/design/butler-home-worker.md, section 5.
"""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from fastapi import HTTPException

from tests.v3.async_test_support import (
    close_database_handles as close_database_handles,
)
from nebula.v3 import control_assistant_work
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.butler_global_capabilities import ButlerGlobalActions
from nebula.v3.butler_instructions import read_butler_instructions
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_oversight import ButlerEventPayload, _append_project_event
from nebula.v3.butler_recovery import ButlerRecoveryAgent, _fit_roster
from nebula.v3.control_assistant_work import dispatch_work_tool
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    ProviderPrivacy,
    ScopePolicy,
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    EngagementStatus,
    ProviderProfile,
    utc_now,
)
from nebula.v3.execution_scope import (
    execution_context,
    global_check_reads,
    observer_reads,
)
from nebula.v3 import butler_recovery, runtime_butler_actions
from nebula.v3.database import EntityRow
from nebula.v3.runtime_butler_actions import (
    BUTLER_ACTION_RESULT,
    BUTLER_SUBMIT_ACTION,
    ButlerAction,
    RemoteButlerActions,
    register_butler_actions,
    run_butler_action,
)
from nebula.v3.runtime_coordination import CoordinationConflict, RuntimeCoordination
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.shared_memory_rpc import MemoryAuthority
from nebula.v3.storage import NebulaStore, NotFoundError
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
SETTINGS = PoolSettings(5, 12, 2, 5)
AUTHORITY = {"anchor": "act", "steered": "act", "watched": "watch"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _projects(store, provider_id):
    """Three active projects, each with one main goal chat."""

    chats = {}
    policies = {}
    for name, authority in AUTHORITY.items():
        await store.create(
            Engagement(id=name, name=name.title(), status=EngagementStatus.ACTIVE)
        )
        policies[name] = await store.create(
            ButlerAuthorityPolicy(
                id=policy_id(name), engagement_id=name, authority=authority
            )
        )
        chats[name] = await store.create(
            ChatSession(
                id=f"chat-{name}",
                engagement_id=name,
                title=f"{name.title()} research",
                provider_profile_id=provider_id,
                model="model-a",
            )
        )
    return chats, policies


async def _case(store, root, *, signal="butler.global_periodic_check"):
    folder = root / ".butler"
    folder.mkdir(exist_ok=True)
    (folder / "BUTLER.md").write_text("Review every Act project's goals.")
    digest = read_butler_instructions(root).sha256
    async with store.transaction(immediate=True) as transaction:
        event = await _append_project_event(
            transaction,
            engagement_id="anchor",
            event_type=signal,
            payload=ButlerEventPayload(
                project_id="anchor",
                source="core",
                actor_role="core",
                summary="Nebula-wide check",
                metrics={"instructions_sha256": digest},
                verification_state="core_observed",
            ),
            idempotency_key=f"scope-{signal}",
            now=utc_now(),
        )
    return await store.create(
        ButlerRecoveryCase(
            engagement_id="anchor",
            trigger="periodic",
            signal=signal,
            source_event_id=event.id,
            source_event_sequence=event.sequence,
            authority_revision=1,
            status="investigating",
            summary="Nebula-wide check",
        )
    )


@pytest.fixture
async def nebula(tmp_path):
    """One Core process: every project is local to the check's worker."""

    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    app = create_app(
        store,
        auth_token="test-token",
        artifact_store=artifacts,
        tool_platform=RuntimePlatform(
            store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
        ),
    )
    work = app.state.control_assistant_work
    provider = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    chats, policies = await _projects(store, provider.id)
    goals = {}
    for name, chat in chats.items():
        created = await dispatch_work_tool(
            work,
            name,
            "nebula.work.create_goal",
            {
                "session_id": chat.id,
                "objective": f"Map the {name} parser",
                "completion_criteria": ["Evidence retained"],
            },
        )
        goals[name] = await store.update(
            ChatGoal,
            created["goal"]["id"],
            {"status": ChatGoalStatus.PAUSED, "paused_at": utc_now()},
            expected_revision=created["goal"]["revision"],
        )
    case = await _case(store, tmp_path)

    async def capabilities(case_id=case.id):
        return await ButlerCapabilities.create(
            store,
            artifacts,
            "anchor",
            work=work,
            background_case_id=case_id,
            instructions_root=tmp_path,
            background_runtime_local=True,
        )

    yield SimpleNamespace(
        store=store,
        work=work,
        root=tmp_path,
        case=case,
        chats=chats,
        goals=goals,
        policies=policies,
        capabilities=capabilities,
    )
    await store.event_history.close()


async def test_global_check_steers_a_goal_in_another_act_project(nebula):
    butler = await nebula.capabilities()
    steered = nebula.chats["steered"]
    read = await butler.invoke(
        {
            "operation": "nebula.global.goal",
            "arguments": {"project_id": "steered", "session_id": steered.id},
        },
        change=False,
    )
    assert read["goal"]["id"] == nebula.goals["steered"].id
    noted = await butler.invoke(
        {
            "operation": "nebula.global.work_change",
            "arguments": {
                "project_id": "steered",
                "expected_authority_revision": nebula.policies["steered"].revision,
                "operation": "nebula.work.add_goal_note",
                "arguments": {"session_id": steered.id, "text": "Check the parser."},
            },
        },
        change=True,
    )
    assert noted["goal"]["supervisor_note_count"] == 1
    goal = await nebula.store.get(ChatGoal, nebula.goals["steered"].id)
    resumed = await butler.invoke(
        {
            "operation": "nebula.global.goal_action",
            "arguments": {
                "project_id": "steered",
                "expected_authority_revision": nebula.policies["steered"].revision,
                "session_id": steered.id,
                "expected_revision": goal.revision,
                "action": "cancel",
                "reason": "Superseded by the operator's plan.",
            },
        },
        change=True,
    )
    assert resumed["goal"]["status"] == "cancelled"
    assert (await nebula.store.get(ChatGoal, goal.id)).status == "cancelled"


async def test_global_check_cannot_act_in_a_watch_project(nebula):
    butler = await nebula.capabilities()
    watched = nebula.chats["watched"]
    for operation, arguments, change in (
        (
            "nebula.global.work_change",
            {
                "project_id": "watched",
                "expected_authority_revision": nebula.policies["watched"].revision,
                "operation": "nebula.work.add_goal_note",
                "arguments": {"session_id": watched.id, "text": "Do not apply."},
            },
            True,
        ),
        (
            "nebula.global.goal",
            {"project_id": "watched", "session_id": watched.id},
            False,
        ),
    ):
        with pytest.raises(ValueError, match="Act and privacy scope") as refused:
            await butler.invoke(
                {"operation": operation, "arguments": arguments}, change=change
            )
        # A model-supplied ID learns neither the name nor the authority.
        assert "Watched" not in str(refused.value)
        assert "Watch" not in str(refused.value)
    goal = await nebula.store.get(ChatGoal, nebula.goals["watched"].id)
    assert goal.supervisor_notes == []
    listed = await butler.invoke(
        {"operation": "nebula.global.projects", "arguments": {}}, change=False
    )
    assert [row["id"] for row in listed["projects"]] == ["anchor", "steered"]


async def test_per_project_checks_stay_in_their_project(nebula):
    case = await _case(nebula.store, nebula.root, signal="goal.blocked")
    butler = await nebula.capabilities(case.id)
    names = {plugin.spec.name for plugin in butler._actions.values()}
    assert not any(name.startswith("nebula.global.") for name in names)
    with pytest.raises(ValueError, match="only on a main goal"):
        await butler.invoke(
            {
                "operation": "nebula.work.add_goal_note",
                "arguments": {
                    "session_id": nebula.chats["steered"].id,
                    "text": "Outside this project.",
                },
            },
            change=True,
        )
    assert (
        await nebula.store.get(ChatGoal, nebula.goals["steered"].id)
    ).supervisor_notes == []


async def test_global_check_prompt_lists_every_act_projects_goals(nebula):
    agent = ButlerRecoveryAgent(
        nebula.store,
        provider_factory=lambda _: None,
        harness_runtime=None,
        artifact_store=None,
        work=nebula.work,
        memory=None,
        instructions_root=nebula.root,
    )
    packet = json.loads(await agent._packet(nebula.case, runtime_local=True))
    rows = {row["project_id"]: row for row in packet["act_projects"]}
    assert set(rows) == {"anchor", "steered"}
    assert packet["act_project_count"] == 2 and packet["truncated"] is False
    for name in ("anchor", "steered"):
        (goal,) = rows[name]["goals"]
        assert goal["goal_id"] == nebula.goals[name].id
        assert goal["session_id"] == nebula.chats[name].id
        assert goal["title"] == f"{name.title()} research"
        assert goal["status"] == "paused"
        assert "stage" in goal and goal["objective"].startswith("Map the")
        assert rows[name]["authority_revision"] == nebula.policies[name].revision


def test_roster_shrinks_to_fit_the_prompt_and_says_so():
    goal = {"goal_id": "g", "objective": "x" * 150, "title": "t", "status": "running"}
    packet = {
        "act_projects": [
            {"project_id": f"p{index}", "goals": [dict(goal) for _ in range(8)]}
            | {"goals_omitted": 0}
            for index in range(10)
        ],
        "truncated": False,
    }
    rendered = _fit_roster(packet, json.dumps(packet), 4_000)
    assert len(rendered) <= 4_000
    assert packet["truncated"] is True
    rows = packet["act_projects"]
    assert all("objective" not in item for row in rows for item in row["goals"])
    assert sum(row["goals_omitted"] for row in rows) > 0
    assert sum(len(row["goals"]) + row["goals_omitted"] for row in rows) == 80


class _Remote:
    def __init__(self, state="succeeded"):
        self.sent, self.state = [], state

    async def run(self, target, action):
        self.sent.append((target, action))
        return {
            "state": self.state,
            "command_id": "command-1",
            "result": {"applied": {"goal": {"status": "paused"}}},
        }


async def test_changes_to_other_projects_go_to_their_own_worker(nebula):
    remote = _Remote()
    actions = ButlerGlobalActions(
        nebula.store,
        nebula.work,
        nebula.root,
        nebula.case.id,
        runtime_local=True,
        remote_actions=remote,
    )
    steered = nebula.chats["steered"]
    goal = nebula.goals["steered"]
    result = await actions.goal_action(
        {
            "project_id": "steered",
            "expected_authority_revision": nebula.policies["steered"].revision,
            "session_id": steered.id,
            "expected_revision": goal.revision,
            "action": "resume",
        }
    )
    assert result == {"goal": {"status": "paused"}}
    ((target, sent),) = remote.sent
    assert target == "steered"
    assert (sent.action, sent.initiator, sent.case_id, sent.authority_revision) == (
        "nebula.work.goal_action",
        "background",
        nebula.case.id,
        nebula.policies["steered"].revision,
    )
    assert sent.arguments == {
        "session_id": steered.id,
        "expected_revision": goal.revision,
        "action": "resume",
    }
    # Nothing ran here: the target's worker owns the change.
    assert (await nebula.store.get(ChatGoal, goal.id)).revision == goal.revision
    # The host project's own changes still run in place.
    await actions.work_change(
        {
            "project_id": "anchor",
            "expected_authority_revision": nebula.policies["anchor"].revision,
            "operation": "nebula.work.add_goal_note",
            "arguments": {"session_id": nebula.chats["anchor"].id, "text": "Here."},
        }
    )
    assert len(remote.sent) == 1
    # Changes whose result links back to this case stay in the host project.
    with pytest.raises(ValueError, match="runs only in this check's host project"):
        await actions.create_recovery_goal(
            {
                "project_id": "steered",
                "expected_project_revision": 1,
                "expected_authority_revision": nebula.policies["steered"].revision,
                "source_goal_id": goal.id,
                "objective": "Recover",
                "completion_criteria": ["Done"],
                "plan": ["Read"],
            }
        )
    with pytest.raises(ValueError, match="Act and privacy scope"):
        await actions.goal_action(
            {
                "project_id": "watched",
                "expected_authority_revision": nebula.policies["watched"].revision,
                "session_id": nebula.chats["watched"].id,
                "expected_revision": nebula.goals["watched"].revision,
                "action": "resume",
            }
        )
    assert len(remote.sent) == 1
    remote.state = "accepted"
    queued = await actions.work_change(
        {
            "project_id": "steered",
            "expected_authority_revision": nebula.policies["steered"].revision,
            "operation": "nebula.work.add_goal_note",
            "arguments": {"session_id": steered.id, "text": "Queued."},
        }
    )
    assert queued["state"] == "accepted" and "not at all" in queued["detail"]


@pytest.fixture
async def fleet(location, tmp_path):
    """Each project owned by its own execution runtime, as in production."""

    control = NebulaStore(Database(location, pool_settings=SETTINGS))
    runtime = None
    try:
        await control.create(
            ProviderProfile(id="provider", name="Inert", provider_type="ollama")
        )
        chats, policies = await _projects(control, "provider")
        goals = {}
        for name, chat in chats.items():
            goals[name] = await control.create(
                ChatGoal(
                    engagement_id=name,
                    session_id=chat.id,
                    objective=f"Map the {name} parser",
                    completion_criteria=["Evidence retained"],
                    status=ChatGoalStatus.RUNNING,
                )
            )
        case = await _case(control, tmp_path)
        coordination = RuntimeCoordination(control.database)
        for name in AUTHORITY:
            await coordination.register_runtime(f"worker-{name}", "fixture")
        # Targets are claimed only after a submission: the host admits a
        # change only for an owner whose release runs it.
        leases = {"anchor": await coordination.claim_project("anchor", "worker-anchor")}
        runtime = NebulaStore(
            Database(
                location,
                bootstrap=False,
                execution_runtime_id="worker-anchor",
                pool_settings=SETTINGS,
            )
        )
        yield SimpleNamespace(
            control=control,
            runtime=runtime,
            coordination=coordination,
            leases=leases,
            case=case,
            chats=chats,
            goals=goals,
            policies=policies,
            root=tmp_path,
        )
    finally:
        if runtime:
            await runtime.event_history.close()
            await runtime.database.dispose()
        await control.event_history.close()
        await control.database.dispose()


async def test_the_host_worker_reads_every_act_project_only_inside_the_check(fleet):
    runtime, lease = fleet.runtime, fleet.leases["anchor"]
    actions = ButlerGlobalActions(
        runtime,
        SimpleNamespace(),
        fleet.root,
        fleet.case.id,
        runtime_local=True,
        remote_actions=_Remote(),
    )
    with execution_context(lease):
        # The production failure: the host worker saw only its own project.
        with pytest.raises(NotFoundError):
            await runtime.get(ChatGoal, fleet.goals["steered"].id)
        with observer_reads(), pytest.raises(CoordinationConflict):
            await runtime.get(ChatGoal, fleet.goals["steered"].id)
        roster = await actions.roster()
        rows = {row["project_id"]: row for row in roster["act_projects"]}
        assert set(rows) == {"anchor", "steered"}
        assert [goal["goal_id"] for goal in rows["steered"]["goals"]] == [
            fleet.goals["steered"].id
        ]
        project = await actions._act("steered", fleet.policies["steered"].revision)
        assert project.id == "steered"
        with pytest.raises(ValueError, match="Act and privacy scope"):
            await actions._act("watched", fleet.policies["watched"].revision)
        # Writes stay fenced to the host project even while reading widely.
        with global_check_reads():
            assert (await runtime.get(ChatGoal, fleet.goals["steered"].id)).id
            with pytest.raises(Exception):
                await runtime.update(
                    ChatGoal,
                    fleet.goals["steered"].id,
                    {"blocked_reason": "injected"},
                )
    saved = await fleet.control.get(ChatGoal, fleet.goals["steered"].id)
    assert saved.blocked_reason is None


class _Rpc:
    def __init__(self):
        self.handlers = {}

    def register(self, name, handler):
        self.handlers[name] = handler

    async def call(self, name, payload):
        return await self.handlers[name](payload)


def _submission(lease, case_id, *, target="steered", key="k1"):
    return {
        "lease": MemoryAuthority(
            project_id=lease.project_id,
            runtime_id=lease.runtime_id,
            fencing_token=lease.fencing_token,
        ).model_dump(),
        "target_project": target,
        "idempotency_key": key,
        "action": ButlerAction(
            action="nebula.work.add_goal_note",
            arguments={"session_id": "chat-steered", "text": "Check."},
            initiator="background",
            case_id=case_id,
            authority_revision=1,
        ).model_dump(mode="json"),
    }


async def test_host_admits_only_the_live_global_cases_own_changes(fleet):
    rpc = _Rpc()
    register_butler_actions(rpc, "worker-anchor", fleet.coordination)
    submit = rpc.handlers[BUTLER_SUBMIT_ACTION]
    anchor = fleet.leases["anchor"]
    accepted = await submit(_submission(anchor, fleet.case.id))
    assert accepted["command_id"]
    other_case = await _case(fleet.control, fleet.root, signal="goal.blocked")
    for refused in (
        _submission(anchor, other_case.id, key="k2"),
        _submission(anchor, fleet.case.id, target="anchor", key="k3"),
        _submission(anchor, "missing-case", key="k4"),
    ):
        with pytest.raises(HTTPException) as denied:
            await submit(refused)
        assert denied.value.status_code == 403
    # An operator-style action still needs the Butler home grant.
    plain = _submission(anchor, fleet.case.id, key="k5")
    plain["action"] = ButlerAction(
        action="nebula.work.add_goal_note", arguments={}
    ).model_dump(mode="json")
    with pytest.raises(HTTPException) as denied:
        await submit(plain)
    assert denied.value.status_code == 403
    # The sender's client submits and reads its own case's change.
    remote = RemoteButlerActions(rpc, anchor, poll_seconds=0.01)
    outcome = await remote.run(
        "steered",
        ButlerAction(
            action="nebula.work.add_goal_note",
            arguments={"session_id": "chat-steered", "text": "Check."},
            initiator="background",
            case_id=fleet.case.id,
            authority_revision=1,
        ),
        wait_seconds=0.05,
    )
    assert outcome["state"] == "accepted" and outcome["result"] == {}
    # A command is read back only under the case that sent it.
    with pytest.raises(HTTPException):
        await rpc.handlers[BUTLER_ACTION_RESULT](
            {
                "lease": _submission(anchor, fleet.case.id)["lease"],
                "target_project": "steered",
                "command_id": accepted["command_id"],
                "case_id": other_case.id,
            }
        )
    # Another project's worker cannot send the anchor's case changes.
    peer = _Rpc()
    register_butler_actions(peer, "worker-steered", fleet.coordination)
    with pytest.raises(HTTPException) as denied:
        await peer.handlers[BUTLER_SUBMIT_ACTION](
            _submission(
                await fleet.coordination.claim_project("steered", "worker-steered"),
                fleet.case.id,
                target="watched",
            )
        )
    assert denied.value.status_code == 403


@pytest.mark.parametrize("target", ["steered", "watched"])
async def test_target_runs_a_background_change_only_under_its_own_act(
    fleet, monkeypatch, target
):
    calls = []

    async def dispatch(work, project_id, name, arguments, *, caller_session_id=None):
        calls.append((project_id, name))
        return {"noted": True}

    monkeypatch.setattr(control_assistant_work, "dispatch_work_tool", dispatch)
    rpc = _Rpc()
    register_butler_actions(rpc, "worker-anchor", fleet.coordination)
    await rpc.handlers[BUTLER_SUBMIT_ACTION](
        _submission(fleet.leases["anchor"], fleet.case.id, target=target)
    )
    lease = await fleet.coordination.claim_project(target, f"worker-{target}")
    claimed = await fleet.coordination.claim_next(lease, lane="control")
    app = SimpleNamespace(
        state=SimpleNamespace(
            control_assistant_work=SimpleNamespace(store=fleet.control)
        )
    )
    await run_butler_action(app, fleet.coordination, lease, claimed)
    outcome = await rpc.handlers[BUTLER_ACTION_RESULT](
        {
            "lease": _submission(fleet.leases["anchor"], fleet.case.id)["lease"],
            "target_project": target,
            "command_id": claimed.receipt.command_id,
            "case_id": fleet.case.id,
        }
    )
    if target == "steered":
        assert outcome["state"] == "succeeded"
        assert calls == [("steered", "nebula.work.add_goal_note")]
    else:
        assert outcome["state"] == "failed"
        assert "Watch authority here" in outcome["result"]["detail"]
        assert "Watched" not in outcome["result"]["detail"]
        assert calls == []


async def test_target_refuses_a_change_read_under_older_authority(fleet, monkeypatch):
    async def dispatch(*args, **kwargs):
        raise AssertionError("a stale authority read must not apply")

    monkeypatch.setattr(control_assistant_work, "dispatch_work_tool", dispatch)
    policy = fleet.policies["steered"]
    await fleet.control.update(
        ButlerAuthorityPolicy,
        policy.id,
        {"authority": "act", "delegate_actions": []},
        expected_revision=policy.revision,
    )
    rpc = _Rpc()
    register_butler_actions(rpc, "worker-anchor", fleet.coordination)
    await rpc.handlers[BUTLER_SUBMIT_ACTION](
        _submission(fleet.leases["anchor"], fleet.case.id)
    )
    lease = await fleet.coordination.claim_project("steered", "worker-steered")
    claimed = await fleet.coordination.claim_next(lease, lane="control")
    app = SimpleNamespace(
        state=SimpleNamespace(
            control_assistant_work=SimpleNamespace(store=fleet.control)
        )
    )
    await run_butler_action(app, fleet.coordination, lease, claimed)
    outcome = await rpc.handlers[BUTLER_ACTION_RESULT](
        {
            "lease": _submission(fleet.leases["anchor"], fleet.case.id)["lease"],
            "target_project": "steered",
            "command_id": claimed.receipt.command_id,
            "case_id": fleet.case.id,
        }
    )
    assert outcome["state"] == "failed"
    assert "changed" in outcome["result"]["detail"]


def test_background_actions_name_their_case_and_authority():
    with pytest.raises(ValueError):
        ButlerAction(action="nebula.work.goal_action", initiator="background")
    with pytest.raises(ValueError):
        ButlerAction(action="nebula.work.goal_action", case_id="case")
    assert (
        ButlerAction(
            action="nebula.work.goal_action",
            initiator="background",
            case_id="case",
            authority_revision=2,
        ).case_id
        == "case"
    )


async def test_a_closed_check_changes_nothing_here_or_remotely(nebula):
    case = await nebula.store.get(ButlerRecoveryCase, nebula.case.id)
    await nebula.store.update(
        ButlerRecoveryCase,
        case.id,
        {"status": "verifying"},
        expected_revision=case.revision,
    )
    remote = _Remote()
    actions = ButlerGlobalActions(
        nebula.store,
        nebula.work,
        nebula.root,
        nebula.case.id,
        runtime_local=True,
        remote_actions=remote,
    )
    with pytest.raises(ValueError, match="no longer active"):
        await actions.work_change(
            {
                "project_id": "steered",
                "expected_authority_revision": nebula.policies["steered"].revision,
                "operation": "nebula.work.add_goal_note",
                "arguments": {
                    "session_id": nebula.chats["steered"].id,
                    "text": "Too late.",
                },
            }
        )
    assert remote.sent == []


async def _private_act_project(store):
    """An Act project whose data stays on local models."""

    scope = await store.create(
        ScopePolicy(id="scope:private", engagement_id="private", local_only=True)
    )
    await store.create(
        Engagement(
            id="private",
            name="Private Lab",
            status=EngagementStatus.ACTIVE,
            scope_policy_id=scope.id,
        )
    )
    await store.create(
        ButlerAuthorityPolicy(
            id=policy_id("private"), engagement_id="private", authority="act"
        )
    )


async def test_a_cloud_runtime_never_learns_a_local_only_projects_name(nebula):
    await _private_act_project(nebula.store)
    cloud = ButlerGlobalActions(
        nebula.store, nebula.work, nebula.root, nebula.case.id, runtime_local=False
    )
    with pytest.raises(ValueError, match="Act and privacy scope") as refused:
        await cloud.project({"project_id": "private"})
    assert "Private Lab" not in str(refused.value)
    assert "local" not in str(refused.value).lower().replace("local models", "")
    roster = await cloud.roster()
    assert "private" not in {row["project_id"] for row in roster["act_projects"]}
    local = ButlerGlobalActions(
        nebula.store, nebula.work, nebula.root, nebula.case.id, runtime_local=True
    )
    assert "private" in {
        row["project_id"] for row in (await local.roster())["act_projects"]
    }


class _PacketBuilt(Exception):
    pass


async def test_roster_privacy_follows_the_account_that_actually_runs(
    nebula, monkeypatch
):
    await _private_act_project(nebula.store)

    def harness(name, local_only):
        return HarnessProfile(
            name=name,
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-h",
            capabilities=HarnessCapabilities(models=["model-h"]),
            privacy=ProviderPrivacy(
                local_only=local_only,
                permits_sensitive_data=True,
                auto_share_tool_results=True,
            ),
        )

    preferred = await nebula.store.create(harness("Local account", True))
    cloud = await nebula.store.create(harness("Cloud account", False))
    agent = ButlerRecoveryAgent(
        nebula.store,
        provider_factory=lambda _: None,
        harness_runtime=None,
        artifact_store=None,
        work=nebula.work,
        memory=None,
        instructions_root=nebula.root,
    )

    async def config(*_args, **_kwargs):
        return {
            "profile_id": preferred.id,
            "model": "model-h",
            "backend": "harness",
            "harness_reasoning_effort": None,
            "harness_cyber_access_program": None,
        }

    async def parent(_project_id):
        return SimpleNamespace(id="parent")

    async def swapped(*_args):
        return cloud

    async def session(*_args):
        return SimpleNamespace(id="case-chat")

    built = []
    original = agent._packet

    async def packet(case, *, runtime_local=False):
        built.append(json.loads(await original(case, runtime_local=runtime_local)))
        raise _PacketBuilt

    monkeypatch.setattr(butler_recovery, "chat_assistant_config", config)
    monkeypatch.setattr(agent, "_butler_session", parent)
    monkeypatch.setattr(agent, "_scheduled_harness_profile", swapped)
    monkeypatch.setattr(agent, "_case_session", session)
    monkeypatch.setattr(agent, "_packet", packet)
    with pytest.raises(_PacketBuilt):
        await agent._run(nebula.case)
    projects = {row["project_id"] for row in built[0]["act_projects"]}
    assert projects == {"anchor", "steered"}


async def test_one_change_to_two_projects_is_two_commands(fleet):
    rpc = _Rpc()
    register_butler_actions(rpc, "worker-anchor", fleet.coordination)
    remote = RemoteButlerActions(rpc, fleet.leases["anchor"], poll_seconds=0.01)
    action = ButlerAction(
        action="nebula.work.add_goal_note",
        arguments={"session_id": "chat", "text": "Same note."},
        initiator="background",
        case_id=fleet.case.id,
        authority_revision=1,
    )
    first = await remote.run("steered", action, wait_seconds=0.02)
    second = await remote.run("watched", action, wait_seconds=0.02)
    assert first["state"] == second["state"] == "accepted"
    assert first["command_id"] != second["command_id"]
    # A resend to the same project still finds its first command.
    again = await remote.run("steered", action, wait_seconds=0.02)
    assert again["command_id"] == first["command_id"]


async def test_a_change_queued_before_the_check_closed_still_runs_within_its_ttl(
    fleet, monkeypatch
):
    calls = []

    async def dispatch(work, project_id, name, arguments, *, caller_session_id=None):
        calls.append(project_id)
        return {"noted": True}

    monkeypatch.setattr(control_assistant_work, "dispatch_work_tool", dispatch)
    rpc = _Rpc()
    register_butler_actions(rpc, "worker-anchor", fleet.coordination)
    submit = rpc.handlers[BUTLER_SUBMIT_ACTION]
    await submit(_submission(fleet.leases["anchor"], fleet.case.id))
    case = await fleet.control.get(ButlerRecoveryCase, fleet.case.id)
    await fleet.control.update(
        ButlerRecoveryCase,
        case.id,
        {"status": "verifying"},
        expected_revision=case.revision,
    )
    # A closed check sends nothing new.
    with pytest.raises(HTTPException) as denied:
        await submit(_submission(fleet.leases["anchor"], fleet.case.id, key="k2"))
    assert denied.value.status_code == 403
    # The documented window: a change already queued runs on the target,
    # under the target's own Act check, until its ten-minute expiry.
    lease = await fleet.coordination.claim_project("steered", "worker-steered")
    claimed = await fleet.coordination.claim_next(lease, lane="control")
    app = SimpleNamespace(
        state=SimpleNamespace(
            control_assistant_work=SimpleNamespace(store=fleet.control)
        )
    )
    await run_butler_action(app, fleet.coordination, lease, claimed)
    assert calls == ["steered"]


async def test_an_expired_background_change_never_applies(fleet, monkeypatch):
    async def dispatch(*args, **kwargs):
        raise AssertionError("an expired change must not apply")

    monkeypatch.setattr(control_assistant_work, "dispatch_work_tool", dispatch)
    monkeypatch.setattr(runtime_butler_actions, "ACTION_TTL_MS", -60_000)
    rpc = _Rpc()
    register_butler_actions(rpc, "worker-anchor", fleet.coordination)
    await rpc.handlers[BUTLER_SUBMIT_ACTION](
        _submission(fleet.leases["anchor"], fleet.case.id)
    )
    lease = await fleet.coordination.claim_project("steered", "worker-steered")
    claimed = await fleet.coordination.claim_next(lease, lane="control")
    app = SimpleNamespace(
        state=SimpleNamespace(
            control_assistant_work=SimpleNamespace(store=fleet.control)
        )
    )
    await run_butler_action(app, fleet.coordination, lease, claimed)
    outcome = await rpc.handlers[BUTLER_ACTION_RESULT](
        {
            "lease": _submission(fleet.leases["anchor"], fleet.case.id)["lease"],
            "target_project": "steered",
            "command_id": claimed.receipt.command_id,
            "case_id": fleet.case.id,
        }
    )
    assert outcome["state"] == "failed"
    assert "waited too long" in outcome["result"]["detail"]


async def test_other_project_reads_take_no_row_lock(fleet):
    goal_id = fleet.goals["steered"].id
    own_goal_id = fleet.goals["anchor"].id
    with execution_context(fleet.leases["anchor"]), global_check_reads():
        async with fleet.runtime.database.session() as session:
            locked_other = await session.scalar(
                select(EntityRow).where(EntityRow.id == goal_id).with_for_update()
            )
            locked_own = await session.scalar(
                select(EntityRow).where(EntityRow.id == own_goal_id).with_for_update()
            )
            plain_other = await session.scalar(
                select(EntityRow).where(EntityRow.id == goal_id)
            )
    # A locking read of another project's row matches nothing, so the check
    # can never block that project's worker; a plain read sees it.
    assert locked_other is None
    assert locked_own is not None
    assert plain_other is not None
