from __future__ import annotations

import asyncio

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import (
    BUTLER_TOOL_NAMES,
    ButlerCapabilities,
    _BACKGROUND_CHANGES,
)
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_instructions import read_butler_instructions
from nebula.v3.butler_global_capabilities import ButlerGlobalActions
from nebula.v3.butler_oversight import ButlerEventPayload, _append_project_event
from nebula.v3.chat_assistant_config import ChatAssistantConfigPatch
from nebula.v3.chat_goals import GoalCreate, GoalUpdate
from nebula.v3.control_assistant_work import (
    CHAT_SETTINGS_FIELDS,
    GOAL_CREATE_FIELDS,
    SET_ASSISTANT_DEFAULTS_SCHEMA,
    UPDATE_GOAL_SCHEMA,
    _ALL_WORK_TOOLS,
)
from nebula.v3.domain import (
    AssistantDefaults,
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
    ChatMessage,
    ChatRole,
    ChatSession,
    Engagement,
    ProviderProfile,
    EngagementStatus,
    ChatGoal,
    ChatGoalStatus,
    HarnessCapabilities,
    HarnessKind,
    HarnessModelOptions,
    HarnessProfile,
    HarnessRuntimeOption,
    HarnessSession,
    WorkItem,
    utc_now,
)
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_failures import FAILURE_SCHEMA, tool_failure
from nebula.v3.tools import CapabilityInputError


def _capabilities(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    own_chat = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Research agent",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    foreign_chat = store.create(
        ChatSession(
            engagement_id=other.id,
            title="Foreign agent",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    capabilities = ButlerCapabilities(
        store, artifacts, selected.id, work=app.state.control_assistant_work
    )
    return store, selected, own_chat, foreign_chat, capabilities


def _scheduled_capabilities(tmp_path):
    store, project, chat, foreign_chat, ordinary = _capabilities(tmp_path)
    project = store.update(
        Engagement,
        project.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=project.revision,
    )
    policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    folder = tmp_path / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_text("Inspect and repair Act project goals.")
    digest = read_butler_instructions(tmp_path).sha256
    with store.transaction(immediate=True) as transaction:
        event = _append_project_event(
            transaction,
            engagement_id=project.id,
            event_type="butler.global_periodic_check",
            payload=ButlerEventPayload(
                project_id=project.id,
                source="core",
                actor_role="core",
                summary="Global work check",
                metrics={"instructions_sha256": digest},
                verification_state="core_observed",
            ),
            idempotency_key="scheduled-capability-case",
            now=utc_now(),
        )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id=event.id,
            source_event_sequence=event.sequence,
            authority_revision=policy.revision,
            status="investigating",
            summary="Global work check",
        )
    )
    scheduled = ButlerCapabilities(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        project.id,
        work=ordinary._work,
        background_case_id=case.id,
        instructions_root=tmp_path,
        background_runtime_local=True,
    )
    return store, project, chat, foreign_chat, policy, case, scheduled


def test_interactive_butler_discovers_and_uses_chat_rewind(tmp_path):
    store, selected, chat, _, capabilities = _capabilities(tmp_path)
    first = store.create(
        ChatMessage(
            engagement_id=selected.id,
            session_id=chat.id,
            sequence=1,
            role=ChatRole.USER,
            content="Original question",
        )
    )
    store.create(
        ChatMessage(
            engagement_id=selected.id,
            session_id=chat.id,
            sequence=2,
            role=ChatRole.ASSISTANT,
            content="Original answer",
        )
    )

    async def scenario():
        found = await capabilities.search({"query": "rewind chat"})
        assert any(
            row["operation"] == "nebula.work.rewind_chat" for row in found["matches"]
        )
        description = await capabilities.describe(
            {"operation": "nebula.work.rewind_chat"}
        )
        assert description["invoke_with"] == "nebula.capabilities.change"
        page = await capabilities.invoke(
            {
                "operation": "nebula.work.chat_messages",
                "arguments": {"session_id": chat.id},
            },
            change=False,
        )
        assert page["messages"][0]["id"] == first.id
        changed = await capabilities.invoke(
            {
                "operation": "nebula.work.rewind_chat",
                "arguments": {
                    "session_id": chat.id,
                    "expected_revision": page["revision"],
                    "before_message_id": first.id,
                },
            },
            change=True,
        )
        assert changed["retracted_message_count"] == 2
        assert changed["revision"] > page["revision"]
        assert (
            await capabilities.invoke(
                {
                    "operation": "nebula.work.chat_messages",
                    "arguments": {"session_id": chat.id},
                },
                change=False,
            )
        )["messages"] == []

    asyncio.run(scenario())


