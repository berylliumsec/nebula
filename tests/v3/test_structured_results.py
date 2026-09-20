import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    StructuredResult,
)
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.structured_results import (
    DASHBOARD_PUBLISH_TOOL_NAME,
    MAX_RESULTS_PER_PROJECT,
    MAX_RESULT_BYTES,
    MAX_RESULT_DEPTH,
    SNAPSHOT_INTERVAL_SECONDS,
    PublishResultTool,
    StructuredResultPublish,
    StructuredResultRejected,
    dashboard_publish_spec,
    goal_snapshot_instruction,
    inspect_payload,
    latest_snapshot,
    payload_preview,
    publish_result,
    snapshot_overdue,
    structured_results_router,
    validate_hints,
)
from nebula.v3.tools import ToolInvocation


def fixture(tmp_path):
    store = NebulaStore(tmp_path / "results.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(Engagement(id="other", name="Other project"))
    app = FastAPI()
    app.include_router(structured_results_router(store))

    # Core maps an absent record to 404; mirror that one handler so these
    # routes are exercised the way the application serves them.
    @app.exception_handler(NotFoundError)
    async def absent(request, exc):  # pragma: no cover - trivial mapping
        return JSONResponse({"detail": str(exc)}, status_code=404)

    return store, TestClient(app, raise_server_exceptions=False)


def publish(client, payload, **extra):
    body = {"title": "A result", "result": payload, **extra}
    return client.post("/projects/project/structured-results", json=body)


def test_any_json_value_publishes_and_reads_back_unchanged(tmp_path):
    _, client = fixture(tmp_path)
    for payload in [
        {"name": "  spaced  ", "count": 3},
        [1, 2, 3],
        "a bare string",
        12345678901234567890,
        0.1 + 0.2,
        True,
        None,
    ]:
        created = publish(client, payload)
        assert created.status_code == 201, created.text
        identity = created.json()["result"]["id"]
        read = client.get(f"/projects/project/structured-results/{identity}")
        assert read.status_code == 200
        # The stored value is the authority: no trimming, rounding or coercion.
        assert read.json()["result"] == payload


def test_stats_describe_shape_without_interpreting_it(tmp_path):
    _, client = fixture(tmp_path)
    created = publish(client, {"rows": [{"a": 1}, {"a": 2}], "note": None})
    stats = created.json()["result"]["stats"]
    assert stats["root_type"] == "object"
    assert stats["top_level_count"] == 2
    assert stats["max_depth"] == 3
    assert stats["node_count"] == 7
    assert stats["byte_size"] > 0


def test_deeply_nested_payload_is_inspected_without_recursion(tmp_path):
    _, client = fixture(tmp_path)
    deep: object = "leaf"
    for _ in range(MAX_RESULT_DEPTH - 1):
        deep = {"next": deep}
    created = publish(client, deep)
    assert created.status_code == 201, created.text
    assert created.json()["result"]["stats"]["max_depth"] == MAX_RESULT_DEPTH - 1

    deeper: object = "leaf"
    for _ in range(MAX_RESULT_DEPTH + 5):
        deeper = {"next": deeper}
    refused = publish(client, deeper)
    assert refused.status_code == 422
    assert "nests deeper" in refused.json()["detail"]


def test_oversized_and_non_json_payloads_are_refused_by_name():
    with pytest.raises(StructuredResultRejected) as oversized:
        inspect_payload({"blob": "x" * (MAX_RESULT_BYTES + 1)})
    assert "the limit is" in str(oversized.value)

    with pytest.raises(StructuredResultRejected) as unsupported:
        inspect_payload({"path": Path("/tmp")})
    assert "PosixPath" in str(unsupported.value)

    with pytest.raises(StructuredResultRejected) as keys:
        inspect_payload({1: "numeric key"})
    assert "object keys must be strings" in str(keys.value)


def test_a_structure_that_refers_into_itself_is_refused_not_walked_forever():
    loop: dict = {"name": "loop"}
    loop["self"] = loop
    with pytest.raises(StructuredResultRejected) as refused:
        inspect_payload(loop)
    assert "refers back into itself" in str(refused.value)

    # Repeating a shared child is not a cycle and must stay publishable.
    shared = {"id": "shared"}
    assert inspect_payload({"left": shared, "right": shared}).node_count == 5


def test_hints_are_stored_beside_the_result_and_never_merged_into_it(tmp_path):
    _, client = fixture(tmp_path)
    created = publish(
        client,
        {"name": "host-1"},
        hints={"titleField": "name", "future_key": {"anything": True}},
    )
    body = created.json()["result"]
    assert body["result"] == {"name": "host-1"}
    assert body["hints"]["titleField"] == "name"
    # Core bounds hints but does not police their vocabulary, so the interface
    # can learn new hints without a Core release.
    assert body["hints"]["future_key"] == {"anything": True}

    with pytest.raises(StructuredResultRejected):
        validate_hints({"blob": "x" * 300_000})
    with pytest.raises(StructuredResultRejected):
        validate_hints(["not an object"])  # type: ignore[arg-type]
    assert validate_hints(None) is None


def test_summaries_stay_light_and_preview_only_top_level_scalars(tmp_path):
    _, client = fixture(tmp_path)
    publish(
        client,
        {
            "status": "unknown-state",
            "nested": {"hidden": True},
            "rows": [1, 2],
            "long": "y" * 500,
        },
        title="Scan output",
        producer="agent",
    )
    listed = client.get("/projects/project/structured-results")
    assert listed.status_code == 200
    (row,) = listed.json()
    assert row["title"] == "Scan output"
    assert "result" not in row
    assert row["has_hints"] is False
    assert [item["key"] for item in row["preview"]] == ["status", "long"]
    assert row["preview"][1]["truncated"] is True
    assert len(row["preview"][1]["value"]) == 120
    assert payload_preview([1, 2, 3]) == []


def test_results_list_newest_first_and_are_scoped_to_their_project(tmp_path):
    store, client = fixture(tmp_path)
    first = publish(client, {"n": 1}, title="First").json()["result"]["id"]
    second = publish(client, {"n": 2}, title="Second").json()["result"]["id"]
    assert [
        row["id"] for row in client.get("/projects/project/structured-results").json()
    ] == [
        second,
        first,
    ]
    assert client.get("/projects/other/structured-results").json() == []
    assert client.get(f"/projects/other/structured-results/{first}").status_code == 404
    assert (
        client.delete(f"/projects/other/structured-results/{first}").status_code == 404
    )
    assert (
        client.delete(f"/projects/project/structured-results/{first}").status_code
        == 204
    )
    assert store.count(StructuredResult, engagement_id="project") == 1


def test_retention_removes_the_oldest_and_says_which_ones(tmp_path):
    store, _ = fixture(tmp_path)
    identities = []
    removed: list[str] = []
    for index in range(MAX_RESULTS_PER_PROJECT + 2):
        outcome = publish_result(
            store,
            "project",
            StructuredResultPublish(title=f"Result {index}", result={"index": index}),
        )
        identities.append(outcome.result.id)
        # Only publishes past the cap remove anything, and they name what went.
        assert len(outcome.retention_removed) == (
            1 if index >= MAX_RESULTS_PER_PROJECT else 0
        )
        removed.extend(outcome.retention_removed)
    assert (
        store.count(StructuredResult, engagement_id="project")
        == MAX_RESULTS_PER_PROJECT
    )
    assert set(removed) == set(identities[:2])
    surviving = {
        row.id
        for row in store.list_entities(
            StructuredResult, engagement_id="project", limit=1_000
        )
    }
    assert surviving == set(identities[2:])


def test_publishing_into_an_unknown_project_is_not_an_orphan_record(tmp_path):
    _, client = fixture(tmp_path)
    missing = client.post(
        "/projects/absent/structured-results", json={"title": "x", "result": {}}
    )
    assert missing.status_code == 404


def test_publish_tool_declares_a_write_with_no_network_or_filesystem_reach():
    spec = dashboard_publish_spec()
    assert spec.name == DASHBOARD_PUBLISH_TOOL_NAME
    assert spec.network_access is False
    assert spec.filesystem_access == "none"
    assert spec.cloud_transfer is False
    assert spec.risk_class.value == "workspace_write"
    # "result" accepts any JSON value, so the schema constrains only its presence.
    assert spec.input_schema["required"] == ["title", "result"]
    assert "type" not in spec.input_schema["properties"]["result"]


def test_publish_tool_stores_the_payload_and_points_the_operator_at_it(tmp_path):
    store, _ = fixture(tmp_path)
    tool = PublishResultTool(store)
    invocation = ToolInvocation(
        engagement_id="project",
        run_id="run",
        chat_session_id="session",
        tool_name=DASHBOARD_PUBLISH_TOOL_NAME,
        workspace=tmp_path,
        arguments={
            "title": "Inventory",
            "result": {"hosts": [{"ip": "10.0.0.1"}]},
            "hints": {"titleField": "ip"},
        },
    )
    result = asyncio.run(tool.execute(invocation, runner=None))
    assert result.exit_code == 0
    stored = store.get(StructuredResult, result.output["result_id"])
    assert stored.result == {"hosts": [{"ip": "10.0.0.1"}]}
    assert stored.origin == "agent"
    assert stored.chat_session_id == "session"
    assert result.output["open_path"] == f"/projects/project/results/{stored.id}"
    # The payload is not echoed back into the conversation.
    assert "hosts" not in str(result.output)


def test_publish_tool_reports_a_refusal_instead_of_raising(tmp_path):
    store, _ = fixture(tmp_path)
    tool = PublishResultTool(store)
    invocation = ToolInvocation(
        engagement_id="project",
        run_id="run",
        tool_name=DASHBOARD_PUBLISH_TOOL_NAME,
        workspace=tmp_path,
        arguments={
            "title": "Too big",
            "result": {"blob": "z" * (MAX_RESULT_BYTES + 1)},
        },
    )
    result = asyncio.run(tool.execute(invocation, runner=None))
    assert result.exit_code == 1
    assert result.output["result_id"] is None
    assert "the limit is" in result.output["detail"]
    assert store.count(StructuredResult, engagement_id="project") == 0


def test_progress_snapshots_form_one_ordered_stream(tmp_path):
    store, client = fixture(tmp_path)
    tool = PublishResultTool(store)

    def snapshot(title, payload, stream="refactor-auth"):
        invocation = ToolInvocation(
            engagement_id="project",
            run_id="run",
            chat_session_id="session",
            tool_name=DASHBOARD_PUBLISH_TOOL_NAME,
            workspace=tmp_path,
            arguments={"title": title, "result": payload, "stream": stream},
        )
        return asyncio.run(tool.execute(invocation, runner=None)).output

    first = snapshot(
        "Read the handler", {"language": "python", "code": "def login():\n    pass"}
    )
    second = snapshot(
        "Mapped the call graph",
        {
            "nodes": [{"id": "login"}],
            "edges": [{"source": "login", "target": "verify"}],
        },
    )
    other = snapshot("Unrelated work", {"note": "separate"}, stream="docs")

    assert (first["stream"], first["sequence"]) == ("refactor-auth", 1)
    assert second["sequence"] == 2
    # A different stream numbers its own steps rather than continuing another.
    assert other["sequence"] == 1

    series = client.get(
        "/projects/project/structured-results?stream=refactor-auth"
    ).json()
    assert [row["sequence"] for row in series] == [2, 1]
    assert {row["title"] for row in series} == {
        "Read the handler",
        "Mapped the call graph",
    }

    by_session = client.get(
        "/projects/project/structured-results?chat_session_id=session"
    ).json()
    assert len(by_session) == 3
    assert (
        client.get("/projects/project/structured-results?chat_session_id=other").json()
        == []
    )


def test_the_newest_results_are_the_first_page(tmp_path):
    _, client = fixture(tmp_path)
    for index in range(5):
        publish(client, {"index": index}, title=f"Result {index}")
    page = client.get("/projects/project/structured-results?limit=2").json()
    assert [row["title"] for row in page] == ["Result 4", "Result 3"]
    later = client.get("/projects/project/structured-results?limit=2&offset=2").json()
    assert [row["title"] for row in later] == ["Result 2", "Result 1"]


def running_goal(store, objective="Refactor the auth handler"):
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Goal chat",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    return store.create(
        ChatGoal(
            id="goal-1",
            engagement_id="project",
            session_id="session",
            objective=objective,
            completion_criteria=["The handler is covered by tests"],
            status=ChatGoalStatus.RUNNING,
        )
    )


