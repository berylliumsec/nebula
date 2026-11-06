"""Real Core observations, private resource boundaries and saved guide progress."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from pathlib import Path
import re

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_administration import ButlerAdministration, GUIDES
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.diagnostics import DiagnosticManager
from nebula.v3.domain import ChatSession, Engagement, HarnessSession, Observation
from nebula.v3.operators import OperatorProfileService
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError, GuidedToolError


@pytest.fixture
async def administration(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = await store.create(Engagement(name="Selected metadata project"))
    foreign = await store.create(Engagement(name="Foreign metadata project"))
    platform = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path
    )
    (await platform.workspace_for(project.id)).mkdir(parents=True, exist_ok=True)
    diagnostics = DiagnosticManager(tmp_path, watch_settings=False)
    app = create_app(
        store,
        artifact_store=artifacts,
        tool_platform=platform,
        diagnostic_manager=diagnostics,
        auth_token="fixture-token",
    )
    gateway = SettingsAssistantApi(app, "fixture-token")
    capabilities = ButlerCapabilities(
        store, artifacts, project.id, settings_api=gateway
    )
    yield store, project, foreign, platform, diagnostics, gateway, capabilities
    diagnostics.close()


async def invoke(capabilities, name, arguments=None, *, change=False):
    return await capabilities.invoke(
        {"operation": f"nebula.data.{name}", "arguments": arguments or {}},
        change=change,
    )


@pytest.mark.parametrize(
    "kind",
    [
        "health",
        "runtime_usage",
        "desktop_browser",
        "execution_capabilities",
        "source_control",
        "diagnostic_settings",
        "diagnostic_files",
        "diagnostic_errors",
        "diagnostic_incidents",
        "issues",
        "workspace_reset_status",
    ],
)
async def test_butler_reads_fixed_core_metadata_without_process_or_account_actions(
    administration, kind
):
    _, _, _, _, _, _, capabilities = administration
    result = await invoke(capabilities, "system", {"kind": kind})
    assert result["read_back"] is True and result["kind"] == kind
    assert len(result["text"]) <= 4096 and len(result["sha256"]) == 64
    assert result["next_offset"] is None or result["total_characters"] > 4096


async def test_butler_terminal_egress_reports_no_terminal_without_creating_one(
    administration,
):
    _, _, _, _, _, gateway, capabilities = administration
    with pytest.raises(GuidedToolError, match="Core did not confirm"):
        (await invoke(capabilities, "system", {"kind": "terminal_public_ip"}))
    assert gateway.app.state.container_terminal_service._sessions == {}


async def test_butler_search_excludes_private_and_foreign_chats_before_pagination(
    administration,
):
    store, project, foreign, _, _, _, capabilities = administration
    for index in range(25):
        (
            await store.create(
                ChatSession(
                    provider_profile_id="fixture-provider",
                    engagement_id=project.id,
                    title="Find this private chat",
                    model="fixture",
                    metadata={"control_assistant": True},
                )
            )
        )
    for key in ("side_chat", "temporary_assistant"):
        (
            await store.create(
                ChatSession(
                    provider_profile_id="fixture-provider",
                    engagement_id=project.id,
                    title="Find this private chat",
                    model="fixture",
                    metadata={key: True},
                )
            )
        )
    (
        await store.create(
            ChatSession(
                provider_profile_id="fixture-provider",
                engagement_id=foreign.id,
                title="Find this foreign chat",
                model="fixture",
            )
        )
    )
    visible = await store.create(
        ChatSession(
            provider_profile_id="fixture-provider",
            engagement_id=project.id,
            title="Find this visible chat",
            model="fixture",
        )
    )
    note = await store.create(
        Observation(
            observation_type="note",
            engagement_id=project.id,
            title="Find this visible note",
            body="Saved source",
        )
    )

    async def read_all():
        first = await invoke(capabilities, "search_resources", {"query": "Find this"})
        part = first
        text = part["text"]
        while part["next_offset"] is not None:
            part = await invoke(
                capabilities,
                "search_resources",
                {
                    "query": "Find this",
                    "offset": part["next_offset"],
                    "expected_sha256": first["sha256"],
                },
            )
            assert part["sha256"] == first["sha256"]
            text += part["text"]
        return first, text

    result, observed_text = await read_all()
    data = json.loads(observed_text)
    assert {row["ref"]["id"] for row in data["items"]} == {visible.id, note.id}
    assert result["page_next_cursor"] is None
    assert "private chat" not in observed_text and "foreign chat" not in observed_text


async def test_butler_search_chunks_are_digest_bound_and_do_not_execute_actions(
    administration,
):
    store, project, _, _, _, _, capabilities = administration
    for index in range(20):
        (
            await store.create(
                Observation(
                    observation_type="note",
                    engagement_id=project.id,
                    title=f"Saved note {index}",
                    body="Source " * 300,
                )
            )
        )

    async def journey():
        first = await invoke(capabilities, "search_resources", {"query": "Saved note"})
        assert first["next_offset"] == 4096
        with pytest.raises(ValueError, match="first chunk"):
            await invoke(
                capabilities,
                "search_resources",
                {"query": "Saved note", "offset": 4096},
            )
        second = await invoke(
            capabilities,
            "search_resources",
            {"query": "Saved note", "offset": 4096, "expected_sha256": first["sha256"]},
        )
        assert second["sha256"] == first["sha256"]

    (await journey())
    assert (await store.count(Observation, engagement_id=project.id)) == 20


async def test_butler_harness_roster_filters_private_owners_before_paging(
    administration,
):
    store, project, foreign, _, _, _, capabilities = administration
    visible = await store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id="profile",
            model="fixture",
            display_name="Visible session",
        )
    )
    (
        await store.create(
            HarnessSession(
                engagement_id=foreign.id,
                harness_profile_id="profile",
                model="fixture",
                display_name="Foreign session",
            )
        )
    )
    for index in range(23):
        session = await store.create(
            HarnessSession(
                engagement_id=project.id,
                harness_profile_id="profile",
                model="fixture",
                display_name="Private session",
                metadata={"token": "never-returned"},
            )
        )
        (
            await store.create(
                ChatSession(
                    engagement_id=project.id,
                    title="Private harness owner",
                    harness_session_id=session.id,
                    backend="harness",
                    harness_profile_id="profile",
                    model="fixture",
                    metadata={"control_assistant": True},
                )
            )
        )
    result = await invoke(capabilities, "system", {"kind": "harness_sessions"})
    data = json.loads(result["text"])
    assert [row["id"] for row in data["sessions"]] == [visible.id]
    assert data["next_offset"] is None
    assert "never-returned" not in result["text"]


@pytest.mark.parametrize(
    "private", ["control_assistant", "side_chat", "temporary_assistant"]
)
async def test_butler_side_chat_inspection_requires_a_visible_parent(
    administration, private
):
    store, project, foreign, _, _, _, capabilities = administration
    chat = await store.create(
        ChatSession(
            provider_profile_id="fixture-provider",
            engagement_id=project.id,
            title="Private parent",
            model="fixture",
            metadata={private: True},
        )
    )
    other = await store.create(
        ChatSession(
            provider_profile_id="fixture-provider",
            engagement_id=foreign.id,
            title="Other parent",
            model="fixture",
        )
    )
    for session in (chat, other):
        with pytest.raises(ValueError, match="visible chat"):
            (
                await invoke(
                    capabilities,
                    "system",
                    {"kind": "side_chats", "session_id": session.id},
                )
            )


async def test_butler_lists_only_side_chat_labels_not_private_messages(administration):
    store, project, _, _, _, _, capabilities = administration
    parent = await store.create(
        ChatSession(
            provider_profile_id="fixture-provider",
            engagement_id=project.id,
            title="Parent",
            model="fixture",
        )
    )
    side = await store.create(
        ChatSession(
            provider_profile_id="fixture-provider",
            engagement_id=project.id,
            title="Side label",
            model="fixture",
            parent_session_id=parent.id,
            metadata={"side_chat": True, "private": "never-returned"},
        )
    )
    result = await invoke(
        capabilities, "system", {"kind": "side_chats", "session_id": parent.id}
    )
    assert json.loads(result["text"]) == [
        {
            "id": side.id,
            "title": "Side label",
            "revision": side.revision,
            "status": None,
        }
    ]
    assert "never-returned" not in result["text"]


async def test_butler_metadata_rejects_unlisted_urls_queries_and_sensitive_detail(
    administration,
):
    *_, capabilities = administration
    for arguments in (
        {"kind": "sensitive_detail"},
        {"kind": "health", "path": "https://example.invalid", "url": "secret"},
        {"kind": "diagnostic_incidents", "records": [{"error_id": "fabricated"}]},
    ):
        with pytest.raises(CapabilityInputError):
            (await invoke(capabilities, "system", arguments))


def test_butler_guide_catalog_matches_actual_named_ui_guides():
    ui = (Path(__file__).resolve().parents[2] / "ui/src/guides/catalog.ts").read_text()
    blocks = re.split(r"\n  \{\n    id: ", ui)[1:]
    expected = []
    for block in blocks:
        identity, title = re.match(r'"([^"]+)",\n    title: "([^"]+)",', block).groups()
        expected.append(
            (identity, title, len(re.findall(r"^        title:", block, re.M)))
        )
    assert list(GUIDES) == expected


async def test_butler_guide_progress_is_operator_and_revision_bound(administration):
    store, _, _, _, _, _, capabilities = administration
    operators = OperatorProfileService(store)
    first = await operators.create_profile(display_name="First guide operator")

    async def journey():
        before = await invoke(capabilities, "guides")
        assert before["operator_key"] == first.id
        args = {
            "guide_id": "agents-md",
            "operator_key": first.id,
            "status": "in_progress",
            "step_index": 1,
            "expected_revision": 0,
        }
        saved = await invoke(capabilities, "set_guide_progress", args, change=True)
        assert saved["read_back"] is True and saved["guide"]["revision"] == 1
        assert saved["client_action"]["kind"] == "guides_refresh"
        with pytest.raises(GuidedToolError):
            await invoke(capabilities, "set_guide_progress", args, change=True)
        assert (await invoke(capabilities, "guides"))["progress"][0]["step_index"] == 1
        with pytest.raises(ValueError, match="step"):
            await invoke(
                capabilities,
                "set_guide_progress",
                {**args, "step_index": 64, "expected_revision": 1},
                change=True,
            )
        second = await operators.create_profile(display_name="Second guide operator")
        (await operators.activate_profile(second.id))
        with pytest.raises(ValueError, match="operator changed"):
            await invoke(
                capabilities,
                "set_guide_progress",
                {**args, "expected_revision": 1},
                change=True,
            )
        assert (await invoke(capabilities, "guides"))["progress"] == []

    (await journey())


@pytest.mark.parametrize("kind", ["skill", "agents_md"])
async def test_butler_starter_templates_are_fixed_verified_and_never_overwrite(
    administration, kind
):
    _, project, _, platform, _, _, capabilities = administration

    async def journey():
        result = await invoke(
            capabilities,
            "create_starter_files",
            {"kind": kind, "name": "fixture-guide"},
            change=True,
        )
        assert result["read_back"] is True
        for path in result["paths"]:
            assert ((await platform.workspace_for(project.id)) / path).is_file()
        with pytest.raises(GuidedToolError):
            await invoke(
                capabilities,
                "create_starter_files",
                {"kind": kind, "name": "fixture-guide"},
                change=True,
            )
        with pytest.raises(CapabilityInputError):
            await invoke(
                capabilities,
                "create_starter_files",
                {"kind": "hook", "name": "fixture-hook"},
                change=True,
            )

    (await journey())


async def test_butler_observations_use_core_authentication(administration):
    store, project, _, _, _, gateway, _ = administration
    observer = ButlerAdministration(
        store, project.id, SettingsAssistantApi(gateway.app, "wrong-token")
    )
    with pytest.raises(GuidedToolError):
        (await observer.system({"kind": "health"}))


pytestmark = pytest.mark.anyio