def test_interactive_butler_prepares_project_lifecycle_with_exact_revisions(tmp_path):
    store, selected, _, _, capabilities = _capabilities(tmp_path)
    older = [store.create(Engagement(name=f"Project {index}")) for index in range(51)]

    async def scenario():
        found = await capabilities.search({"query": "archive project"})
        assert any(
            row["operation"] == "nebula.projects.archive" for row in found["matches"]
        )
        listed = await capabilities.invoke(
            {"operation": "nebula.projects.list", "arguments": {}}, change=False
        )
        assert listed["next_offset"] == 50
        second = await capabilities.invoke(
            {"operation": "nebula.projects.list", "arguments": {"offset": 50}},
            change=False,
        )
        assert any(row["project_id"] == older[-1].id for row in second["projects"])
        detail = await capabilities.invoke(
            {
                "operation": "nebula.projects.detail",
                "arguments": {"project_id": selected.id},
            },
            change=False,
        )
        assert detail["revision"] == selected.revision
        archive = await capabilities.invoke(
            {
                "operation": "nebula.projects.archive",
                "arguments": {
                    "project_id": selected.id,
                    "expected_revision": selected.revision,
                },
            },
            change=True,
        )
        assert archive["state"] == "pending_browser_apply"
        assert archive["client_action"] == {
            "kind": "project_archive",
            "project_id": selected.id,
            "expected_revision": selected.revision,
            "name": selected.name,
        }
        assert store.get(Engagement, selected.id).status == selected.status
        renamed = store.update(
            Engagement,
            selected.id,
            {"name": "Changed on another device"},
            expected_revision=selected.revision,
        )
        with pytest.raises(ValueError, match="project changed"):
            await capabilities.invoke(
                {
                    "operation": "nebula.projects.rename",
                    "arguments": {
                        "project_id": selected.id,
                        "expected_revision": selected.revision,
                        "name": "Stale name",
                    },
                },
                change=True,
            )
        rename = await capabilities.invoke(
            {
                "operation": "nebula.projects.rename",
                "arguments": {
                    "project_id": selected.id,
                    "expected_revision": renamed.revision,
                    "name": "New name",
                },
            },
            change=True,
        )
        assert rename["client_action"]["old_name"] == renamed.name
        assert store.get(Engagement, selected.id).name == renamed.name
        relink = await capabilities.invoke(
            {
                "operation": "nebula.projects.relink",
                "arguments": {
                    "project_id": selected.id,
                    "expected_revision": renamed.revision,
                },
            },
            change=True,
        )
        assert relink["client_action"] == {
            "kind": "project_relink",
            "project_id": selected.id,
            "expected_revision": renamed.revision,
            "name": renamed.name,
        }
        assert "workspace_path" not in relink["client_action"]
        create = await capabilities.invoke(
            {
                "operation": "nebula.projects.create",
                "arguments": {"name": "New project"},
            },
            change=True,
        )
        assert create["client_action"]["kind"] == "project_create"
        assert create["client_action"]["name"] == "New project"
        assert create["client_action"]["request_id"]
        linked = await capabilities.invoke(
            {
                "operation": "nebula.projects.create_linked",
                "arguments": {"name": "Linked project"},
            },
            change=True,
        )
        assert linked["client_action"]["kind"] == "project_create_linked"
        assert linked["client_action"]["name"] == "Linked project"
        assert linked["client_action"]["request_id"]
        assert "workspace_path" not in linked["client_action"]
        assert len(store.list_entities(Engagement, limit=1000)) == 53
        archived = store.update(
            Engagement,
            selected.id,
            {"status": EngagementStatus.ARCHIVED},
            expected_revision=renamed.revision,
        )
        restore = await capabilities.invoke(
            {
                "operation": "nebula.projects.restore",
                "arguments": {
                    "project_id": selected.id,
                    "expected_revision": archived.revision,
                },
            },
            change=True,
        )
        assert restore["client_action"]["kind"] == "project_restore"
        delete = await capabilities.invoke(
            {
                "operation": "nebula.projects.delete",
                "arguments": {
                    "project_id": selected.id,
                    "expected_revision": archived.revision,
                },
            },
            change=True,
        )
        assert delete["client_action"]["kind"] == "project_delete"
        assert store.get(Engagement, selected.id).status == EngagementStatus.ARCHIVED

    asyncio.run(scenario())


def test_interactive_butler_prepares_device_chat_export_without_transcript(tmp_path):
    store, selected, own_chat, foreign_chat, capabilities = _capabilities(tmp_path)
    private = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Private side chat",
            provider_profile_id=own_chat.provider_profile_id,
            model="model-a",
            metadata={"side_chat": True},
        )
    )

    async def scenario():
        found = await capabilities.search({"query": "export chat transcript"})
        assert any(
            row["operation"] == "nebula.chats.export" for row in found["matches"]
        )
        described = await capabilities.describe({"operation": "nebula.chats.export"})
        assert described["invoke_with"] == "nebula.capabilities.change"
        prepared = await capabilities.invoke(
            {
                "operation": "nebula.chats.export",
                "arguments": {
                    "session_id": own_chat.id,
                    "expected_revision": own_chat.revision,
                },
            },
            change=True,
        )
        assert prepared["state"] == "pending_browser_apply"
        assert prepared["client_action"] == {
            "kind": "chat_export",
            "project_id": selected.id,
            "session_id": own_chat.id,
            "expected_revision": own_chat.revision,
        }
        assert "messages" not in prepared and "transcript" not in prepared
        assert store.get(ChatSession, own_chat.id).revision == own_chat.revision
        for target in (foreign_chat, private):
            with pytest.raises(ValueError, match="saved main chat"):
                await capabilities.invoke(
                    {
                        "operation": "nebula.chats.export",
                        "arguments": {
                            "session_id": target.id,
                            "expected_revision": target.revision,
                        },
                    },
                    change=True,
                )
        store.update(
            ChatSession,
            own_chat.id,
            {"title": "Changed on another device"},
            expected_revision=own_chat.revision,
        )
        with pytest.raises(ValueError, match="chat changed"):
            await capabilities.invoke(
                {
                    "operation": "nebula.chats.export",
                    "arguments": {
                        "session_id": own_chat.id,
                        "expected_revision": own_chat.revision,
                    },
                },
                change=True,
            )

    asyncio.run(scenario())


def test_scheduled_butler_cannot_discover_interactive_project_mutations(tmp_path):
    _, _, _, _, _, _, capabilities = _scheduled_capabilities(tmp_path)

    async def scenario():
        assert "projects" not in [
            row["id"] for row in (await capabilities.search({}))["categories"]
        ]
        assert not any(
            row["operation"].startswith("nebula.projects.")
            for row in (await capabilities.search({"query": "archive project"}))[
                "matches"
            ]
        )
        assert not any(
            row["operation"] == "nebula.chats.export"
            for row in (await capabilities.search({"query": "export chat transcript"}))[
                "matches"
            ]
        )
        assert not any(
            row["operation"].startswith("nebula.library.")
            for row in (await capabilities.search({"query": "shared library"}))[
                "matches"
            ]
        )
        with pytest.raises(CapabilityInputError, match="listed operation"):
            await capabilities.invoke(
                {"operation": "nebula.projects.archive", "arguments": {}}, change=True
            )
        with pytest.raises(CapabilityInputError, match="listed operation"):
            await capabilities.invoke(
                {
                    "operation": "nebula.projects.create_linked",
                    "arguments": {"name": "No"},
                },
                change=True,
            )

    asyncio.run(scenario())


