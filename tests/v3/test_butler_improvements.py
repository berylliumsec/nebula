"""Focused issue destination, draft safety, and publication receipts."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import json
from types import SimpleNamespace

from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.butler_improvements import (
    ButlerImprovementService,
    DestinationWrite,
    DraftEdit,
    _dedup_key,
    _unsafe_field,
)
from nebula.v3.butler_oversight import ButlerOversightService
from nebula.v3.domain import (
    ButlerIssueCandidate,
    ButlerIssueDestination,
    ButlerIssueScan,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessTurn,
    HarnessTurnOrigin,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    ProviderProfile,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_control_assistant_work import _app


class Credentials:
    def status(self, reference):
        return SimpleNamespace(available=reference == "env:GITHUB_TOKEN")

    def resolve(self, reference):
        assert reference == "env:GITHUB_TOKEN"
        return SimpleNamespace(get_secret_value=lambda: "private-test-token")


class Harness:
    def __init__(self, response="[]"):
        self.response = response
        self.calls = []

    async def analyze_structured(self, **kwargs):
        self.calls.append(kwargs)
        return HarnessTurn(
            engagement_id=kwargs["engagement_id"],
            harness_session_id="analysis",
            origin=HarnessTurnOrigin.ANALYSIS,
            prompt=kwargs["prompt"],
            response=self.response,
        )


class Mcp:
    def __init__(self):
        self.calls = []

    async def call_tool(self, profile, *, engagement_id, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        repository = arguments["repository"]
        if tool_name == "issue_target":
            value = {"repository": repository, "visibility": "private"}
        elif tool_name == "issue_find":
            value = {"found": False}
        else:
            value = {
                "repository": repository,
                "dedup_key": arguments["dedup_key"],
                "id": "42",
                "url": "https://issues.example.test/42",
            }
        return {"content": [{"type": "text", "text": json.dumps(value)}]}


async def setup(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    codex = await store.create(
        HarnessProfile(
            name="Host Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/usr/bin/true",
            default_model="codex-test",
            enabled=True,
        )
    )
    harness = Harness()
    mcp = Mcp()
    service = ButlerImprovementService(store, harness, Credentials(), mcp)
    return store, project, codex, harness, mcp, service


async def destination(service, project, codex, repository="owner/nebula", **changes):
    data = dict(
        name=repository,
        repository=repository,
        publisher="github",
        credential_ref="env:GITHUB_TOKEN",
        review_profile_id=codex.id,
        auto_publish=True,
    )
    data.update(changes)
    return await service.create_destination(project.id, DestinationWrite(**data))


async def candidate(store, project, dest, key="refresh-failure", **changes):
    data = dict(
        engagement_id=project.id,
        destination_id=dest.id,
        source_kind="chat_turn",
        source_id="turn-1",
        problem_key=key,
        dedup_key=_dedup_key(dest.id, key),
        title="Refresh loses the saved selection",
        body="The selected item disappears after refresh even though it remains saved.",
        reproduction="Create an item, select it, and refresh the page.",
        confidence="hypothesis",
        public_independent=True,
    )
    data.update(changes)
    return await store.create(ButlerIssueCandidate(**data))


async def test_public_issue_rechecks_visibility_and_holds_private_content(tmp_path):
    store, project, codex, _, _, service = await setup(tmp_path)
    dest = await destination(service, project, codex)
    calls = []

    async def github(_, method, path, **kwargs):
        calls.append((method, path))
        if path.startswith("/repos/") and method == "GET":
            return {
                "full_name": dest.repository,
                "visibility": "public",
                "private": False,
                "has_issues": True,
            }
        if path == "/search/issues":
            return {"items": []}
        return {"number": 1, "html_url": "https://github.com/owner/nebula/issues/1"}

    service._github = github
    unsafe = await candidate(
        store, project, dest, title="Fix /home/agent/private_research leak"
    )
    held = await service.publish(unsafe)
    assert held.status == "held" and held.reason == "private_or_research_detail"
    assert not any(method == "POST" for method, _ in calls)
    safe = await candidate(store, project, dest, key="second-problem")
    published = await service.publish(safe)
    assert published.status == "published"
    assert published.issue_url == "https://github.com/owner/nebula/issues/1"
    assert sum(method == "POST" for method, _ in calls) == 1
    assert (
        _unsafe_field("Bearer ghp_abcdefghijklmnopqrstuvwxyz012345", public=False)
        == "secret_detected"
    )
    assert (
        _unsafe_field("apple_private_research", public=True)
        == "private_or_research_detail"
    )


async def test_private_to_public_change_blocks_research_draft_before_create(tmp_path):
    store, project, codex, _, _, service = await setup(tmp_path)
    dest = await destination(service, project, codex, auto_publish=False)
    draft = await candidate(
        store,
        project,
        dest,
        body=("The public wording still contains proprietary Apple research details."),
        public_independent=False,
    )
    held = await service.publish(draft)
    assert held.reason == "automatic_publishing_not_granted"
    updated = await store.update(
        ButlerIssueDestination, dest.id, {"auto_publish": True}
    )
    created = []

    async def target(_):
        return "public"  # visibility changed after the draft was saved

    async def create(*args):
        created.append(args)
        raise AssertionError("unsafe public draft must never be sent")

    service._target, service._create = target, create
    blocked = await service.publish((await store.get(ButlerIssueCandidate, draft.id)))
    assert updated.auto_publish and blocked.status == "held"
    assert blocked.reason == "private_or_research_detail" and created == []


async def test_unknown_github_visibility_holds_before_create(tmp_path):
    store, project, codex, _, _, service = await setup(tmp_path)
    dest = await destination(service, project, codex)
    draft = await candidate(store, project, dest)

    async def github(_, method, path, **kwargs):
        assert method == "GET" and path.startswith("/repos/")
        return {
            "full_name": dest.repository,
            "visibility": "public",
            "has_issues": True,
        }

    service._github = github
    held = await service.publish(draft)
    assert held.status == "held" and held.reason == "target_visibility_unavailable"


async def test_destination_identity_is_fixed_and_uncertain_create_cannot_be_revised(
    tmp_path,
):
    store, project, codex, _, _, service = await setup(tmp_path)
    dest = await destination(service, project, codex)
    data = DestinationWrite(
        name="Updated name",
        repository="owner/other",
        publisher="github",
        credential_ref="env:GITHUB_TOKEN",
        review_profile_id=codex.id,
        auto_publish=True,
    )
    try:
        (await service.update_destination(project.id, dest.id, data, dest.revision))
    except ValueError as exc:
        assert "new destination" in str(exc)
    else:
        raise AssertionError("existing candidates must not be rerouted")
    draft = await candidate(store, project, dest, status="uncertain")
    edit = DraftEdit(
        expected_revision=draft.revision,
        title=draft.title,
        body=draft.body,
        reproduction=draft.reproduction,
        public_independent=True,
    )
    try:
        (await service.edit_candidate(project.id, draft.id, edit))
    except ValueError as exc:
        assert "held or reviewed" in str(exc)
    else:
        raise AssertionError("an uncertain create must be reconciled before revision")


async def test_slow_issue_review_does_not_occupy_the_oversight_worker(tmp_path):
    async def scenario():
        store = NebulaStore(tmp_path / "core.db")
        project = await store.create(Engagement(name="Selected"))
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowReview:
            async def process_project(self, project_id):
                assert project_id == project.id
                started.set()
                await release.wait()

        oversight = ButlerOversightService(store)
        oversight.improvement_service = SlowReview()
        oversight._schedule_improvements(project.id)
        await asyncio.wait_for(started.wait(), timeout=1)
        first = oversight._issue_tasks[project.id]
        oversight._schedule_improvements(project.id)
        assert oversight._issue_tasks[project.id] is first
        assert not first.done()
        release.set()
        await asyncio.wait_for(first, timeout=1)
        await oversight.shutdown()

    (await scenario())


async def test_interrupted_source_review_is_held_without_replaying_codex(tmp_path):
    store, project, codex, harness, _, service = await setup(tmp_path)
    dest = await destination(service, project, codex)
    scan = await store.create(
        ButlerIssueScan(
            engagement_id=project.id,
            destination_id=dest.id,
            source_kind="chat_turn",
            source_id="turn-1",
        )
    )
    assert await service._scan_already_started(scan.id)
    held = await store.get(ButlerIssueScan, scan.id)
    assert held.status == "held" and held.reason == "review_interrupted"
    assert await service._scan_already_started(scan.id)
    assert harness.calls == []


async def test_timeout_is_reconciled_by_key_without_blind_retry(tmp_path):
    store, project, codex, _, _, service = await setup(tmp_path)
    dest = await destination(service, project, codex)
    draft = await candidate(store, project, dest)
    creates = []
    found = []

    async def target(_):
        return "private"

    async def lookup(_, key):
        return found[0] if found else None

    async def create(*args):
        creates.append(args)
        raise TimeoutError("response lost")

    service._target, service._find, service._create = target, lookup, create
    uncertain = await service.publish(draft)
    assert uncertain.status == "uncertain" and len(creates) == 1
    assert (await service.publish(uncertain)).status == "uncertain"
    assert len(creates) == 1
    found.append(("17", "https://github.com/owner/nebula/issues/17"))
    assert (
        await service.publish((await store.get(ButlerIssueCandidate, draft.id)))
    ).status == "published"
    assert len(creates) == 1


async def test_trusted_mcp_contract_routes_to_selected_private_repository(tmp_path):
    store, project, codex, _, mcp, service = await setup(tmp_path)
    tools = [
        McpToolSnapshot(name=name, input_schema={"type": "object"})
        for name in ("issue_target", "issue_find", "issue_create")
    ]
    server = await store.create(
        McpServerProfile(
            name="issue-tools",
            transport=McpTransport.STDIO,
            command="/usr/bin/true",
            enabled=True,
            trusted_stdio=True,
            capabilities=McpCapabilitySnapshot(tools=tools),
        )
    )
    dest = await destination(
        service,
        project,
        codex,
        repository="someone/custom",
        publisher="mcp",
        credential_ref=None,
        mcp_profile_id=server.id,
    )
    draft = await candidate(store, project, dest)
    result = await service.publish(draft)
    assert (
        result.status == "published"
        and result.issue_url == "https://issues.example.test/42"
    )
    assert [name for name, _ in mcp.calls] == [
        "issue_target",
        "issue_find",
        "issue_create",
    ]
    assert all(args["repository"] == "someone/custom" for _, args in mcp.calls)


async def test_completed_saved_chat_creates_separate_candidates_with_toolless_codex(
    tmp_path,
):
    store, project, codex, harness, _, service = await setup(tmp_path)
    dest = await destination(service, project, codex)
    provider = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    chat = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    message = await store.create(
        ChatMessage(
            engagement_id=project.id,
            session_id=chat.id,
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="Refresh loses selection; a separate export action fails.",
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            provider_profile_id=provider.id,
            model="model-a",
            status=ChatTurnStatus.COMPLETE,
            final_message_id=message.id,
        )
    )
    (
        await service.store.update(
            ButlerIssueDestination, dest.id, {"observe_from": turn.created_at}
        )
    )
    harness.response = json.dumps(
        [
            {
                "problem_key": key,
                "title": title,
                "body": "A specific saved workflow fails after the action completes.",
                "reproduction": "Create the item, run the action, and inspect the result.",
                "confidence": "observed",
                "public_independent": True,
            }
            for key, title in (
                ("refresh-selection", "Refresh loses the selection"),
                ("export-failure", "Export fails for saved item"),
            )
        ]
    )

    async def target(_):
        return "public"

    async def lookup(_, key):
        return None

    async def create(_, draft, *args):
        return draft.dedup_key[
            :6
        ], f"https://github.com/owner/nebula/issues/{draft.dedup_key[:6]}"

    service._target, service._find, service._create = target, lookup, create
    (await service.process_project(project.id))
    rows = await service.candidates(project.id)
    assert len(rows) == 2 and all(row.status == "published" for row in rows)
    assert len({row.dedup_key for row in rows}) == 2
    assert harness.calls[0]["nebula_tools"] is False
    assert "private-test-token" not in harness.calls[0]["prompt"]
    (await service.process_project(project.id))
    assert len(harness.calls) == 1


async def test_destination_api_requires_auth_and_persists_per_project_grant(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "disposable-test-token")
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    codex = await store.create(
        HarnessProfile(
            name="Host Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/usr/bin/true",
            default_model="codex-test",
            enabled=True,
        )
    )
    client = TestClient(app)
    path = f"/api/v1/engagements/{project.id}/butler/issues"
    data = dict(
        name="Own repo",
        repository="someone/other-tool",
        publisher="github",
        credential_ref="env:GITHUB_TOKEN",
        review_profile_id=codex.id,
        auto_publish=False,
        enabled=True,
    )
    assert (await client.post(path + "/destinations", json=data)).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    created = await client.post(path + "/destinations", json=data, headers=headers)
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["repository"] == "someone/other-tool" and row["auto_publish"] is False
    assert (await client.get(path, headers=headers)).json()["destinations"][0][
        "id"
    ] == row["id"]
    assert (
        await client.get(
            f"/api/v1/engagements/{other.id}/butler/issues", headers=headers
        )
    ).json()["destinations"] == []
    changed = await client.put(
        path + f"/destinations/{row['id']}?expected_revision={row['revision']}",
        json={**data, "auto_publish": True},
        headers=headers,
    )
    assert changed.status_code == 200 and changed.json()["auto_publish"] is True
    assert (
        await client.put(
            path + f"/destinations/{row['id']}?expected_revision={row['revision']}",
            json=data,
            headers=headers,
        )
    ).status_code == 409


pytestmark = pytest.mark.anyio
