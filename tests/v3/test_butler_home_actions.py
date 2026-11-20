"""Butler changes from its home worker, run by the target project's worker.

Butler's home worker cannot write another project. It asks the host to submit
one ``butler.action/v1`` command; the target project's owner runs it with the
same Core action Butler uses there and always finishes it succeeded or
failed. See docs/design/butler-home-worker.md, section 4.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from nebula.v3 import control_assistant_work, runtime_butler_actions
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.database import Database, DeploymentHostRow, RuntimeCommandRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import (
    BUTLER_ACTION_COMMAND,
    BUTLER_HOME_PROJECT,
    GLOBAL_EXECUTION_PROJECT,
)
from nebula.v3.domain import Engagement, EngagementStatus
from nebula.v3.runtime_butler_actions import (
    BUTLER_ACTION_RESULT,
    BUTLER_SUBMIT_ACTION,
    ButlerAction,
    RemoteButlerActions,
    action_key,
    register_butler_actions,
    run_butler_action,
)
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import DurableHttpRunner
from nebula.v3.shared_memory_rpc import MemoryAuthority
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError, RiskClass, ToolSpec
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
SETTINGS = PoolSettings(5, 12, 2, 5)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Rpc:
    def __init__(self):
        self.handlers = {}

    def register(self, name, handler):
        self.handlers[name] = handler

    async def call(self, name, payload):
        return await self.handlers[name](payload)


@pytest.fixture
async def core(location):
    store = NebulaStore(Database(location, pool_settings=SETTINGS))
    try:
        for project in (BUTLER_HOME_PROJECT, "target"):
            await store.create(Engagement(id=project, name=project))
        coordination = RuntimeCoordination(store.database)
        await coordination.register_runtime("butler", "fixture")
        await coordination.register_runtime("target-worker", "fixture")
        home = await coordination.claim_project(BUTLER_HOME_PROJECT, "butler")
        rpc = _Rpc()
        register_butler_actions(rpc, "butler", coordination)
        yield SimpleNamespace(
            store=store, coordination=coordination, home=home, rpc=rpc
        )
    finally:
        await store.event_history.close()
        await store.database.dispose()


def _submit(lease, *, target="target", action="nebula.work.goal_action", key="k1"):
    return {
        "lease": MemoryAuthority(
            project_id=lease.project_id,
            runtime_id=lease.runtime_id,
            fencing_token=lease.fencing_token,
        ).model_dump(),
        "target_project": target,
        "idempotency_key": key,
        "action": ButlerAction(
            action=action, arguments={"session_id": "chat", "action": "pause"}
        ).model_dump(mode="json"),
    }


async def _row(core, command_id):
    async with core.store.database.session() as session:
        return await session.scalar(
            select(RuntimeCommandRow).where(RuntimeCommandRow.id == command_id)
        )


async def test_only_the_home_worker_submits_listed_changes(core):
    submit = core.rpc.handlers[BUTLER_SUBMIT_ACTION]
    first = await submit(_submit(core.home))
    row = await _row(core, first["command_id"])
    assert (row.project_id, row.name, row.lane, row.actor_id, row.state) == (
        "target",
        BUTLER_ACTION_COMMAND,
        "control",
        "butler",
        "queued",
    )
    assert row.payload["expires_at_ms"] > row.accepted_at_ms
    # A resend with the same key finds the same command.
    assert (await submit(_submit(core.home)))["command_id"] == first["command_id"]

    other = SimpleNamespace(
        project_id="target", runtime_id="butler", fencing_token=core.home.fencing_token
    )
    stale = SimpleNamespace(
        project_id=BUTLER_HOME_PROJECT,
        runtime_id="butler",
        fencing_token=core.home.fencing_token + 1,
    )
    for refused in (
        _submit(other),
        _submit(stale),
        _submit(core.home, action="nebula.work.chats"),
        _submit(core.home, target=GLOBAL_EXECUTION_PROJECT),
    ):
        with pytest.raises(HTTPException) as denied:
            await submit(refused)
        assert denied.value.status_code == 403
    peer = _Rpc()
    register_butler_actions(peer, "someone-else", core.coordination)
    with pytest.raises(HTTPException) as denied:
        await peer.handlers[BUTLER_SUBMIT_ACTION](_submit(core.home, key="k2"))
    assert denied.value.status_code == 403


async def test_submission_waits_for_a_release_that_runs_the_command(core):
    from nebula.v3.deployments import DeploymentCoordinator
    from tests.v3.test_deployment_lifecycle import manifest

    deployments = DeploymentCoordinator(core.store.database)
    await deployments.stage(
        manifest("older", features=["project.handoff/v1"]),
        Path("/synthetic/releases/older"),
        "a" * 64,
    )
    async with deployments.coordination._write() as session:
        host = await session.get(DeploymentHostRow, "host")
        host.target_version = "older"
    with pytest.raises(HTTPException) as refused:
        await core.rpc.handlers[BUTLER_SUBMIT_ACTION](_submit(core.home))
    assert refused.value.status_code == 409
    assert "next update" in refused.value.detail
    async with core.store.database.session() as session:
        assert (
            await session.scalar(
                select(RuntimeCommandRow).where(
                    RuntimeCommandRow.name == BUTLER_ACTION_COMMAND
                )
            )
        ) is None


def _app(core):
    return SimpleNamespace(
        state=SimpleNamespace(control_assistant_work=SimpleNamespace(store=core.store))
    )


async def _claimed(core, **changes):
    submitted = await core.rpc.handlers[BUTLER_SUBMIT_ACTION](
        _submit(core.home, **changes)
    )
    target = await core.coordination.claim_project("target", "target-worker")
    claimed = await core.coordination.claim_next(target, lane="control")
    assert claimed.receipt.command_id == submitted["command_id"]
    return target, claimed


async def test_target_runs_the_change_through_butlers_own_action(core, monkeypatch):
    calls = []

    async def dispatch(work, project_id, name, arguments, *, caller_session_id=None):
        calls.append((project_id, name, arguments))
        return {"goal": "paused", "revision": 4}

    monkeypatch.setattr(control_assistant_work, "dispatch_work_tool", dispatch)
    target, claimed = await _claimed(core)
    runner = DurableHttpRunner(
        SimpleNamespace(coordination=core.coordination), _app(core), operator_token="x"
    )
    await runner._run_claimed_inline(target, claimed)
    assert calls == [
        ("target", "nebula.work.goal_action", {"session_id": "chat", "action": "pause"})
    ]
    row = await _row(core, claimed.receipt.command_id)
    assert row.state == "succeeded"
    assert row.result["applied"] == {"goal": "paused", "revision": 4}
    outcome = await core.rpc.handlers[BUTLER_ACTION_RESULT](
        {
            "lease": _submit(core.home)["lease"],
            "target_project": "target",
            "command_id": claimed.receipt.command_id,
        }
    )
    assert outcome["state"] == "succeeded"


@pytest.mark.parametrize("case", ["refused", "expired", "archived"])
async def test_target_failures_finish_failed_never_uncertain(core, monkeypatch, case):
    calls = []

    async def dispatch(work, project_id, name, arguments, *, caller_session_id=None):
        calls.append(name)
        raise ValueError("goal revision changed; read it again")

    monkeypatch.setattr(control_assistant_work, "dispatch_work_tool", dispatch)
    if case == "expired":
        monkeypatch.setattr(runtime_butler_actions, "ACTION_TTL_MS", -1)
    if case == "archived":
        await core.store.update(
            Engagement, "target", {"status": EngagementStatus.ARCHIVED}
        )
    target, claimed = await _claimed(core)
    await run_butler_action(_app(core), core.coordination, target, claimed)
    row = await _row(core, claimed.receipt.command_id)
    assert row.state == "failed"
    expected = {
        "refused": "goal revision changed",
        "expired": "waited too long",
        "archived": "Restore the project",
    }[case]
    assert expected in row.result["detail"]
    assert calls == (["nebula.work.goal_action"] if case == "refused" else [])


async def test_remote_client_waits_for_the_outcome_or_reports_it_queued(core):
    client = RemoteButlerActions(core.rpc, core.home, poll_seconds=0.01)
    queued = await client.run(
        "target",
        ButlerAction(action="nebula.work.stop_chat", arguments={"session_id": "c"}),
        wait_seconds=0.05,
    )
    assert queued["state"] == "accepted"
    row = await _row(core, queued["command_id"])
    assert row.idempotency_key == action_key(
        None, "nebula.work.stop_chat", {"session_id": "c"}
    )


class _Change:
    def __init__(self, name):
        self.spec = ToolSpec(
            name=name,
            description="change",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_class=RiskClass.WORKSPACE_WRITE,
        )

    async def _handler(self, _payload):
        raise AssertionError("a home change never runs in Butler's own worker")


class _Actions:
    def __init__(self, outcome):
        self.outcome, self.sent = outcome, []

    async def run(self, target, action):
        self.sent.append((target, action.action, action.arguments))
        return self.outcome


@pytest.mark.parametrize(
    "outcome",
    [
        {"state": "succeeded", "command_id": "c", "result": {"applied": {"ok": 1}}},
        {"state": "accepted", "command_id": "c", "result": {}},
        {"state": "failed", "command_id": "c", "result": {"detail": "stale"}},
    ],
)
async def test_gateway_sends_focus_changes_to_the_target(core, outcome):
    actions = _Actions(outcome)
    gateway = await ButlerCapabilities.create(
        core.store, None, "target", observer=True, butler_actions=actions
    )
    gateway._actions["nebula.work.goal_action"] = _Change("nebula.work.goal_action")
    call = {"operation": "nebula.work.goal_action", "arguments": {"x": 1}}
    if outcome["state"] == "failed":
        with pytest.raises(ValueError, match="refused the change: stale"):
            await gateway.invoke(call, change=True)
    else:
        result = await gateway.invoke(call, change=True)
        if outcome["state"] == "succeeded":
            assert result["ok"] == 1
        else:
            assert result["state"] == "accepted"
    assert actions.sent == [("target", "nebula.work.goal_action", {"x": 1})]

    gateway._actions["nebula.settings.change"] = _Change("nebula.settings.change")
    with pytest.raises(CapabilityInputError) as refused:
        await gateway.invoke(
            {"operation": "nebula.settings.change", "arguments": {}}, change=True
        )
    assert refused.value.reason == "home_change_unavailable"