def test_scheduled_butler_renews_goal_budget_after_more_than_40_reads(tmp_path):
    store, project, chat, foreign_chat, policy, case, scheduled = (
        _scheduled_capabilities(tmp_path)
    )
    goal = store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=chat.id,
            objective="Finish the research",
            completion_criteria=["Evidence saved"],
            status=ChatGoalStatus.PAUSED,
            elapsed_seconds=14_401,
            time_budget_seconds=14_400,
            current_step=3,
            step_budget=3,
        )
    )

    async def scenario():
        for _ in range(42):
            result = await scheduled.invoke(
                {"operation": "nebula.global.projects", "arguments": {}},
                change=False,
            )
            assert result["projects"][0]["id"] == project.id
        updated = await scheduled.invoke(
            {
                "operation": "nebula.work.update_goal",
                "arguments": {
                    "session_id": chat.id,
                    "expected_revision": goal.revision,
                    "time_budget_seconds": 18_000,
                    "step_budget": 10,
                },
            },
            change=True,
        )
        assert updated["goal"]["time_budget_seconds"] == 18_000
        assert updated["goal"]["step_budget"] == 10
        with pytest.raises(ValueError, match="Core-owned goal"):
            await scheduled.invoke(
                {
                    "operation": "nebula.work.update_goal",
                    "arguments": {
                        "session_id": foreign_chat.id,
                        "expected_revision": goal.revision,
                        "step_budget": 11,
                    },
                },
                change=True,
            )
        resumed = await scheduled.invoke(
            {
                "operation": "nebula.global.goal_action",
                "arguments": {
                    "project_id": project.id,
                    "session_id": chat.id,
                    "expected_authority_revision": policy.revision,
                    "expected_revision": updated["goal"]["revision"],
                    "action": "resume",
                },
            },
            change=True,
        )
        assert resumed["goal"]["status"] == "running"
        paused = await scheduled.invoke(
            {
                "operation": "nebula.work.goal_action",
                "arguments": {
                    "session_id": chat.id,
                    "expected_revision": resumed["goal"]["revision"],
                    "action": "pause",
                    "reason": "Synthetic pause",
                },
            },
            change=True,
        )
        assert paused["goal"]["status"] == "paused"

    asyncio.run(scenario())
    assert len(store.get(ButlerRecoveryCase, case.id).steps) == 45


def test_scheduled_butler_can_edit_all_assistant_settings(tmp_path):
    assert {
        name for name, _, _, read_only in _ALL_WORK_TOOLS if not read_only
    } <= _BACKGROUND_CHANGES
    assert set(CHAT_SETTINGS_FIELDS) == set(ChatAssistantConfigPatch.model_fields)
    assert set(GOAL_CREATE_FIELDS) == set(GoalCreate.model_fields)
    assert set(UPDATE_GOAL_SCHEMA["properties"]) == {
        "session_id",
        *GoalUpdate.model_fields,
    }
    assert set(
        SET_ASSISTANT_DEFAULTS_SCHEMA["properties"]["settings"]["properties"]
    ) == set(AssistantDefaults.model_fields)
    store, project, chat, _, policy, _, scheduled = _scheduled_capabilities(tmp_path)

    async def scenario():
        defaults = await scheduled.invoke(
            {
                "operation": "nebula.global.assistant_defaults",
                "arguments": {"project_id": project.id},
            },
            change=False,
        )
        changed_defaults = await scheduled.invoke(
            {
                "operation": "nebula.global.set_assistant_defaults",
                "arguments": {
                    "project_id": project.id,
                    "expected_authority_revision": policy.revision,
                    "expected_revision": defaults["revision"],
                    "settings": {"allow_agent_messaging": False},
                },
            },
            change=True,
        )
        assert changed_defaults["settings"]["allow_agent_messaging"] is False
        current = await scheduled.invoke(
            {
                "operation": "nebula.global.chat_settings",
                "arguments": {"project_id": project.id, "session_id": chat.id},
            },
            change=False,
        )
        changed = await scheduled.invoke(
            {
                "operation": "nebula.global.set_chat_settings",
                "arguments": {
                    "project_id": project.id,
                    "expected_authority_revision": policy.revision,
                    "session_id": chat.id,
                    "expected_revision": current["revision"],
                    "settings": {
                        "include_knowledge": False,
                        "planning_enabled": True,
                    },
                },
            },
            change=True,
        )
        assert changed["settings"]["include_knowledge"] is False
        assert changed["settings"]["planning_enabled"] is True
        with pytest.raises(ValueError, match="changed"):
            await scheduled.invoke(
                {
                    "operation": "nebula.global.set_chat_settings",
                    "arguments": {
                        "project_id": project.id,
                        "expected_authority_revision": policy.revision,
                        "session_id": chat.id,
                        "expected_revision": current["revision"],
                        "settings": {"planning_enabled": False},
                    },
                },
                change=True,
            )

    asyncio.run(scenario())
    assert (
        store.get(Engagement, project.id).assistant_defaults.allow_agent_messaging
        is False
    )


def test_scheduled_butler_can_reach_every_project_assistant_work_action(tmp_path):
    store, project, chat, foreign_chat, policy, _, scheduled = _scheduled_capabilities(
        tmp_path
    )

    async def scenario():
        catalog = await scheduled.invoke(
            {
                "operation": "nebula.global.work_catalog",
                "arguments": {"project_id": project.id},
            },
            change=False,
        )
        assert set(catalog["actions"]) == {
            name for name, *_ in _ALL_WORK_TOOLS if name != "nebula.work.catalog"
        }
        details = await scheduled.invoke(
            {
                "operation": "nebula.global.work_catalog",
                "arguments": {
                    "project_id": project.id,
                    "operation": "nebula.work.update_chat",
                },
            },
            change=False,
        )
        assert details["parameters"]["required"] == [
            "session_id",
            "expected_revision",
        ]
        current = await scheduled.invoke(
            {
                "operation": "nebula.global.work_read",
                "arguments": {
                    "project_id": project.id,
                    "operation": "nebula.work.chat",
                    "arguments": {"session_id": chat.id},
                },
            },
            change=False,
        )
        with pytest.raises(ValueError, match="Invalid Assistant work arguments"):
            await scheduled.invoke(
                {
                    "operation": "nebula.global.work_change",
                    "arguments": {
                        "project_id": project.id,
                        "expected_authority_revision": policy.revision,
                        "operation": "nebula.work.update_chat",
                        "arguments": {"session_id": chat.id, "title": "Edited"},
                    },
                },
                change=True,
            )
        renamed = await scheduled.invoke(
            {
                "operation": "nebula.global.work_change",
                "arguments": {
                    "project_id": project.id,
                    "expected_authority_revision": policy.revision,
                    "operation": "nebula.work.update_chat",
                    "arguments": {
                        "session_id": chat.id,
                        "expected_revision": current["revision"],
                        "title": "Edited by scheduled Butler",
                    },
                },
            },
            change=True,
        )
        assert renamed["title"] == "Edited by scheduled Butler"
        assert store.get(ChatSession, chat.id).title == renamed["title"]
        with pytest.raises(ValueError, match="Act and privacy scope"):
            await scheduled.invoke(
                {
                    "operation": "nebula.global.work_change",
                    "arguments": {
                        "project_id": foreign_chat.engagement_id,
                        "expected_authority_revision": policy.revision,
                        "operation": "nebula.work.update_chat",
                        "arguments": {
                            "session_id": foreign_chat.id,
                            "expected_revision": foreign_chat.revision,
                            "title": "Should not change",
                        },
                    },
                },
                change=True,
            )

    asyncio.run(scenario())


