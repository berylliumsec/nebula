from __future__ import annotations
from nebula.v3.async_utils import async_any
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import threading
from datetime import timedelta

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
    McpServerProfile,
    McpTransport,
    StorageSource,
    PairedDeviceSession,
    WorkItem,
    utc_now,
)
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_failures import FAILURE_SCHEMA, tool_failure
from nebula.v3.tools import CapabilityInputError


async def _capabilities(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    selected = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    provider = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    own_chat = await store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Research agent",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    foreign_chat = await store.create(
        ChatSession(
            engagement_id=other.id,
            title="Foreign agent",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    capabilities = await ButlerCapabilities.create(
        store, artifacts, selected.id, work=app.state.control_assistant_work
    )
    return store, selected, own_chat, foreign_chat, capabilities


async def _scheduled_capabilities(tmp_path):
    store, project, chat, foreign_chat, ordinary = await _capabilities(tmp_path)
    project = await store.update(
        Engagement,
        project.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=project.revision,
    )
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    folder = tmp_path / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_text("Inspect and repair Act project goals.")
    digest = read_butler_instructions(tmp_path).sha256
    async with store.transaction(immediate=True) as transaction:
        event = await _append_project_event(
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
    case = await store.create(
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
    scheduled = await ButlerCapabilities.create(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        project.id,
        work=ordinary._work,
        background_case_id=case.id,
        instructions_root=tmp_path,
        background_runtime_local=True,
    )
    return store, project, chat, foreign_chat, policy, case, scheduled


async def test_global_create_goal_exposes_and_saves_harness_max_effort(tmp_path):
    store, project, _, _, policy, _, butler = await _scheduled_capabilities(tmp_path)
    profile = await store.create(
        HarnessProfile(
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-6-sol",
            capabilities=HarnessCapabilities(
                models=["gpt-6-sol"],
                model_options=[
                    HarnessModelOptions(
                        model="gpt-6-sol",
                        reasoning_efforts=[HarnessRuntimeOption(id="max", label="Max")],
                    )
                ],
            ),
        )
    )

    async def scenario():
        described = await butler.describe({"operation": "nebula.global.create_goal"})
        fields = described["input_schema"]["properties"]
        assert "harness_reasoning_effort" in fields
        assert "max" not in fields["reasoning_effort"]["enum"]
        created = await butler.invoke(
            {
                "operation": "nebula.global.create_goal",
                "arguments": {
                    "project_id": project.id,
                    "expected_project_revision": project.revision,
                    "expected_authority_revision": policy.revision,
                    "backend": "harness",
                    "profile_id": profile.id,
                    "model": "gpt-6-sol",
                    "harness_reasoning_effort": "max",
                    "objective": "Verify archive byte reachability",
                    "completion_criteria": ["Report the observed byte path"],
                },
            },
            change=True,
        )
        assert created["goal"]["status"] == "draft"
        session = await store.get(ChatSession, created["session_id"])
        harness = await store.get(HarnessSession, session.harness_session_id)
        assert harness.metadata["runtime_options"]["reasoning_effort"] == "max"

    (await scenario())


async def test_global_chat_settings_batch_avoids_catalog_and_stays_project_bound(
    tmp_path, monkeypatch
):
    store, project, chat, foreign_chat, _, _, scheduled = await _scheduled_capabilities(
        tmp_path
    )
    second = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Second agent",
            provider_profile_id=chat.provider_profile_id,
            model=chat.model,
        )
    )
    original = store.find_entities

    async def without_catalog(model, *args, **kwargs):
        if model is McpServerProfile:
            raise AssertionError("batch read must not load the global MCP catalog")
        return await original(model, *args, **kwargs)

    monkeypatch.setattr(store, "find_entities", without_catalog)

    async def scenario():
        result = await scheduled.invoke(
            {
                "operation": "nebula.global.chat_settings_many",
                "arguments": {
                    "project_id": project.id,
                    "session_ids": [chat.id, second.id],
                },
            },
            change=False,
        )
        assert [item["session_id"] for item in result["chats"]] == [
            chat.id,
            second.id,
        ]
        assert result["chats"][0]["revision"] == chat.revision
        assert result["chats"][0]["settings"]["model"] == chat.model
        assert all("available" not in item for item in result["chats"])
        with pytest.raises(ValueError, match="work roster"):
            await scheduled.invoke(
                {
                    "operation": "nebula.global.chat_settings_many",
                    "arguments": {
                        "project_id": project.id,
                        "session_ids": [foreign_chat.id],
                    },
                },
                change=False,
            )

    (await scenario())


async def test_global_chat_settings_read_runs_outside_event_loop(tmp_path, monkeypatch):
    _, project, chat, _, _, _, scheduled = await _scheduled_capabilities(tmp_path)
    read_threads = []

    def read_settings(project_id, session_id):
        read_threads.append(threading.get_ident())
        return {"session_id": session_id}

    monkeypatch.setattr(scheduled._work, "chat_settings", read_settings)

    async def scenario():
        loop_thread = threading.get_ident()
        result = await scheduled.invoke(
            {
                "operation": "nebula.global.chat_settings",
                "arguments": {"project_id": project.id, "session_id": chat.id},
            },
            change=False,
        )
        assert result["session_id"] == chat.id
        assert read_threads and read_threads[0] != loop_thread

    (await scenario())


async def test_interactive_butler_discovers_and_uses_chat_rewind(tmp_path):
    store, selected, chat, _, capabilities = await _capabilities(tmp_path)
    first = await store.create(
        ChatMessage(
            engagement_id=selected.id,
            session_id=chat.id,
            sequence=1,
            role=ChatRole.USER,
            content="Original question",
        )
    )
    (
        await store.create(
            ChatMessage(
                engagement_id=selected.id,
                session_id=chat.id,
                sequence=2,
                role=ChatRole.ASSISTANT,
                content="Original answer",
            )
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

    (await scenario())


async def test_interactive_butler_prepares_project_lifecycle_with_exact_revisions(
    tmp_path,
):
    store, selected, _, _, capabilities = await _capabilities(tmp_path)
    older = [
        (await store.create(Engagement(name=f"Project {index}"))) for index in range(51)
    ]

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
        assert (await store.get(Engagement, selected.id)).status == selected.status
        renamed = await store.update(
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
        assert (await store.get(Engagement, selected.id)).name == renamed.name
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
        assert len((await store.list_entities(Engagement, limit=1000))) == 53
        archived = await store.update(
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
        assert (
            await store.get(Engagement, selected.id)
        ).status == EngagementStatus.ARCHIVED

    (await scenario())


async def test_interactive_butler_prepares_device_chat_export_without_transcript(
    tmp_path,
):
    store, selected, own_chat, foreign_chat, capabilities = await _capabilities(
        tmp_path
    )
    private = await store.create(
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
        assert (await store.get(ChatSession, own_chat.id)).revision == own_chat.revision
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
        (
            await store.update(
                ChatSession,
                own_chat.id,
                {"title": "Changed on another device"},
                expected_revision=own_chat.revision,
            )
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

    (await scenario())


async def test_interactive_butler_prepares_scoped_storage_token_handoffs(tmp_path):
    store, selected, _, foreign_chat, capabilities = await _capabilities(tmp_path)
    own = await store.create(
        StorageSource(
            name="Fixture NAS",
            engagement_id=selected.id,
            profile_id="runtime",
            model="model-a",
            token_sha256="a" * 64,
            callback_credential_ref="env:STORAGE_CALLBACK_TOKEN",
        )
    )
    foreign = await store.create(
        StorageSource(
            name="Other source",
            engagement_id=foreign_chat.engagement_id,
            profile_id="runtime",
            model="model-a",
            token_sha256="b" * 64,
        )
    )

    async def scenario():
        found = await capabilities.search({"query": "storage source token"})
        assert {
            "nebula.storage.sources",
            "nebula.storage.rotate",
            "nebula.storage.revoke",
            "nebula.storage.configure_reporting",
            "nebula.storage.edit_callbacks",
        }.issubset({row["operation"] for row in found["matches"]})
        listed = await capabilities.invoke(
            {"operation": "nebula.storage.sources", "arguments": {}}, change=False
        )
        assert [item["source_id"] for item in listed["sources"]] == [own.id]
        assert "token" not in str(listed).lower()
        assert "credential" not in str(listed).lower()
        with pytest.raises(ValueError, match="selected project"):
            await capabilities.invoke(
                {
                    "operation": "nebula.storage.source",
                    "arguments": {"source_id": foreign.id},
                },
                change=False,
            )
        enrolled = await capabilities.invoke(
            {"operation": "nebula.storage.enroll", "arguments": {}}, change=True
        )
        assert enrolled["client_action"] == {
            "kind": "storage_enroll",
            "project_id": selected.id,
        }
        for operation, kind in (
            ("rotate", "storage_rotate"),
            ("revoke", "storage_revoke"),
            ("edit_callbacks", "storage_edit_callbacks"),
        ):
            prepared = await capabilities.invoke(
                {
                    "operation": f"nebula.storage.{operation}",
                    "arguments": {
                        "source_id": own.id,
                        "expected_revision": own.revision,
                    },
                },
                change=True,
            )
            assert prepared["client_action"] == {
                "kind": kind,
                "project_id": selected.id,
                "source_id": own.id,
                "expected_revision": own.revision,
                "name": own.name,
            }
            assert (await store.get(StorageSource, own.id)).revision == own.revision
            with pytest.raises(ValueError, match="changed"):
                await capabilities.invoke(
                    {
                        "operation": f"nebula.storage.{operation}",
                        "arguments": {
                            "source_id": own.id,
                            "expected_revision": own.revision + 1,
                        },
                    },
                    change=True,
                )
            with pytest.raises(ValueError, match="selected project"):
                await capabilities.invoke(
                    {
                        "operation": f"nebula.storage.{operation}",
                        "arguments": {
                            "source_id": foreign.id,
                            "expected_revision": foreign.revision,
                        },
                    },
                    change=True,
                )
        reporting = await capabilities.invoke(
            {
                "operation": "nebula.storage.configure_reporting",
                "arguments": {
                    "source_id": own.id,
                    "expected_revision": own.revision,
                    "enabled": True,
                    "reporting_interval_seconds": 3600,
                },
            },
            change=True,
        )
        assert reporting["client_action"]["changes"] == {
            "enabled": True,
            "reporting_interval_seconds": 3600,
        }
        assert "credential" not in str(reporting).lower()
        assert (await store.get(StorageSource, own.id)).revision == own.revision
        with pytest.raises(CapabilityInputError):
            await capabilities.invoke(
                {
                    "operation": "nebula.storage.configure_reporting",
                    "arguments": {
                        "source_id": own.id,
                        "expected_revision": own.revision,
                    },
                },
                change=True,
            )

    (await scenario())


async def test_interactive_butler_prepares_nonsecret_device_handoffs(tmp_path):
    store, selected, _, _, capabilities = await _capabilities(tmp_path)
    now = utc_now()
    device = await store.create(
        PairedDeviceSession(
            name="Fixture phone",
            token_sha256="a" * 64,
            csrf_sha256="b" * 64,
            idle_expires_at=now + timedelta(days=30),
            absolute_expires_at=now + timedelta(days=90),
        )
    )

    async def scenario():
        found = await capabilities.search({"query": "pair or revoke a device"})
        assert {"nebula.devices.pair", "nebula.devices.revoke"}.issubset(
            {row["operation"] for row in found["matches"]}
        )
        listed = await capabilities.invoke(
            {"operation": "nebula.devices.list", "arguments": {}}, change=False
        )
        assert [item["device_id"] for item in listed["devices"]] == [device.id]
        assert "token" not in str(listed).lower()
        assert "csrf" not in str(listed).lower()
        paired = await capabilities.invoke(
            {"operation": "nebula.devices.pair", "arguments": {}}, change=True
        )
        assert paired["client_action"] == {
            "kind": "device_pair",
            "project_id": selected.id,
        }
        assert (
            len((await store.find_entities(PairedDeviceSession, {"revoked_at": None})))
            == 1
        )
        created_at = listed["devices"][0]["created_at"]
        revoke = await capabilities.invoke(
            {
                "operation": "nebula.devices.revoke",
                "arguments": {
                    "device_id": device.id,
                    "expected_created_at": created_at,
                },
            },
            change=True,
        )
        assert revoke["client_action"] == {
            "kind": "device_revoke",
            "project_id": selected.id,
            "device_id": device.id,
            "name": device.name,
            "expected_created_at": created_at,
        }
        assert (await store.get(PairedDeviceSession, device.id)).revoked_at is None
        with pytest.raises(ValueError, match="changed"):
            await capabilities.invoke(
                {
                    "operation": "nebula.devices.revoke",
                    "arguments": {
                        "device_id": device.id,
                        "expected_created_at": "2000-01-01T00:00:00Z",
                    },
                },
                change=True,
            )

    (await scenario())


async def test_interactive_butler_prepares_write_only_credential_handoffs(tmp_path):
    _, selected, _, _, capabilities = await _capabilities(tmp_path)
    reference = "session:" + "a" * 32

    async def scenario():
        found = await capabilities.search({"query": "create or delete credential"})
        assert {"nebula.credentials.create", "nebula.credentials.delete"}.issubset(
            {row["operation"] for row in found["matches"]}
        )
        created = await capabilities.invoke(
            {"operation": "nebula.credentials.create", "arguments": {}}, change=True
        )
        assert created["client_action"] == {
            "kind": "credential_create",
            "project_id": selected.id,
        }
        assert "secret" not in str(created).lower()
        deleted = await capabilities.invoke(
            {
                "operation": "nebula.credentials.delete",
                "arguments": {"reference": reference},
            },
            change=True,
        )
        assert deleted["client_action"] == {
            "kind": "credential_delete",
            "project_id": selected.id,
            "reference": reference,
        }
        with pytest.raises(CapabilityInputError):
            await capabilities.invoke(
                {
                    "operation": "nebula.credentials.delete",
                    "arguments": {"reference": "env:NEBULA_TOKEN"},
                },
                change=True,
            )

    (await scenario())


async def test_interactive_butler_prepares_exact_provider_credential_attachment(
    tmp_path,
):
    store, selected, _, _, capabilities = await _capabilities(tmp_path)
    profile = next(
        item
        for item in (await store.list_entities(ProviderProfile, limit=10))
        if item.name == "Local"
    )
    management = await store.create(
        ProviderProfile(name="OpenRouter", provider_type="openrouter")
    )
    reference = "session:" + "a" * 32

    async def scenario():
        found = await capabilities.search({"query": "attach credential to provider"})
        assert {
            "nebula.providers.list",
            "nebula.providers.profile",
            "nebula.providers.attach_credential",
        }.issubset({row["operation"] for row in found["matches"]})
        listed = await capabilities.invoke(
            {"operation": "nebula.providers.list", "arguments": {"offset": 0}},
            change=False,
        )
        assert listed["providers"][0]["provider_id"] == profile.id
        assert "secret_ref" not in str(listed)
        attached = await capabilities.invoke(
            {
                "operation": "nebula.providers.attach_credential",
                "arguments": {
                    "provider_id": profile.id,
                    "expected_revision": profile.revision,
                    "credential_ref": reference,
                    "slot": "inference",
                },
            },
            change=True,
        )
        assert attached["client_action"] == {
            "kind": "provider_attach_credential",
            "project_id": selected.id,
            "provider_id": profile.id,
            "name": profile.name,
            "expected_revision": profile.revision,
            "credential_ref": reference,
            "slot": "inference",
        }
        assert (await store.get(ProviderProfile, profile.id)).secret_ref is None
        management_action = await capabilities.invoke(
            {
                "operation": "nebula.providers.attach_credential",
                "arguments": {
                    "provider_id": management.id,
                    "expected_revision": management.revision,
                    "credential_ref": reference,
                    "slot": "management",
                },
            },
            change=True,
        )
        assert management_action["client_action"]["slot"] == "management"
        assert (
            await store.get(ProviderProfile, management.id)
        ).openrouter_management_secret_ref is None
        for changes in (
            {"expected_revision": profile.revision + 1},
            {"slot": "management"},
            {"credential_ref": "env:NEBULA_KEY"},
        ):
            with pytest.raises((ValueError, CapabilityInputError)):
                await capabilities.invoke(
                    {
                        "operation": "nebula.providers.attach_credential",
                        "arguments": {
                            "provider_id": profile.id,
                            "expected_revision": profile.revision,
                            "credential_ref": reference,
                            "slot": "inference",
                            **changes,
                        },
                    },
                    change=True,
                )

    (await scenario())


async def test_interactive_butler_prepares_exact_harness_credential_attachment(
    tmp_path,
):
    store, selected, _, _, capabilities = await _capabilities(tmp_path)
    harness = await store.create(
        HarnessProfile(name="SDK", kind=HarnessKind.CLAUDE_AGENT_SDK)
    )
    grok = await store.create(
        HarnessProfile(name="Grok", kind=HarnessKind.GROK_ACP, executable="/bin/true")
    )
    reference = "session:" + "b" * 32

    async def scenario():
        found = await capabilities.search({"query": "attach credential to harness"})
        assert {
            "nebula.harnesses.list",
            "nebula.harnesses.harness",
            "nebula.harnesses.attach_credential",
        }.issubset({row["operation"] for row in found["matches"]})
        listed = await capabilities.invoke(
            {"operation": "nebula.harnesses.list", "arguments": {}}, change=False
        )
        assert any(item["harness_id"] == harness.id for item in listed["harnesses"])
        assert "secret_ref" not in str(listed)
        attached = await capabilities.invoke(
            {
                "operation": "nebula.harnesses.attach_credential",
                "arguments": {
                    "harness_id": harness.id,
                    "expected_revision": harness.revision,
                    "credential_ref": reference,
                },
            },
            change=True,
        )
        assert attached["client_action"] == {
            "kind": "harness_attach_credential",
            "project_id": selected.id,
            "harness_id": harness.id,
            "name": harness.name,
            "expected_revision": harness.revision,
            "credential_ref": reference,
            "auth_mode": "secret_ref",
        }
        assert (await store.get(HarnessProfile, harness.id)).secret_ref is None
        for invalid in (
            {
                "harness_id": harness.id,
                "expected_revision": harness.revision + 1,
                "credential_ref": reference,
            },
            {
                "harness_id": grok.id,
                "expected_revision": grok.revision,
                "credential_ref": reference,
            },
            {
                "harness_id": harness.id,
                "expected_revision": harness.revision,
                "credential_ref": "env:KEY",
            },
        ):
            with pytest.raises((ValueError, CapabilityInputError)):
                await capabilities.invoke(
                    {
                        "operation": "nebula.harnesses.attach_credential",
                        "arguments": invalid,
                    },
                    change=True,
                )

    (await scenario())


async def test_interactive_butler_prepares_exact_mcp_credential_attachment(tmp_path):
    store, selected, other, _, capabilities = await _capabilities(tmp_path)
    foreign = await store.create(
        McpServerProfile(
            name="foreign",
            transport=McpTransport.STREAMABLE_HTTP,
            url="https://example.test/other",
            metadata={"engagement_id": other.id},
        )
    )
    for number in range(49):
        (
            await store.create(
                McpServerProfile(
                    name=f"foreign-{number}",
                    transport=McpTransport.STREAMABLE_HTTP,
                    url="https://example.test/other",
                    metadata={"engagement_id": other.id},
                )
            )
        )
    http = await store.create(
        McpServerProfile(
            name="remote",
            transport=McpTransport.STREAMABLE_HTTP,
            url="https://example.test/mcp",
        )
    )
    stdio = await store.create(
        McpServerProfile(
            name="local", transport=McpTransport.STDIO, command="/bin/true"
        )
    )
    reference = "session:" + "b" * 32

    async def scenario():
        found = await capabilities.search({"query": "attach credential to MCP server"})
        assert {
            "nebula.mcp.list",
            "nebula.mcp.server",
            "nebula.mcp.attach_credential",
        }.issubset({row["operation"] for row in found["matches"]})
        listed = await capabilities.invoke(
            {"operation": "nebula.mcp.list", "arguments": {}}, change=False
        )
        assert any(row["server_id"] == http.id for row in listed["servers"])
        assert not any(row["server_id"] == foreign.id for row in listed["servers"])
        assert "secret_ref" not in str(listed)
        with pytest.raises(ValueError, match="selected project"):
            await capabilities.invoke(
                {
                    "operation": "nebula.mcp.server",
                    "arguments": {"server_id": foreign.id},
                },
                change=False,
            )
        prepared = await capabilities.invoke(
            {
                "operation": "nebula.mcp.attach_credential",
                "arguments": {
                    "server_id": http.id,
                    "expected_revision": http.revision,
                    "credential_ref": reference,
                    "slot": "header",
                    "key": "X-API-Key",
                },
            },
            change=True,
        )
        assert prepared["client_action"] == {
            "kind": "mcp_attach_credential",
            "project_id": selected.id,
            "server_id": http.id,
            "name": "remote",
            "transport": "streamable_http",
            "expected_revision": http.revision,
            "credential_ref": reference,
            "slot": "header",
            "key": "X-API-Key",
        }
        assert (await store.get(McpServerProfile, http.id)).header_secret_refs == {}
        for server, slot, key in (
            (http, "environment", "TOKEN"),
            (stdio, "header", "X-API-Key"),
            (http, "header", "bad header"),
            (foreign, "bearer", None),
        ):
            with pytest.raises(ValueError):
                arguments = {
                    "server_id": server.id,
                    "expected_revision": server.revision,
                    "credential_ref": reference,
                    "slot": slot,
                }
                if key is not None:
                    arguments["key"] = key
                await capabilities.invoke(
                    {
                        "operation": "nebula.mcp.attach_credential",
                        "arguments": arguments,
                    },
                    change=True,
                )

    (await scenario())


async def test_interactive_butler_prepares_device_owned_diagnostics_export(tmp_path):
    _, selected, _, _, capabilities = await _capabilities(tmp_path)

    async def scenario():
        found = await capabilities.search({"query": "export diagnostics support zip"})
        assert "nebula.diagnostics.export" in {
            row["operation"] for row in found["matches"]
        }
        prepared = await capabilities.invoke(
            {"operation": "nebula.diagnostics.export", "arguments": {}}, change=True
        )
        assert prepared["client_action"] == {
            "kind": "diagnostics_export",
            "project_id": selected.id,
        }
        assert "zip" not in str(prepared).lower()
        with pytest.raises(CapabilityInputError):
            await capabilities.invoke(
                {
                    "operation": "nebula.diagnostics.export",
                    "arguments": {"include_database": True},
                },
                change=True,
            )

    (await scenario())


async def test_scheduled_butler_cannot_discover_interactive_project_mutations(tmp_path):
    _, _, _, _, _, _, capabilities = await _scheduled_capabilities(tmp_path)

    async def scenario():
        assert not any(
            row["operation"].startswith("nebula.notes.")
            for row in (await capabilities.search({"query": "project notes"}))[
                "matches"
            ]
        )
        assert "projects" not in [
            row["id"] for row in (await capabilities.search({}))["categories"]
        ]
        assert "devices" not in [
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

        for operation in (
            "nebula.assets.create",
            "nebula.findings.create",
            "nebula.findings.update",
            "nebula.sources.add",
            "nebula.sources.reindex",
            "nebula.sources.remove",
        ):
            with pytest.raises(CapabilityInputError, match="listed operation"):
                await capabilities.invoke(
                    {"operation": operation, "arguments": {}}, change=True
                )

    (await scenario())


async def test_scheduled_butler_renews_goal_budget_after_more_than_40_reads(tmp_path):
    (
        store,
        project,
        chat,
        foreign_chat,
        policy,
        case,
        scheduled,
    ) = await _scheduled_capabilities(tmp_path)
    goal = await store.create(
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

    (await scenario())
    assert len((await store.get(ButlerRecoveryCase, case.id)).steps) == 45


async def test_scheduled_butler_can_edit_all_assistant_settings(tmp_path):
    assert {
        "nebula.work.set_chat_settings",
        "nebula.work.set_assistant_defaults",
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
    store, project, chat, _, policy, _, scheduled = await _scheduled_capabilities(
        tmp_path
    )

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

    (await scenario())
    assert (
        await store.get(Engagement, project.id)
    ).assistant_defaults.allow_agent_messaging is False


async def test_scheduled_butler_can_reach_every_project_assistant_work_action(tmp_path):
    (
        store,
        project,
        chat,
        foreign_chat,
        policy,
        _,
        scheduled,
    ) = await _scheduled_capabilities(tmp_path)

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
        assert (await store.get(ChatSession, chat.id)).title == renamed["title"]
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

    (await scenario())


async def test_scheduled_recovery_goal_is_typed_linked_and_uses_daybreak_blue(tmp_path):
    store, selected, own_chat, _, ordinary = await _capabilities(tmp_path)
    selected = await store.update(
        Engagement,
        selected.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=selected.revision,
    )
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(selected.id), engagement_id=selected.id, authority="act"
        )
    )
    profile = await store.create(
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
    source = await store.create(
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
    async with store.transaction(immediate=True) as transaction:
        event = await _append_project_event(
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
    case = await store.create(
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
    created = await actions.create_recovery_goal(arguments)
    assert created["created"] is True
    goal = await store.get(ChatGoal, created["goal"]["id"])
    assert goal.metadata == {
        "butler_recovery_case_id": case.id,
        "butler_recovery_source_goal_id": source.id,
    }
    assert goal.plan == arguments["plan"]
    assert (await store.get(ButlerRecoveryCase, case.id)).recovery_goal_ids == [goal.id]
    session = await store.get(ChatSession, goal.session_id)
    assert session.harness_profile_id == profile.id and session.model == "gpt-6-sol"
    assert session.metadata["butler_initiated"] is True
    harness_session = await store.get(HarnessSession, session.harness_session_id)
    assert harness_session.metadata["runtime_options"]["reasoning_effort"] == "max"
    assert (
        harness_session.metadata["runtime_options"]["cyber_access_program"]
        == "daybreakBlue"
    )
    again = await actions.create_recovery_goal(arguments)
    assert again["already_exists"] is True
    assert (await store.get(ButlerRecoveryCase, case.id)).recovery_goal_ids == [goal.id]


async def test_scheduled_recovery_selects_available_quota_over_butler_profile(tmp_path):
    store, selected, _, _, ordinary = await _capabilities(tmp_path)
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
    preferred = await store.create(
        HarnessProfile(
            name="Butler Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-6-sol",
            privacy={"local_only": True},
            capabilities=options,
        )
    )
    alternate = await store.create(
        HarnessProfile(
            name="Available Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-6-sol",
            privacy={"local_only": True},
            capabilities=options,
        )
    )
    butler_harness_session = await store.create(
        HarnessSession(
            engagement_id=selected.id,
            harness_profile_id=preferred.id,
            model="gpt-6-sol",
        )
    )
    (
        await store.create(
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
    )
    case = await store.create(
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
    assert (await actions._recovery_harness(selected.id)).id == alternate.id

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
    assert (await actions._recovery_harness(selected.id)).id == preferred.id

    async def unavailable_quota(_project_id, _arguments):
        return {"state": "unavailable", "snapshots": []}

    ordinary._work.harness_quota = unavailable_quota
    with pytest.raises(ValueError, match="confirmed included usage or credits"):
        (await actions._recovery_harness(selected.id))


async def test_global_butler_chooses_act_projects_and_deduplicates_new_work(
    tmp_path, monkeypatch
):
    store, selected, own_chat, foreign_chat, ordinary = await _capabilities(tmp_path)
    other = await store.get(Engagement, foreign_chat.engagement_id)
    selected = await store.update(
        Engagement,
        selected.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=selected.revision,
    )
    other = await store.update(
        Engagement,
        other.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=other.revision,
    )
    selected_policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(selected.id), engagement_id=selected.id, authority="act"
        )
    )
    other_policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(other.id), engagement_id=other.id, authority="act"
        )
    )
    folder = tmp_path / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_text("Choose work from all Act projects.")
    digest = read_butler_instructions(tmp_path).sha256
    async with store.transaction(immediate=True) as transaction:
        event = await _append_project_event(
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
    case = await store.create(
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
    global_butler = await ButlerCapabilities.create(
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
        assert (await store.get(ButlerRecoveryCase, case.id)).steps == []

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
        saved_case = await store.get(ButlerRecoveryCase, case.id)
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

        (
            await store.update(
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
        assert (
            await store.get(ButlerRecoveryCase, case.id)
        ).dialogue_read_turn_ids == []
        reply["saved_reply"] = "I am blocked on the trace."
        await global_butler.invoke(
            {
                "operation": "nebula.work.await_chat_result",
                "arguments": {"session_id": own_chat.id, "turn_id": "turn-1"},
            },
            change=False,
        )
        assert (
            await store.get(ButlerRecoveryCase, case.id)
        ).dialogue_read_turn_ids == ["turn-1"]

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
        child = await store.get(Engagement, first["project"]["id"])
        assert child.parent_engagement_id == selected.id
        assert child.workspace_path is None
        assert child.status == EngagementStatus.ACTIVE
        assert (
            await store.find_entity(ButlerAuthorityPolicy, "engagement_id", child.id)
        ) is None

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
        assert (await store.get(ChatSession, created["session_id"])).metadata[
            "planning_enabled"
        ] is True
        assert duplicate["already_exists"] is True
        assert (await store.count_entities(ChatGoal, engagement_id=other.id)) == 1

        paused = await store.update(
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

        draft = await store.update(
            Engagement,
            other.id,
            {"status": EngagementStatus.DRAFT},
            expected_revision=(await store.get(Engagement, other.id)).revision,
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
        assert (
            await store.get(ButlerRecoveryCase, case.id)
        ).dialogue_pending_turns == [
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
        assert not (
            await async_any(
                item.name == "Forbidden after edit"
                async for item in store.iter_readable_entities(Engagement)
            )
        )

    (await scenario())


async def test_butler_discovers_chat_control_without_advertising_every_schema(tmp_path):
    _, _, own_chat, _, capabilities = await _capabilities(tmp_path)
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

    (await scenario())


async def test_butler_discovers_and_uses_chat_priority(tmp_path):
    store, _, chat, _, capabilities = await _capabilities(tmp_path)

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

    (await scenario())
    assert (await store.get(ChatSession, chat.id)).metadata["pinned"] is True
    assert (await store.get(ChatSession, chat.id)).metadata["flag_color"] == "blue"


async def test_butler_discovers_and_uses_project_work_board(tmp_path):
    store, project, _, _, capabilities = await _capabilities(tmp_path)

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

    item_id = await scenario()
    assert (await store.get(WorkItem, item_id)).engagement_id == project.id
    assert (await store.get(WorkItem, item_id)).status == "review"


async def test_butler_unlisted_operation_receipt_gives_exact_recovery(tmp_path):
    _, _, _, _, capabilities = await _capabilities(tmp_path)
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

    error = await scenario()
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


async def test_butler_pre_dispatch_errors_name_the_repair(tmp_path):
    _, _, own_chat, _, capabilities = await _capabilities(tmp_path)
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

    (await scenario())


async def test_butler_change_routes_validate_schema_revision_and_project(tmp_path):
    store, _, own_chat, foreign_chat, capabilities = await _capabilities(tmp_path)

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
        assert (await store.get(ChatSession, own_chat.id)).title == "Sources review"
        with pytest.raises(ValueError, match="current revision"):
            await capabilities.invoke(payload, change=True)

    (await scenario())


async def test_butler_browser_action_is_not_reported_as_saved_state(tmp_path):
    _, _, _, _, capabilities = await _capabilities(tmp_path)

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

    (await scenario())


async def test_background_act_excludes_browser_and_child_chat_changes(tmp_path):
    store, selected, parent, _, ordinary = await _capabilities(tmp_path)
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(selected.id), engagement_id=selected.id, authority="act"
        )
    )
    case = await store.create(
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
    child = await store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Child",
            parent_session_id=parent.id,
            provider_profile_id=parent.provider_profile_id,
            model=parent.model,
        )
    )
    background = await ButlerCapabilities.create(
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
        assert (await store.get(ButlerRecoveryCase, case.id)).steps == []

    (await scenario())


pytestmark = pytest.mark.anyio