def snapshot(store, goal, title="Step", payload=None, stream="ignored-by-core"):
    tool = PublishResultTool(store, goal)
    invocation = ToolInvocation(
        engagement_id="project",
        run_id="run",
        chat_session_id="session",
        tool_name=DASHBOARD_PUBLISH_TOOL_NAME,
        workspace=Path("/tmp"),
        arguments={
            "title": title,
            "result": payload or {"state": "working"},
            "stream": stream,
        },
    )
    return asyncio.run(tool.execute(invocation, runner=None)).output


def test_a_goals_snapshots_join_its_own_series_whatever_the_model_passes(tmp_path):
    store, client = fixture(tmp_path)
    goal = running_goal(store)

    first = snapshot(store, goal, title="Read the handler")
    second = snapshot(store, goal, title="Mapped the callers", stream="something-else")

    # Core owns the series: the model cannot split or merge one by accident.
    assert (first["stream"], first["sequence"]) == (goal.id, 1)
    assert (second["stream"], second["sequence"]) == (goal.id, 2)
    rows = client.get(f"/projects/project/structured-results?stream={goal.id}").json()
    assert [row["sequence"] for row in rows] == [2, 1]
    # The operator reads the objective, not the goal's identity.
    assert {row["stream_label"] for row in rows} == {"Refactor the auth handler"}