def test_scheduled_recovery_goal_is_typed_linked_and_uses_daybreak_blue(tmp_path):
    store, selected, own_chat, _, ordinary = _capabilities(tmp_path)
    selected = store.update(
        Engagement,
        selected.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=selected.revision,
    )
    policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id(selected.id), engagement_id=selected.id, authority="act"
        )
    )
    profile = store.create(
        HarnessProfile(
            name="Recovery Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-6-sol",
            privacy={"local_only": True},
            capabilities=HarnessCapabilities(
                models=["gpt-6-sol"],
                model_options=[
                    HarnessModelOptions(
                        model="gpt-6-sol",
                        reasoning_efforts=[HarnessRuntimeOption(id="max", label="Max")],
                        cyber_access_programs=["daybreakBlue"],
                    ),
                ],
            ),
        )
    )
    source = store.create(
        ChatGoal(
            engagement_id=selected.id,
            session_id=own_chat.id,
            objective="Continue existing work",
            completion_criteria=["Report result"],
            status=ChatGoalStatus.PAUSED,
        )
    )
    folder = tmp_path / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_text("Recover paused goals with evidence.")
    digest = read_butler_instructions(tmp_path).sha256
    with store.transaction(immediate=True) as transaction:
        event = _append_project_event(
            transaction,
            engagement_id=selected.id,
            event_type="butler.global_periodic_check",
            payload=ButlerEventPayload(
                project_id=selected.id,
                source="core",
                actor_role="core",
                summary="Global work check",
                metrics={"instructions_sha256": digest},
                verification_state="core_observed",
            ),
            idempotency_key="recovery-goal-case",
            now=utc_now(),
        )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=selected.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id=event.id,
            source_event_sequence=event.sequence,
            authority_revision=policy.revision,
            status="investigating",
            summary="Global work check",
        )
    )
    actions = ButlerGlobalActions(
        store, ordinary._work, tmp_path, case.id, runtime_local=True
    )

    async def measured_quota(_project_id, arguments):
        assert arguments == {"profile_id": profile.id, "refresh": True}
        return {"state": "available", "snapshots": [{"remaining_percent": 75.0}]}

    ordinary._work.harness_quota = measured_quota
    arguments = {
        "project_id": selected.id,
        "source_goal_id": source.id,
        "expected_project_revision": selected.revision,
        "expected_authority_revision": policy.revision,
        "objective": "Unblock the existing goal",
        "completion_criteria": ["Report the safe resume path"],
        "plan": ["Inspect the paused goal", "Report evidence to Butler"],
        "start": False,
    }
    created = asyncio.run(actions.create_recovery_goal(arguments))
    assert created["created"] is True
    goal = store.get(ChatGoal, created["goal"]["id"])
    assert goal.metadata == {
        "butler_recovery_case_id": case.id,
        "butler_recovery_source_goal_id": source.id,
    }
    assert goal.plan == arguments["plan"]
    assert store.get(ButlerRecoveryCase, case.id).recovery_goal_ids == [goal.id]
    session = store.get(ChatSession, goal.session_id)
    assert session.harness_profile_id == profile.id and session.model == "gpt-6-sol"
    assert session.metadata["butler_initiated"] is True
    harness_session = store.get(HarnessSession, session.harness_session_id)
    assert harness_session.metadata["runtime_options"]["reasoning_effort"] == "max"
    assert (
        harness_session.metadata["runtime_options"]["cyber_access_program"]
        == "daybreakBlue"
    )
    again = asyncio.run(actions.create_recovery_goal(arguments))
    assert again["already_exists"] is True
    assert store.get(ButlerRecoveryCase, case.id).recovery_goal_ids == [goal.id]


def test_scheduled_recovery_selects_available_quota_over_butler_profile(tmp_path):
    store, selected, _, _, ordinary = _capabilities(tmp_path)
    options = HarnessCapabilities(
        models=["gpt-6-sol"],
        model_options=[
            HarnessModelOptions(
                model="gpt-6-sol",
                reasoning_efforts=[HarnessRuntimeOption(id="max", label="Max")],
                cyber_access_programs=["daybreakBlue"],
            )
        ],
    )
    preferred = store.create(
        HarnessProfile(
            name="Butler Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-6-sol",
            privacy={"local_only": True},
            capabilities=options,
        )
    )
    alternate = store.create(
        HarnessProfile(
            name="Available Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-6-sol",
            privacy={"local_only": True},
            capabilities=options,
        )
    )
    butler_harness_session = store.create(
        HarnessSession(
            engagement_id=selected.id,
            harness_profile_id=preferred.id,
            model="gpt-6-sol",
        )
    )
    store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Butler",
            backend="harness",
            harness_profile_id=preferred.id,
            harness_session_id=butler_harness_session.id,
            model="gpt-6-sol",
            metadata={"control_assistant": True},
        )
    )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=selected.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="synthetic-event",
            source_event_sequence=1,
            authority_revision=1,
            status="investigating",
            summary="Global work check",
        )
    )
    actions = ButlerGlobalActions(
        store, ordinary._work, tmp_path, case.id, runtime_local=True
    )

    async def measured_quota(_project_id, arguments):
        return {
            "state": "available",
            "snapshots": [
                {
                    "remaining_percent": (
                        0.0 if arguments["profile_id"] == preferred.id else 100.0
                    )
                }
            ],
        }

    ordinary._work.harness_quota = measured_quota
    assert asyncio.run(actions._recovery_harness(selected.id)).id == alternate.id

    async def credits_after_included_limit(_project_id, arguments):
        return {
            "state": "available",
            "ordinary_usage_allowed": False,
            "snapshots": [{"remaining_percent": 0.0}],
            "balances": [
                {"remaining": 4.0 if arguments["profile_id"] == preferred.id else 0.0}
            ],
        }

    ordinary._work.harness_quota = credits_after_included_limit
    assert asyncio.run(actions._recovery_harness(selected.id)).id == preferred.id

    async def unavailable_quota(_project_id, _arguments):
        return {"state": "unavailable", "snapshots": []}

    ordinary._work.harness_quota = unavailable_quota
    with pytest.raises(ValueError, match="confirmed included usage or credits"):
        asyncio.run(actions._recovery_harness(selected.id))


