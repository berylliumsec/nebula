from __future__ import annotations

import asyncio

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import BUTLER_TOOL_NAMES, ButlerCapabilities
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_instructions import read_butler_instructions
from nebula.v3.butler_global_capabilities import ButlerGlobalActions
from nebula.v3.butler_oversight import ButlerEventPayload, _append_project_event
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
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
    utc_now,
)
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore


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
    harness_session = store.get(HarnessSession, session.harness_session_id)
    assert harness_session.metadata["runtime_options"]["reasoning_effort"] == "max"
    assert (
        harness_session.metadata["runtime_options"]["cyber_access_program"]
        == "daybreakBlue"
    )
    again = asyncio.run(actions.create_recovery_goal(arguments))
    assert again["already_exists"] is True
    assert store.get(ButlerRecoveryCase, case.id).recovery_goal_ids == [goal.id]


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
        assert guidance["input_schema"]["properties"]["delivery"]["enum"] == [
            "immediate"
        ]
        assert "delivery" in guidance["input_schema"]["required"]
        for delivery in ("steer", "next", None):
            arguments = {"session_id": own_chat.id, "text": "Check the proof gate"}
            if delivery is not None:
                arguments["delivery"] = delivery
            with pytest.raises(ValueError, match="invalid arguments"):
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
        with pytest.raises(ValueError, match="capabilities.change"):
            await capabilities.invoke(payload, change=False)
        with pytest.raises(ValueError, match="exact schema"):
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
        with pytest.raises(ValueError, match="listed operation"):
            await background.describe({"operation": "nebula.settings.set_device_theme"})
        with pytest.raises(ValueError, match="listed operation"):
            await background.describe({"operation": "nebula.work.delete_chat"})
        with pytest.raises(ValueError, match="main chat"):
            await background.invoke(
                {
                    "operation": "nebula.work.message_chat",
                    "arguments": {
                        "session_id": child.id,
                        "text": "Retry work",
                        "delivery": "next",
                    },
                },
                change=True,
            )
        assert store.get(ButlerRecoveryCase, case.id).steps == []

    asyncio.run(scenario())
