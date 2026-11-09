"""Actual ACP requests against inert retained sessions; no live account."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pytest
from nebula.v3.chat import ChatHistoryConflict
from nebula.v3.credentials import CredentialStore
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    ChatDecision,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
)
from nebula.v3.harnesses import (
    GrokAcpAdapter,
    HarnessRuntimeService,
    HarnessTransportError,
)
from nebula.v3.storage import NebulaStore

RULE = "SYNTHETIC_RULE_ALPHA: Only use the fixture cluster."
EDITED_RULE = "SYNTHETIC_RULE_BETA: Only use the updated fixture cluster."


class RetainedAcp:
    """Model system-head retention from grok-build2bdd1d6a, not a vendor proof."""

    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.fail_once = None
        self.cancel_new = False
        self.new_started = asyncio.Event()
        self.home = None
        self.ignore_rules = False

    def persist_head(self, external_id):
        path = (
            self.home
            / "sessions"
            / "synthetic-workspace"
            / external_id
            / "system_prompt.txt"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.sessions[external_id]["system"], encoding="utf-8")

    def compact(self, external_id):
        self.sessions[external_id]["history"] = ["Lossy summary without instructions."]

    def effective(self, external_id):
        state = self.sessions[external_id]
        return state["system"] + "\n" + "\n".join(state["history"])


class RetainedAcpRpc:
    connection_state = "connected"

    def __init__(self, vendor):
        self.vendor = vendor
        self.events = asyncio.Queue()

    async def request(self, method, params):
        vendor = self.vendor
        vendor.calls.append((method, deepcopy(params)))
        if method == vendor.fail_once:
            vendor.fail_once = None
            raise HarnessTransportError("Synthetic rejection before acceptance")
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentInfo": {"name": "fixture", "version": "1.0.50"},
                "agentCapabilities": {"loadSession": True},
            }
        if method == "session/new":
            if vendor.cancel_new:
                vendor.new_started.set()
                await asyncio.Event().wait()
            external_id = f"synthetic-native-{len(vendor.sessions) + 1}"
            vendor.sessions[external_id] = {
                "system": "Native base without client rules"
                if vendor.ignore_rules
                else params.get("_meta", {}).get("rules", ""),
                "history": [],
            }
            vendor.persist_head(external_id)
            return {"sessionId": external_id}
        if method == "session/load":
            assert params["sessionId"] in vendor.sessions
            vendor.persist_head(params["sessionId"])
        if method == "session/prompt":
            vendor.sessions[params["sessionId"]]["history"].append(
                "\n".join(i["text"] for i in params["prompt"])
            )
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": params["sessionId"],
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Synthetic answer"},
                        },
                    },
                }
            )
            return {"stopReason": "end_turn"}
        return {}

    async def notify(self, method, params=None):
        self.vendor.calls.append((method, deepcopy(params)))

    async def close(self):
        self.connection_state = "disconnected"


class RetainedAcpAdapter(GrokAcpAdapter):
    def __init__(self, vendor):
        self.vendor = vendor

    async def _connect(self, *_args, **_kwargs):
        return RetainedAcpRpc(self.vendor)

    async def open_memory_managed(self, request, _memory):
        # Explicitly inert: no worker, native process or account is launched.
        return await self.open(request)


async def environment(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project = await store.create(
        Engagement(id="eng-a", name="Synthetic instruction project")
    )
    profile = await store.create(
        HarnessProfile(
            id="harness-a",
            name="Synthetic ACP",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="test-model",
            home_directory=str(tmp_path / "synthetic-account"),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    vendor = RetainedAcp()
    vendor.home = Path(profile.home_directory)
    adapter = RetainedAcpAdapter(vendor)
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        adapter_factory=lambda _: adapter,
    )
    decision = await store.create(
        ChatDecision(
            engagement_id=project.id, scope="project", kind="constraint", text=RULE
        )
    )
    return store, project, profile, runtime, vendor, adapter, decision


async def prepare(runtime, project, profile, chat=None, **kwargs):
    return await runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="test-model",
        prompt="Continue synthetic work" if chat else "Start synthetic work",
        chat_session_id=chat.id if chat else None,
        harness_session_id=None,
        mcp_server_ids=[],
        include_knowledge=False,
        **kwargs,
    )


async def run(runtime, turn):
    return [event async for event in runtime.stream_turn(turn.id)]


async def external(store, turn):
    saved = await store.get(type(turn), turn.id)
    return (
        await store.get(HarnessSession, saved.harness_session_id)
    ).external_session_id


@pytest.mark.parametrize("boundary", ["turn", "reconnect", "restart", "compaction"])
async def test_exact_saved_context_uses_one_durable_native_channel(tmp_path, boundary):
    store, project, profile, runtime, vendor, adapter, _ = await environment(tmp_path)
    chat, _, first = await prepare(runtime, project, profile)
    assert not any(e.type == "error" for e in await run(runtime, first))
    native_id = await external(store, first)
    assert vendor.effective(native_id).count(RULE) == 1
    assert vendor.sessions[native_id]["system"].count(RULE) == 1
    if boundary == "reconnect":
        await runtime._discard_connection(
            first.harness_session_id,
            runtime._connections[first.harness_session_id],
            reason="synthetic_reconnect",
        )
    elif boundary == "restart":
        await runtime.shutdown()
        runtime = HarnessRuntimeService(
            store,
            credential_store=CredentialStore(),
            workspace_resolver=lambda _: tmp_path,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            adapter_factory=lambda _: adapter,
        )
    elif boundary == "compaction":
        vendor.compact(native_id)
    same_chat, _, second = await prepare(runtime, project, profile, chat)
    assert not any(e.type == "error" for e in await run(runtime, second))
    assert same_chat.id == chat.id
    assert await external(store, second) == native_id
    assert vendor.effective(native_id).count(RULE) == 1
    prompts = [p for m, p in vendor.calls if m == "session/prompt"]
    assert len(prompts) == 2
    assert all(RULE not in json.dumps(p) for p in prompts)
    assert len([m for m, _ in vendor.calls if m == "session/new"]) == 1
    if boundary in {"restart", "reconnect"}:
        initial_cwd = next(
            p["cwd"] for method, p in vendor.calls if method == "session/new"
        )
        resumed_cwd = next(
            p["cwd"] for method, p in vendor.calls if method == "session/load"
        )
        assert resumed_cwd == initial_cwd


@pytest.mark.parametrize("remove", [False, True])
async def test_edit_or_remove_replaces_old_native_instruction_authority(
    tmp_path, remove
):
    store, project, profile, runtime, vendor, _, decision = await environment(tmp_path)
    chat, _, first = await prepare(runtime, project, profile)
    first_errors = [
        event.model_dump(mode="json")
        for event in await run(runtime, first)
        if event.type == "error"
    ]
    assert not first_errors, first_errors
    old_native = await external(store, first)
    await store.update(
        ChatDecision,
        decision.id,
        {"status": "removed"} if remove else {"text": EDITED_RULE},
        expected_revision=decision.revision,
    )
    _, _, second = await prepare(runtime, project, profile, chat)
    second_errors = [
        event.model_dump(mode="json")
        for event in await run(runtime, second)
        if event.type == "error"
    ]
    assert not second_errors, second_errors
    current_native = await external(store, second)
    assert current_native != old_native
    assert RULE not in vendor.effective(current_native)
    assert vendor.effective(current_native).count(EDITED_RULE) == (0 if remove else 1)


pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("failure", ["session/new", "session/prompt"])
async def test_failed_channel_or_prompt_delivery_does_not_drop_instructions(
    tmp_path, failure
):
    store, project, profile, runtime, vendor, _, _ = await environment(tmp_path)
    vendor.fail_once = failure
    chat, _, first = await prepare(runtime, project, profile)
    assert any(e.type == "error" for e in await run(runtime, first))
    _, _, second = await prepare(runtime, project, profile, chat)
    assert not any(e.type == "error" for e in await run(runtime, second))
    assert vendor.effective(await external(store, second)).count(RULE) == 1


async def test_cancelled_channel_install_is_retried_before_next_prompt(tmp_path):
    store, project, profile, runtime, vendor, _, _ = await environment(tmp_path)
    vendor.cancel_new = True
    chat, _, first = await prepare(runtime, project, profile)
    task = asyncio.create_task(run(runtime, first))
    await asyncio.wait_for(vendor.new_started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not vendor.sessions
    vendor.cancel_new = False
    _, _, second = await prepare(runtime, project, profile, chat)
    assert not any(e.type == "error" for e in await run(runtime, second))
    assert vendor.effective(await external(store, second)).count(RULE) == 1
    installs = [p for method, p in vendor.calls if method == "session/new"]
    assert len(installs) == 2
    assert all(p["_meta"]["rules"].count(RULE) == 1 for p in installs)


async def test_concurrent_sends_admit_one_instruction_version(tmp_path):
    store, project, profile, runtime, vendor, _, _ = await environment(tmp_path)
    chat, _, first = await prepare(runtime, project, profile)
    await run(runtime, first)
    results = await asyncio.gather(
        prepare(runtime, project, profile, chat),
        prepare(runtime, project, profile, chat),
        return_exceptions=True,
    )
    admitted = [r for r in results if isinstance(r, tuple)]
    assert len(admitted) == 1
    assert any(isinstance(r, ChatHistoryConflict) for r in results)
    _, _, turn = admitted[0]
    assert not any(e.type == "error" for e in await run(runtime, turn))
    assert vendor.effective(await external(store, turn)).count(RULE) == 1


async def test_goal_resume_keeps_exact_pinned_skill_once_after_compaction(tmp_path):
    from nebula.v3.domain import ChatGoal, ChatGoalStatus
    from nebula.v3.skill_catalog import (
        SkillSelection,
        discover_skills,
        native_skill_roots,
        snapshot_skill,
    )

    store, project, profile, runtime, vendor, _, _ = await environment(tmp_path)
    chat, _, initial = await prepare(runtime, project, profile)
    await run(runtime, initial)
    skill = tmp_path / ".agents" / "skills" / "synthetic" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    body = (
        "SYNTHETIC_SKILL_RULE: Preserve these exact steps.\n1. Inspect.\n2. Verify.\n"
    )
    skill.write_text(body)
    selection = SkillSelection(name="synthetic", path=str(skill))
    snapshot = snapshot_skill(selection, discover_skills(native_skill_roots(tmp_path)))
    goal = await store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=chat.id,
            objective="Finish the synthetic instruction journey",
            completion_criteria=["Keep exact instructions"],
            status=ChatGoalStatus.RUNNING,
            skill_snapshots=[snapshot.model_dump(mode="json")],
        )
    )
    _, _, first = await prepare(
        runtime, project, profile, chat, skill_selections=[selection.model_dump()]
    )
    assert not any(e.type == "error" for e in await run(runtime, first))
    native_id = await external(store, first)
    system = vendor.sessions[native_id]["system"]
    assert system.count("SYNTHETIC_SKILL_RULE") == 1
    assert json.dumps(body, ensure_ascii=False) in system
    vendor.compact(native_id)
    current_goal = await store.get(ChatGoal, goal.id)
    paused = await store.update(
        ChatGoal,
        goal.id,
        {"status": ChatGoalStatus.PAUSED},
        expected_revision=current_goal.revision,
    )
    await store.update(
        ChatGoal,
        goal.id,
        {"status": ChatGoalStatus.RUNNING},
        expected_revision=paused.revision,
    )
    _, _, resumed = await prepare(runtime, project, profile, chat)
    assert not any(e.type == "error" for e in await run(runtime, resumed))
    effective = vendor.effective(await external(store, resumed))
    assert effective.count("SYNTHETIC_SKILL_RULE") == 1
    assert json.dumps(body, ensure_ascii=False) in effective


@pytest.mark.parametrize("kind", ["codex", "claude"])
async def test_native_developer_and_system_payloads_keep_exact_standing_text(
    tmp_path, monkeypatch, kind
):
    from types import SimpleNamespace
    from nebula.v3.harnesses import ClaudeAgentSdkAdapter
    from tests.v3.test_harness_adapters import (
        FixtureCodexAdapter,
        FixtureCodexRpc,
        FakeClaudeClient,
        FakeClaudeOptions,
        FakeHookMatcher,
    )

    store, project, profile, runtime, _, _, _ = await environment(tmp_path)
    profile = await store.update(
        HarnessProfile,
        profile.id,
        {
            "kind": HarnessKind.CODEX_APP_SERVER
            if kind == "codex"
            else HarnessKind.CLAUDE_AGENT_SDK,
            "home_directory": profile.home_directory if kind == "codex" else None,
        },
        expected_revision=profile.revision,
    )
    rpc = FixtureCodexRpc()
    if kind == "codex":
        adapter = FixtureCodexAdapter(rpc)
    else:
        sdk = SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeOptions,
            ClaudeSDKClient=FakeClaudeClient,
            HookMatcher=FakeHookMatcher,
        )
        monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))
        adapter = ClaudeAgentSdkAdapter()

    async def inert_open(request, _memory):
        return await adapter.open(request)

    adapter.open_memory_managed = inert_open
    runtime.adapter_factory = lambda _: adapter
    _, _, turn = await prepare(runtime, project, profile)
    session = await store.get(HarnessSession, turn.harness_session_id)
    await runtime._connection(session, turn)
    if kind == "codex":
        instructions = next(
            p["developerInstructions"] for m, p in rpc.calls if m == "thread/start"
        )
    else:
        instructions = FakeClaudeClient.latest.options.kwargs["system_prompt"]
    assert instructions.count(RULE) == 1
    assert RULE not in turn.prompt


async def test_legacy_retained_context_rolls_over_before_installing_standing_channel(
    tmp_path,
):
    store, project, profile, runtime, vendor, _, _ = await environment(tmp_path)
    chat, _, first = await prepare(runtime, project, profile)
    await run(runtime, first)
    native_id = await external(store, first)
    session = await store.get(HarnessSession, first.harness_session_id)
    await runtime._discard_connection(
        session.id, runtime._connections[session.id], reason="legacy_fixture"
    )
    metadata = {
        k: v
        for k, v in session.metadata.items()
        if k not in {"standing_instructions", "instruction_channel"}
    }
    await store.update(
        HarnessSession,
        session.id,
        {"metadata": metadata},
        expected_revision=session.revision,
    )
    _, _, second = await prepare(runtime, project, profile, chat)
    assert not any(e.type == "error" for e in await run(runtime, second))
    replacement = await external(store, second)
    assert replacement != native_id
    assert vendor.effective(replacement).count(RULE) == 1


async def test_ignored_acp_rules_are_rejected_before_any_model_prompt(tmp_path):
    store, project, profile, runtime, vendor, _, _ = await environment(tmp_path)
    vendor.ignore_rules = True
    chat, _, turn = await prepare(runtime, project, profile)
    events = await run(runtime, turn)
    assert any(
        e.type == "error"
        and "did not confirm durable standing instructions" in e.message
        for e in events
    )
    assert not any(method == "session/prompt" for method, _ in vendor.calls)
    session = await store.get(HarnessSession, turn.harness_session_id)
    assert session.external_session_id is None
    assert "instruction_channel" not in session.metadata
    vendor.ignore_rules = False
    _, _, retried = await prepare(runtime, project, profile, chat)
    assert not any(e.type == "error" for e in await run(runtime, retried))
    assert vendor.effective(await external(store, retried)).count(RULE) == 1


async def test_native_head_rebuild_on_load_restores_exact_current_instructions(
    tmp_path,
):
    store, project, profile, runtime, vendor, _, _ = await environment(tmp_path)
    chat, _, first = await prepare(runtime, project, profile)
    await run(runtime, first)
    native_id = await external(store, first)
    vendor.sessions[native_id]["system"] = "Simulated native memory-mode head rebuild"
    await runtime._discard_connection(
        first.harness_session_id,
        runtime._connections[first.harness_session_id],
        reason="synthetic_memory_reconfiguration",
    )
    _, _, second = await prepare(runtime, project, profile, chat)
    assert not any(e.type == "error" for e in await run(runtime, second))
    recovered = await external(store, second)
    assert recovered != native_id
    assert vendor.effective(recovered).count(RULE) == 1


async def test_separate_channel_preserves_existing_combined_input_limit(tmp_path):
    from nebula.v3.harnesses import HarnessConfigurationError

    store, project, profile, runtime, vendor, _, _ = await environment(tmp_path)
    selections = []
    for name in ["large-one", "large-two"]:
        path = tmp_path / ".agents" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("x" * 126_000)
        selections.append({"name": name, "path": str(path)})
    with pytest.raises(HarnessConfigurationError, match="input limit"):
        await prepare(runtime, project, profile, skill_selections=selections)
    assert not vendor.calls