def test_global_butler_chooses_act_projects_and_deduplicates_new_work(
    tmp_path, monkeypatch
):
    store, selected, own_chat, foreign_chat, ordinary = _capabilities(tmp_path)
    other = store.get(Engagement, foreign_chat.engagement_id)
    selected = store.update(
        Engagement,
        selected.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=selected.revision,
    )
    other = store.update(
        Engagement,
        other.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=other.revision,
    )
    selected_policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id(selected.id), engagement_id=selected.id, authority="act"
        )
    )
    other_policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id(other.id), engagement_id=other.id, authority="act"
        )
    )
    folder = tmp_path / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_text("Choose work from all Act projects.")
    digest = read_butler_instructions(tmp_path).sha256
    with store.transaction(immediate=True) as transaction:
        event = _append_project_event(
            transaction,
            engagement_id=selected.id,
            event_type="butler.global_periodic_check",
            payload=ButlerEventPayload(
                project_id=selected.id,
                source="core",
                actor_role="core",
                summary="Global work check",
                metrics={"instructions_sha256": digest},
                verification_state="core_observed",
            ),
            idempotency_key="test-global-case",
            now=utc_now(),
        )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=selected.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id=event.id,
            source_event_sequence=event.sequence,
            authority_revision=selected_policy.revision,
            status="investigating",
            summary="Global work check",
        )
    )
    global_butler = ButlerCapabilities(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        selected.id,
        work=ordinary._work,
        background_case_id=case.id,
        instructions_root=tmp_path,
        background_runtime_local=True,
    )

    async def scenario():
        guidance = await global_butler.describe(
            {"operation": "nebula.work.message_chat"}
        )
        assert "delivery=immediate" in guidance["description"]
        assert "Busy Grok turns cannot be steered" in guidance["description"]
        assert guidance["input_schema"]["properties"]["delivery"]["enum"] == [
            "immediate"
        ]
        assert "delivery" in guidance["input_schema"]["required"]
        for delivery in ("steer", "next", None):
            arguments = {"session_id": own_chat.id, "text": "Check the proof gate"}
            if delivery is not None:
                arguments["delivery"] = delivery
            with pytest.raises(CapabilityInputError, match="invalid arguments"):
                await global_butler.invoke(
                    {"operation": "nebula.work.message_chat", "arguments": arguments},
                    change=True,
                )
        assert store.get(ButlerRecoveryCase, case.id).steps == []

        async def deliver(project_id, arguments, *, idempotency_key):
            assert project_id == selected.id
            assert arguments["session_id"] == own_chat.id
            assert arguments["delivery"] == "immediate"
            assert idempotency_key.startswith(f"recovery:{case.id}:")
            return {
                "session_id": own_chat.id,
                "delivery": "started",
                "turn_id": "turn-1",
            }

        monkeypatch.setattr(ordinary._work, "message_chat", deliver)
        receipt = await global_butler.invoke(
            {
                "operation": "nebula.work.message_chat",
                "arguments": {
                    "session_id": own_chat.id,
                    "text": "Check the proof gate",
                    "delivery": "immediate",
                },
            },
            change=True,
        )
        assert receipt["delivery"] == "started"
        saved_case = store.get(ButlerRecoveryCase, case.id)
        assert saved_case.steps[-1]["status"] == "applied"
        assert saved_case.delivered_turn_id == "turn-1"
        assert saved_case.dialogue_pending_turns == [
            {
                "project_id": selected.id,
                "session_id": own_chat.id,
                "turn_id": "turn-1",
                "guidance": "Check the proof gate",
            }
        ]

        store.update(
            ButlerRecoveryCase,
            case.id,
            {
                "dialogue_pending_turns": [],
                "dialogue_results": [
                    {
                        "project_id": selected.id,
                        "session_id": own_chat.id,
                        "turn_id": "turn-1",
                        "status": "complete",
                    }
                ],
            },
            expected_revision=saved_case.revision,
        )
        reply = {
            "session_id": own_chat.id,
            "turn_id": "turn-1",
            "turn_status": "complete",
        }

        async def read_reply(project_id, arguments):
            assert project_id == selected.id
            assert arguments["turn_id"] == "turn-1"
            return dict(reply)

        monkeypatch.setattr(ordinary._work, "await_chat_result", read_reply)
        await global_butler.invoke(
            {
                "operation": "nebula.work.await_chat_result",
                "arguments": {"session_id": own_chat.id, "turn_id": "turn-1"},
            },
            change=False,
        )
        assert store.get(ButlerRecoveryCase, case.id).dialogue_read_turn_ids == []
        reply["saved_reply"] = "I am blocked on the trace."
        await global_butler.invoke(
            {
                "operation": "nebula.work.await_chat_result",
                "arguments": {"session_id": own_chat.id, "turn_id": "turn-1"},
            },
            change=False,
        )
        assert store.get(ButlerRecoveryCase, case.id).dialogue_read_turn_ids == [
            "turn-1"
        ]

        listed = await global_butler.invoke(
            {"operation": "nebula.global.projects", "arguments": {}}, change=False
        )
        assert {item["id"] for item in listed["projects"]} == {
            selected.id,
            other.id,
        }

        async def quota(project_id, arguments):
            assert project_id == selected.id
            assert arguments["profile_id"] == "grok"
            return {"state": "available", "snapshots": [{"remaining_percent": 19}]}

        monkeypatch.setattr(ordinary._work, "harness_quota", quota)
        quota_matches = await global_butler.search({"query": "Grok remaining quota"})
        assert "nebula.global.harness_quota" in {
            item["operation"] for item in quota_matches["matches"]
        }
        quota_result = await global_butler.invoke(
            {
                "operation": "nebula.global.harness_quota",
                "arguments": {"project_id": selected.id, "profile_id": "grok"},
            },
            change=False,
        )
        assert quota_result["snapshots"][0]["remaining_percent"] == 19
        handoff_matches = await global_butler.search(
            {"query": "handoff a paused harness planning goal"}
        )
        assert "nebula.global.handoff_goal" in {
            item["operation"] for item in handoff_matches["matches"]
        }
        handoff_description = await global_butler.describe(
            {"operation": "nebula.global.handoff_goal"}
        )
        assert "expected_revision" in handoff_description["input_schema"]["required"]
        child_args = {
            "parent_project_id": selected.id,
            "expected_parent_revision": selected.revision,
            "expected_authority_revision": selected_policy.revision,
            "name": "New investigation",
        }
        first = await global_butler.invoke(
            {
                "operation": "nebula.global.create_child_project",
                "arguments": child_args,
            },
            change=True,
        )
        second = await global_butler.invoke(
            {
                "operation": "nebula.global.create_child_project",
                "arguments": child_args,
            },
            change=True,
        )
        assert first["created"] is True and second["created"] is False
        assert first["project"]["id"] == second["project"]["id"]
        child = store.get(Engagement, first["project"]["id"])
        assert child.parent_engagement_id == selected.id
        assert child.workspace_path is None
        assert child.status == EngagementStatus.ACTIVE
        assert (
            store.find_entity(ButlerAuthorityPolicy, "engagement_id", child.id) is None
        )

        goal_args = {
            "project_id": other.id,
            "expected_project_revision": other.revision,
            "expected_authority_revision": other_policy.revision,
            "backend": "provider",
            "profile_id": own_chat.provider_profile_id,
            "model": own_chat.model,
            "planning_enabled": True,
            "objective": "Map the files",
            "completion_criteria": ["Report saved"],
        }
        created = await global_butler.invoke(
            {"operation": "nebula.global.create_goal", "arguments": goal_args},
            change=True,
        )
        duplicate = await global_butler.invoke(
            {"operation": "nebula.global.create_goal", "arguments": goal_args},
            change=True,
        )
        assert created["goal"]["status"] == "draft"
        assert (
            store.get(ChatSession, created["session_id"]).metadata["planning_enabled"]
            is True
        )
        assert duplicate["already_exists"] is True
        assert store.count_entities(ChatGoal, engagement_id=other.id) == 1

        paused = store.update(
            Engagement,
            other.id,
            {"status": EngagementStatus.PAUSED},
            expected_revision=other.revision,
        )
        resumed = await global_butler.invoke(
            {
                "operation": "nebula.global.resume_project",
                "arguments": {
                    "project_id": other.id,
                    "expected_project_revision": paused.revision,
                    "expected_authority_revision": other_policy.revision,
                },
            },
            change=True,
        )
        assert resumed["project"]["status"] == "active"

        draft = store.update(
            Engagement,
            other.id,
            {"status": EngagementStatus.DRAFT},
            expected_revision=store.get(Engagement, other.id).revision,
        )
        matches = await global_butler.search({"query": "activate draft project"})
        assert "nebula.global.resume_project" in {
            item["operation"] for item in matches["matches"]
        }
        description = await global_butler.describe(
            {"operation": "nebula.global.resume_project"}
        )
        assert "draft or paused" in description["description"]
        activated = await global_butler.invoke(
            {
                "operation": "nebula.global.resume_project",
                "arguments": {
                    "project_id": other.id,
                    "expected_project_revision": draft.revision,
                    "expected_authority_revision": other_policy.revision,
                },
            },
            change=True,
        )
        assert activated["project"]["status"] == "active"
        assert activated["resumed"] is True

        async def deliver_other(project_id, arguments, *, idempotency_key=None):
            assert project_id == other.id
            assert arguments["session_id"] == foreign_chat.id
            return {
                "session_id": foreign_chat.id,
                "delivery": "started",
                "turn_id": "turn-other",
            }

        monkeypatch.setattr(ordinary._work, "message_chat", deliver_other)
        await global_butler.invoke(
            {
                "operation": "nebula.global.work_change",
                "arguments": {
                    "project_id": other.id,
                    "expected_authority_revision": other_policy.revision,
                    "operation": "nebula.work.message_chat",
                    "arguments": {
                        "session_id": foreign_chat.id,
                        "text": "What is blocking the trace?",
                        "delivery": "immediate",
                    },
                },
            },
            change=True,
        )
        assert store.get(ButlerRecoveryCase, case.id).dialogue_pending_turns == [
            {
                "project_id": other.id,
                "session_id": foreign_chat.id,
                "turn_id": "turn-other",
                "guidance": "What is blocking the trace?",
            }
        ]

        (folder / "BUTLER.md").write_text("Changed instructions.")
        with pytest.raises(ValueError, match="changed during this check"):
            await global_butler.invoke(
                {
                    "operation": "nebula.global.create_child_project",
                    "arguments": {**child_args, "name": "Forbidden after edit"},
                },
                change=True,
            )
        assert not any(
            item.name == "Forbidden after edit"
            for item in store.iter_readable_entities(Engagement)
        )

    asyncio.run(scenario())