def test_an_unbound_tool_still_honours_the_stream_a_producer_chose(tmp_path):
    store, _ = fixture(tmp_path)
    tool = PublishResultTool(store)
    invocation = ToolInvocation(
        engagement_id="project",
        run_id="run",
        tool_name=DASHBOARD_PUBLISH_TOOL_NAME,
        workspace=tmp_path,
        arguments={"title": "Ad hoc", "result": {"a": 1}, "stream": "chosen"},
    )
    output = asyncio.run(tool.execute(invocation, runner=None)).output
    assert output["stream"] == "chosen"
    assert store.get(StructuredResult, output["result_id"]).stream_label == ""


def test_a_running_goal_is_asked_for_a_snapshot_only_once_per_interval(tmp_path):
    store, _ = fixture(tmp_path)
    goal = running_goal(store)

    # Nothing published yet: the first goal turn is asked immediately.
    opening = goal_snapshot_instruction(store, goal)
    assert DASHBOARD_PUBLISH_TOOL_NAME in opening
    assert "Nothing has been published for this goal yet" in opening
    assert "every 10 minutes" in opening

    snapshot(store, goal)
    published = store.get(
        StructuredResult, latest_snapshot(store, "project", goal.id).id
    )

    # Inside the interval the model is left alone to work.
    soon = published.created_at + timedelta(seconds=SNAPSHOT_INTERVAL_SECONDS - 1)
    assert goal_snapshot_instruction(store, goal, now=soon) == ""

    later = published.created_at + timedelta(seconds=SNAPSHOT_INTERVAL_SECONDS + 60)
    due = goal_snapshot_instruction(store, goal, now=later)
    assert DASHBOARD_PUBLISH_TOOL_NAME in due
    assert "11 minutes ago" in due

    overdue, elapsed = snapshot_overdue(store, "project", goal.id, now=later)
    assert (
        overdue is True and elapsed is not None and elapsed > SNAPSHOT_INTERVAL_SECONDS
    )


