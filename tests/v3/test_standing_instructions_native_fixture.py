"""Narrow checks for the retained-bundle native acceptance helper."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
    AsyncTestClient,
)
import json
from types import SimpleNamespace
import pytest
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import ChatDecision, ChatSession, Engagement, ProviderProfile
from nebula.v3.runtime_platform import default_runtime_platform
from nebula.v3.storage import NebulaStore
from tests.v3 import standing_instructions_native_fixture as fixture


async def setup(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(
        Engagement(id="continuity-project", name="Synthetic public-native fixture")
    )
    profile = await store.create(
        ProviderProfile(
            id="continuity-provider",
            name="Seed provider",
            provider_type="vllm",
            is_local=True,
        )
    )
    await store.create(
        ChatSession(
            id="continuity-chat",
            engagement_id=project.id,
            title="Native fixture",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"initial_title_state": "operator"},
        )
    )
    root = tmp_path / "data"
    root.mkdir()
    receipt = await fixture.initialize(
        store, root=root, project_id=project.id, session_id="continuity-chat"
    )
    artifacts = ArtifactStore(root / "artifacts")
    app = create_app(
        store,
        auth_token="synthetic-token",
        execution_data_root=root,
        artifact_store=artifacts,
        tool_platform=default_runtime_platform(
            store=store, artifact_store=artifacts, data_root=root
        ),
    )
    fixture.configure_app(
        app,
        root=root,
        lease=SimpleNamespace(project_id=project.id),
        version="acceptance-v1",
    )
    return store, root, receipt, app


async def send(client, session_id="continuity-chat"):
    response = await client.post(
        "/api/v1/chat/completions",
        headers={"Authorization": "Bearer synthetic-token"},
        json={
            "backend": "harness",
            "session_id": session_id,
            "engagement_id": "continuity-project",
            "harness_profile_id": "standing-native-profile",
            "model": fixture.MODEL,
            "include_knowledge": False,
            "stream": True,
            "messages": [
                {"role": "user", "content": "Use the current saved instructions."}
            ],
        },
    )
    assert response.status_code == 200, response.text
    frames = [
        json.loads(line[5:])
        for line in response.text.splitlines()
        if line.startswith("data:") and "[DONE]" not in line
    ]
    return frames


async def test_native_bundle_helper_uses_real_guard_and_persistent_context(tmp_path):
    store, root, receipt, app = await setup(tmp_path)
    path = receipt["control_path"]
    headers = {"Authorization": "Bearer synthetic-token"}
    async with AsyncTestClient(app) as client:
        assert (
            await client.post(path, json={"operation": "capture"})
        ).status_code == 401
        first = await send(client)
        assert any(frame["type"] == "done" for frame in first), first
        initial = fixture.evidence(root)
        native_id = initial["active_native_session_id"]
        assert (initial["alpha_copies"], initial["beta_copies"]) == (1, 0)
        await client.post(path, headers=headers, json={"operation": "compact"})
        second = await send(client)
        assert any(frame["type"] == "done" for frame in second), second
        await client.post(path, headers=headers, json={"operation": "reconnect"})
        third = await send(client)
        assert any(frame["type"] == "done" for frame in third), third
        capture = (
            await client.post(path, headers=headers, json={"operation": "capture"})
        ).json()
        assert capture["active_native_session_id"] == native_id
        assert capture["turn_active"] is False
        assert (capture["alpha_copies"], capture["beta_copies"]) == (1, 0)
        assert capture["channel_receipt"]["external_session_id"] == native_id
        calls = capture["native"]["requests"]
        assert any(
            row["method"] == "session/load" and row["native_session_id"] == native_id
            for row in calls
        )
        assert all(
            fixture.RULE_ALPHA not in json.dumps(row["params"])
            for row in calls
            if row["method"] == "session/prompt" and row["managed"]
        )
        decision = await store.get(ChatDecision, receipt["decision_id"])
        changed = await client.put(
            f"/api/v1/chat/sessions/continuity-chat/decisions/{decision.id}",
            headers=headers,
            json={
                "expected_revision": decision.revision,
                "kind": "constraint",
                "text": fixture.RULE_BETA,
            },
        )
        assert changed.status_code == 200, changed.text
        final = await send(client)
        assert any(frame["type"] == "done" for frame in final), final
        updated = fixture.evidence(root)
        assert updated["active_native_session_id"] != native_id
        assert (updated["alpha_copies"], updated["beta_copies"]) == (0, 1)
        assert "Native fixture confirmed BETA." in json.dumps(final)
        assert (
            updated["native"]["compactions"][0]["system_sha256_before"]
            == updated["native"]["compactions"][0]["system_sha256_after"]
        )


async def test_native_bundle_helper_never_bypasses_missing_system_head(
    tmp_path, monkeypatch
):
    _, root, _, app = await setup(tmp_path)
    monkeypatch.setattr(
        fixture.NativeState, "persist_head", lambda *args, **kwargs: None
    )
    async with AsyncTestClient(app) as client:
        frames = await send(client)
    assert any(
        "did not confirm durable standing instructions" in json.dumps(frame)
        for frame in frames
    )
    assert not any(
        row["method"] == "session/prompt"
        for row in fixture.evidence(root)["native"]["requests"]
    )


pytestmark = pytest.mark.anyio