def test_butler_discovers_chat_control_without_advertising_every_schema(tmp_path):
    _, _, own_chat, _, capabilities = _capabilities(tmp_path)
    assert {plugin.spec.name for plugin in capabilities.plugins()} == BUTLER_TOOL_NAMES

    async def scenario():
        found = await capabilities.search({"query": "send a message to a chat"})
        assert "nebula.work.message_chat" in {
            item["operation"] for item in found["matches"]
        }
        for query, operation in (
            ("fork a chat", "nebula.work.fork_chat"),
            ("delete a chat", "nebula.work.delete_chat"),
            ("retry failed chat turn", "nebula.work.retry_chat_turn"),
        ):
            matches = await capabilities.search({"query": query})
            assert operation in {item["operation"] for item in matches["matches"]}
            exact = await capabilities.describe({"operation": operation})
            assert exact["invoke_with"] == "nebula.capabilities.change"
        approvals = await capabilities.search({"query": "approve an agent request"})
        assert approvals["matches"][0]["operation"] == "nebula.work.decide_approval"
        assert approvals["next_offset"] is not None
        next_page = await capabilities.search(
            {
                "query": "approve an agent request",
                "offset": approvals["next_offset"],
            }
        )
        assert next_page["matches"]
        assert next_page["matches"][0] != approvals["matches"][0]
        described = await capabilities.describe(
            {"operation": "nebula.work.message_chat"}
        )
        assert described["invoke_with"] == "nebula.capabilities.change"
        assert "session_id" in described["input_schema"]["properties"]
        chat = await capabilities.invoke(
            {"operation": "nebula.work.chat", "arguments": {"session_id": own_chat.id}},
            change=False,
        )
        assert chat["title"] == "Research agent"
        assert chat["kind"] == "main"

    asyncio.run(scenario())