def test_only_a_running_goal_is_asked_at_all(tmp_path):
    store, _ = fixture(tmp_path)
    goal = running_goal(store)
    for status in [
        ChatGoalStatus.PAUSED,
        ChatGoalStatus.BLOCKED,
        ChatGoalStatus.COMPLETED,
        ChatGoalStatus.CANCELLED,
        ChatGoalStatus.DRAFT,
    ]:
        stopped = goal.model_copy(update={"status": status, "blocked_reason": "held"})
        assert goal_snapshot_instruction(store, stopped) == ""
    assert goal_snapshot_instruction(store, goal) != ""


def test_publishing_is_offered_to_a_goal_turn_and_to_nothing_else(
    tmp_path, monkeypatch
):
    import nebula.v3.chat as chat_module
    from nebula.v3.chat import ChatCompletionRequest, ChatService
    from nebula.v3.domain import ProviderProfile
    from tests.v3.test_chat import FakeProvider, _profile

    store = NebulaStore(tmp_path / "goal-tools.db")
    workspace = tmp_path / "workspace"
    skill_path = workspace / ".agents" / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    # A skill that links a resource is the lightest runtime yielding tools.
    skill_path.write_text(
        "Review only changed files. See [checklist](references/checklist.md).",
        encoding="utf-8",
    )
    reference = skill_path.parent / "references" / "checklist.md"
    reference.parent.mkdir()
    reference.write_text("Check the diff.", encoding="utf-8")
    store.create(Engagement(id="project", name="Project"))
    payload = _profile(local=True).model_dump(mode="python")
    payload["capabilities"]["tool_calling"] = True
    payload["capability_verifications"] = {
        "model-a": {"model": "model-a", "status": "verified"}
    }
    profile = store.create(ProviderProfile.model_validate(payload))
    for identity in ("session", "session-plain"):
        store.create(
            ChatSession(
                id=identity,
                engagement_id="project",
                title="Goal chat",
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
    goal = store.create(
        ChatGoal(
            id="goal-1",
            engagement_id="project",
            session_id="session",
            objective="Refactor the auth handler",
            completion_criteria=["The handler is covered by tests"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    provider = FakeProvider(profile.id, local=True)
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(store, workspace_resolver=lambda _: workspace)

    def prepare(session_id="session", **extra):
        return service.prepare(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id="project",
                session_id=session_id,
                skill={"name": "review", "path": str(skill_path.resolve())},
                messages=[{"role": "user", "content": "$review inspect it"}],
                include_knowledge=False,
                stream=True,
                **extra,
            )
        )

    inside = prepare(goal_id=goal.id)
    assert set(inside.tool_components.specs) == {
        "skill.read_resource",
        DASHBOARD_PUBLISH_TOOL_NAME,
    }
    # The goal turn is also told to show the operator where the work stands.
    assert DASHBOARD_PUBLISH_TOOL_NAME in (inside.model_request.instructions or "")

    # A conversation with the same runtime but no goal is not offered it.
    outside = prepare(session_id="session-plain")
    assert set(outside.tool_components.specs) == {"skill.read_resource"}
    assert DASHBOARD_PUBLISH_TOOL_NAME not in (outside.model_request.instructions or "")
