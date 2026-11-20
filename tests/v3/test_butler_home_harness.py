"""Harness Butler in Butler's own project works on its focus project.

The gateway's Butler tools read the focus project and send changes to it as
commands; a local-only focus project never reaches a harness that is not
local; the vendor CLI gets no shell, files or command runtime of its own; and
gateway workspace reads serve the focus project's existing folder. See
docs/design/butler-home-worker.md, sections 3 and 4.
"""

from types import SimpleNamespace

import pytest

from nebula.v3.butler_sessions import (
    ButlerFocus,
    ButlerSessionReservation,
    reserve_butler_session,
)
from nebula.v3.chat import ChatConfigurationError
from nebula.v3.deployment_protocol import BUTLER_HOME_PROJECT
from nebula.v3.domain import (
    ChatBackend,
    ChatSession,
    Engagement,
    HarnessKind,
    HarnessProfile,
    ScopePolicy,
)
from nebula.v3.harnesses import HarnessConfigurationError, HarnessRuntimeService
from tests.v3.test_butler_home_scope import home as home
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_focus_comes_from_the_chat_session(home):
    control, runtime, _ = home
    await control.create(
        ChatSession(
            engagement_id=BUTLER_HOME_PROJECT,
            title="Butler",
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness",
            harness_session_id="harness-session",
            model="inert",
            metadata={"control_assistant": True, "butler_focus_project": "target"},
        )
    )
    service = SimpleNamespace(store=runtime)
    focus = HarnessRuntimeService._butler_focus
    assert await focus(service, BUTLER_HOME_PROJECT, "harness-session") == "target"
    assert await focus(service, BUTLER_HOME_PROJECT, "unknown-session") is None
    assert await focus(service, "target", "harness-session") is None
    assert await focus(service, BUTLER_HOME_PROJECT, None) is None


class _Records:
    def __init__(self, local_harness: bool, local_project: bool):
        self.values = {
            "HarnessSession": SimpleNamespace(harness_profile_id="profile"),
            "HarnessProfile": SimpleNamespace(
                privacy=SimpleNamespace(local_only=local_harness)
            ),
            "Engagement": SimpleNamespace(name="Target", scope_policy_id="scope"),
            "ScopePolicy": SimpleNamespace(local_only=local_project),
        }

    async def get(self, model, _identifier):
        return self.values[model.__name__]


@pytest.mark.parametrize(
    "local_harness, local_project, refused",
    [(False, False, False), (False, True, True), (True, True, False)],
)
async def test_local_only_focus_needs_a_local_harness(
    local_harness, local_project, refused
):
    service = SimpleNamespace(store=_Records(local_harness, local_project))
    check = HarnessRuntimeService._check_focus_privacy(service, "target", "session")
    if refused:
        with pytest.raises(HarnessConfigurationError, match="local models"):
            await check
    else:
        await check


async def test_gateway_workspace_reads_serve_the_focus_folder(tmp_path):
    focus_folder = tmp_path / "focus"

    async def butler_focus(project_id, _session_id):
        return "target" if project_id == BUTLER_HOME_PROJECT else None

    async def focus_workspace(project_id):
        assert project_id == "target"
        return "Target", focus_folder

    service = SimpleNamespace(
        _butler_focus=butler_focus,
        tool_platform=SimpleNamespace(focus_workspace=focus_workspace),
    )
    turn = SimpleNamespace(engagement_id=BUTLER_HOME_PROJECT, harness_session_id="s")
    assert await HarnessRuntimeService._gateway_workspace(service, turn) == focus_folder

    async def no_folder(_project_id):
        return "Target", None

    service.tool_platform = SimpleNamespace(focus_workspace=no_folder)
    with pytest.raises(HarnessConfigurationError, match="no workspace files"):
        await HarnessRuntimeService._gateway_workspace(service, turn)


class _HarnessRuntime:
    def __init__(self):
        self.created = []

    async def _validate_harness_privacy(self, *_args, **_kwargs):
        return None

    async def create_session(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            id="harness-session",
            model="inert",
            mcp_server_ids=[],
            metadata={"runtime_options": {}},
        )


@pytest.mark.parametrize("local_project", [False, True])
async def test_home_harness_butler_gets_no_vendor_tools(home, local_project):
    control, runtime, _ = home
    profile = await control.create(
        HarnessProfile(
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="inert",
            privacy={"local_only": False, "permits_sensitive_data": True},
        )
    )
    await control.create(
        ScopePolicy(id="scope:target", engagement_id="target", local_only=local_project)
    )
    await control.update(Engagement, "target", {"scope_policy_id": "scope:target"})
    harness = _HarnessRuntime()
    request = ButlerSessionReservation(
        engagement_id=BUTLER_HOME_PROJECT,
        backend=ChatBackend.HARNESS,
        harness_profile_id=profile.id,
        focus=ButlerFocus(project="target"),
    )
    if local_project:
        with pytest.raises(ChatConfigurationError, match="local models"):
            await reserve_butler_session(runtime, harness, request)
        assert harness.created == []
        return
    chat = await reserve_butler_session(runtime, harness, request)
    assert chat.engagement_id == BUTLER_HOME_PROJECT
    assert chat.metadata["butler_focus_project"] == "target"
    assert [call["tools_enabled"] for call in harness.created] == [False]