def test_butler_discovers_and_uses_chat_priority(tmp_path):
    store, _, chat, _, capabilities = _capabilities(tmp_path)

    async def scenario():
        for query, operation in (
            ("page older chats", "nebula.work.chats"),
            ("pin and flag a chat", "nebula.work.update_chat"),
        ):
            found = await capabilities.search({"query": query})
            assert operation in {item["operation"] for item in found["matches"]}
        described = await capabilities.describe(
            {"operation": "nebula.work.update_chat"}
        )
        assert described["invoke_with"] == "nebula.capabilities.change"
        assert {"pinned", "flag_color"}.issubset(
            described["input_schema"]["properties"]
        )
        saved = await capabilities.invoke(
            {
                "operation": "nebula.work.update_chat",
                "arguments": {
                    "session_id": chat.id,
                    "expected_revision": chat.revision,
                    "pinned": True,
                    "flag_color": "blue",
                },
            },
            change=True,
        )
        assert saved["pinned"] is True
        assert saved["flag_color"] == "blue"
        with pytest.raises(CapabilityInputError, match="invalid arguments"):
            await capabilities.invoke(
                {
                    "operation": "nebula.work.update_chat",
                    "arguments": {
                        "session_id": chat.id,
                        "expected_revision": saved["revision"],
                        "flag_color": "magenta",
                    },
                },
                change=True,
            )

    asyncio.run(scenario())
    assert store.get(ChatSession, chat.id).metadata["pinned"] is True
    assert store.get(ChatSession, chat.id).metadata["flag_color"] == "blue"


def test_butler_discovers_and_uses_project_work_board(tmp_path):
    store, project, _, _, capabilities = _capabilities(tmp_path)

    async def scenario():
        found = await capabilities.search({"query": "create a Work board item"})
        assert "nebula.work.create_work_item" in {
            item["operation"] for item in found["matches"]
        }
        described = await capabilities.describe(
            {"operation": "nebula.work.create_work_item"}
        )
        assert described["invoke_with"] == "nebula.capabilities.change"
        assert "request_id" in described["input_schema"]["required"]
        created = await capabilities.invoke(
            {
                "operation": "nebula.work.create_work_item",
                "arguments": {"title": "Review launch", "request_id": "launch-1"},
            },
            change=True,
        )
        item_id = created["item"]["id"]
        read = await capabilities.invoke(
            {"operation": "nebula.work.work_item", "arguments": {"item_id": item_id}},
            change=False,
        )
        assert read["item"]["title"] == "Review launch"
        assert read["item"]["revision"] == created["item"]["revision"]
        checked = await capabilities.invoke(
            {
                "operation": "nebula.work.check_in_work_item",
                "arguments": {
                    "item_id": item_id,
                    "expected_revision": read["item"]["revision"],
                    "summary": "Ready for review",
                    "status": "review",
                    "request_id": "launch-check-1",
                },
            },
            change=True,
        )
        assert checked["status"] == "review"
        setting_matches = await capabilities.search(
            {"query": "disable Work agent tools"}
        )
        assert "nebula.work.set_work_setting" in {
            item["operation"] for item in setting_matches["matches"]
        }
        setting = await capabilities.invoke(
            {"operation": "nebula.work.work_setting", "arguments": {}},
            change=False,
        )
        assert setting["enabled"] is True
        setting_schema = await capabilities.describe(
            {"operation": "nebula.work.set_work_setting"}
        )
        assert setting_schema["invoke_with"] == "nebula.capabilities.change"
        disabled = await capabilities.invoke(
            {
                "operation": "nebula.work.set_work_setting",
                "arguments": {
                    "enabled": False,
                    "expected_revision": setting["revision"],
                },
            },
            change=True,
        )
        assert disabled["saved"] is True and disabled["enabled"] is False
        updates = await capabilities.invoke(
            {
                "operation": "nebula.work.work_updates",
                "arguments": {"item_id": item_id},
            },
            change=False,
        )
        assert checked["update_id"] in {update["id"] for update in updates["updates"]}
        update = await capabilities.invoke(
            {
                "operation": "nebula.work.work_update",
                "arguments": {"item_id": item_id, "update_id": checked["update_id"]},
            },
            change=False,
        )
        assert update["update"]["summary"] == "Ready for review"
        with pytest.raises(CapabilityInputError, match="invalid arguments"):
            await capabilities.invoke(
                {
                    "operation": "nebula.work.check_in_work_item",
                    "arguments": {
                        "item_id": item_id,
                        "expected_revision": checked["revision"],
                        "summary": "Invalid status",
                        "status": "invented",
                        "request_id": "launch-check-2",
                    },
                },
                change=True,
            )
        return item_id

    item_id = asyncio.run(scenario())
    assert store.get(WorkItem, item_id).engagement_id == project.id
    assert store.get(WorkItem, item_id).status == "review"


def test_butler_unlisted_operation_receipt_gives_exact_recovery(tmp_path):
    _, _, _, _, capabilities = _capabilities(tmp_path)
    spec = next(
        plugin.spec
        for plugin in capabilities.plugins()
        if plugin.spec.name == "nebula.capabilities.read"
    )
    attempted = {
        "operation": "work.list",
        "arguments": {"project_name": "private-project"},
    }

    async def scenario():
        with pytest.raises(CapabilityInputError) as caught:
            await capabilities.invoke(attempted, change=False)
        return caught.value

    error = asyncio.run(scenario())
    assert error.reason == "unlisted_operation"
    assert error._nebula_before_execution is True
    failure = tool_failure(spec, attempted, error, phase="before_execution")
    assert failure["schema"] == FAILURE_SCHEMA
    assert failure["category"] == "invalid_arguments"
    assert failure["invalid_input"] == "operation"
    assert failure["side_effects"] == "none"
    assert failure["retry_safe"] is True
    assert failure["problem"] == (
        "The operation is not in this Butler's capability catalog; no action ran."
    )
    assert failure["next_action"] == (
        "Do not repeat this operation. Call nebula.capabilities.search with the task intent, "
        "then nebula.capabilities.describe for a returned operation. Invoke it with "
        "the described arguments and invoke_with tool. For Work items, use the "
        "separate work_list tool if it is offered. If no matching action or tool "
        "is available, report that fresh state could not be read."
    )
    assert "work.list" not in str(failure)
    assert "private-project" not in str(failure)
    assert failure["effective_input_schema"] == spec.input_schema


def test_butler_pre_dispatch_errors_name_the_repair(tmp_path):
    _, _, own_chat, _, capabilities = _capabilities(tmp_path)
    specs = {plugin.spec.name: plugin.spec for plugin in capabilities.plugins()}

    async def scenario():
        cases = [
            (
                "nebula.capabilities.change",
                {
                    "operation": "nebula.work.chat",
                    "arguments": {"session_id": own_chat.id},
                },
                True,
                "use_read",
                "operation",
                "nebula.capabilities.read",
            ),
            (
                "nebula.capabilities.read",
                {"operation": "nebula.work.update_chat", "arguments": {}},
                False,
                "use_change",
                "operation",
                "nebula.capabilities.change",
            ),
            (
                "nebula.capabilities.read",
                {"operation": "nebula.work.chat", "arguments": {}},
                False,
                "invalid_action_arguments",
                "arguments",
                "input_schema",
            ),
        ]
        for tool, attempted, change, reason, field, guidance in cases:
            with pytest.raises(CapabilityInputError) as caught:
                await capabilities.invoke(attempted, change=change)
            assert caught.value.reason == reason
            failure = tool_failure(
                specs[tool], attempted, caught.value, phase="before_execution"
            )
            assert failure["category"] == "invalid_arguments"
            assert failure["invalid_input"] == field
            assert failure["side_effects"] == "none"
            assert guidance in failure["next_action"]

    asyncio.run(scenario())


def test_butler_change_routes_validate_schema_revision_and_project(tmp_path):
    store, _, own_chat, foreign_chat, capabilities = _capabilities(tmp_path)

    async def scenario():
        operation = "nebula.work.update_chat"
        payload = {
            "operation": operation,
            "arguments": {
                "session_id": own_chat.id,
                "expected_revision": own_chat.revision,
                "title": "Sources review",
            },
        }
        with pytest.raises(CapabilityInputError, match="capabilities.change"):
            await capabilities.invoke(payload, change=False)
        with pytest.raises(CapabilityInputError, match="exact schema"):
            await capabilities.invoke(
                {"operation": operation, "arguments": {"session_id": own_chat.id}},
                change=True,
            )
        with pytest.raises(ValueError, match="project's work roster"):
            await capabilities.invoke(
                {
                    "operation": "nebula.work.chat",
                    "arguments": {"session_id": foreign_chat.id},
                },
                change=False,
            )
        saved = await capabilities.invoke(payload, change=True)
        assert saved["title"] == "Sources review"
        assert saved["butler_operation"] == operation
        assert saved["butler_project_id"] == own_chat.engagement_id
        assert store.get(ChatSession, own_chat.id).title == "Sources review"
        with pytest.raises(ValueError, match="current revision"):
            await capabilities.invoke(payload, change=True)

    asyncio.run(scenario())


def test_butler_browser_action_is_not_reported_as_saved_state(tmp_path):
    _, _, _, _, capabilities = _capabilities(tmp_path)

    async def scenario():
        described = await capabilities.describe(
            {"operation": "nebula.settings.set_device_theme"}
        )
        assert described["invoke_with"] == "nebula.capabilities.change"
        result = await capabilities.invoke(
            {
                "operation": "nebula.settings.set_device_theme",
                "arguments": {"theme": "light"},
            },
            change=True,
        )
        assert result["state"] == "pending_browser_apply"
        assert result["butler_operation"] == "nebula.settings.set_device_theme"

    asyncio.run(scenario())


def test_background_act_excludes_browser_and_child_chat_changes(tmp_path):
    store, selected, parent, _, ordinary = _capabilities(tmp_path)
    policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id(selected.id), engagement_id=selected.id, authority="act"
        )
    )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=selected.id,
            trigger="immediate",
            signal="goal.blocked",
            source_event_id="event-1",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="investigating",
            summary="Subagent failed",
        )
    )
    child = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Child",
            parent_session_id=parent.id,
            provider_profile_id=parent.provider_profile_id,
            model=parent.model,
        )
    )
    background = ButlerCapabilities(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        selected.id,
        work=ordinary._work,
        background_case_id=case.id,
    )

    async def scenario():
        with pytest.raises(CapabilityInputError, match="listed operation"):
            await background.describe({"operation": "nebula.settings.set_device_theme"})
        with pytest.raises(CapabilityInputError, match="listed operation"):
            await background.describe({"operation": "nebula.work.rewind_chat"})
        assert (await background.describe({"operation": "nebula.work.chat_messages"}))[
            "invoke_with"
        ] == "nebula.capabilities.read"
        assert (await background.describe({"operation": "nebula.work.delete_chat"}))[
            "invoke_with"
        ] == "nebula.capabilities.change"
        with pytest.raises(ValueError, match="main chat"):
            await background.invoke(
                {
                    "operation": "nebula.work.message_chat",
                    "arguments": {
                        "session_id": child.id,
                        "text": "Retry work",
                        "delivery": "immediate",
                    },
                },
                change=True,
            )
        assert store.get(ButlerRecoveryCase, case.id).steps == []

    asyncio.run(scenario())
