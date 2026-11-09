"""Provider-session schema knowledge, independent of transcript compaction."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import json

import pytest
from sqlalchemy import select

from nebula.v3.domain import ChatSession, Engagement
from nebula.v3.database import ProviderToolManifestRow
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_coordination import LeaseLost
from nebula.v3.providers import ModelMessage, ModelRequest, ModelToolResult
from nebula.v3.provider_tool_manifest import (
    SECTION_START,
    ToolManifest,
    absent_candidates,
    apply_tools_context,
    managed_load_result,
    read_manifest,
    reconcile,
    render_tools,
    spec_digest,
    stage_manifest,
    legacy_present_names,
)
from nebula.v3.storage import ConflictError, NebulaStore
from nebula.v3.tool_catalog import fingerprint
from tests.v3.test_tool_suggestions import MCP_TOOL, _spec
from tests.v3.test_execution_scope import execution as execution
from tests.v3.test_runtime_coordination import location as location


def _manifest(spec=None, *, loaded=False):
    spec = spec or _spec(MCP_TOOL, "Exact description.\n  Keep all spacing.")
    return reconcile(
        ToolManifest(session_id="chat", project_id="project"),
        {spec.name: spec},
        native=[],
        deferred=[spec.name],
        introduce=[spec.name] if loaded else [],
    )


def test_one_canonical_section_preserves_the_complete_definition():
    description = "Keep whitespace.\n  " + "long description " * 900
    spec = _spec(MCP_TOOL, description).model_copy(
        update={
            "input_schema": {
                "type": "object",
                "properties": {
                    "precise_field": {
                        "type": "string",
                        "pattern": "^[A-Z]{3}$",
                        "minLength": 3,
                        "maxLength": 3,
                        "enum": ["ABC", "XYZ"],
                    }
                },
                "required": ["precise_field"],
                "additionalProperties": False,
            }
        }
    )
    manifest = _manifest(spec, loaded=True)
    request = ModelRequest(messages=[ModelMessage(role="user", content="Use it")])
    once = apply_tools_context(request, manifest)
    twice = apply_tools_context(once, manifest)
    assert once == twice
    assert once.instructions.count(SECTION_START) == 1
    value = json.loads(once.instructions.split(SECTION_START)[1].split("\n")[0])
    assert value["loaded"][0]["description"] == description
    assert value["loaded"][0]["input_schema"] == spec.input_schema
    assert value["available_not_loaded"] == []
    assert spec.name not in {tool.name for tool in once.tools}


@pytest.mark.parametrize("constraint", ["required", "enum", "pattern", "maximum"])
def test_complete_digest_tracks_constraints_the_search_fingerprint_omits(constraint):
    spec = _spec(MCP_TOOL, "Search tracker issues.")
    schema = {
        **spec.input_schema,
        constraint: ["changed"] if constraint in {"required", "enum"} else 7,
    }
    changed = spec.model_copy(update={"input_schema": schema})
    assert fingerprint(changed) == fingerprint(spec)
    assert spec_digest(changed) != spec_digest(spec)


def test_suggestion_does_not_establish_presence_and_native_schema_is_not_prose():
    spec = _spec(MCP_TOOL, "Search.")
    manifest = _manifest(spec)
    request = apply_tools_context(
        ModelRequest(messages=[]), manifest, receipt={"suggested": [MCP_TOOL]}
    )
    assert manifest.present_digests == {}
    assert absent_candidates({MCP_TOOL: spec}, manifest) == {MCP_TOOL: spec}
    assert '"input_schema"' not in request.instructions
    native = reconcile(
        manifest,
        {MCP_TOOL: spec},
        native=[manifest.definitions[0].definition()],
        deferred=[],
    )
    assert render_tools(native) == ""
    assert absent_candidates({MCP_TOOL: spec}, native) == {}


def test_reconcile_update_disable_reenable_and_order_are_deterministic():
    spec = _spec(MCP_TOOL, "Search.")
    first = _manifest(spec, loaded=True)
    assert reconcile(first, {spec.name: spec}, native=[], deferred=[spec.name]) == first
    changed = spec.model_copy(update={"description": "New exact description"})
    updated = reconcile(first, {spec.name: changed}, native=[], deferred=[spec.name])
    assert updated.revision == first.revision + 1
    assert updated.definitions[0].schema_present
    assert updated.definitions[0].introduction_reason == "definition_update"
    disabled = reconcile(updated, {}, native=[], deferred=[])
    assert disabled.definitions == []
    enabled = reconcile(disabled, {spec.name: changed}, native=[], deferred=[spec.name])
    assert not enabled.definitions[0].schema_present
    assert absent_candidates({spec.name: changed}, enabled) == {spec.name: changed}
    second = _spec("mcp.z.second", "Second")
    a = reconcile(
        enabled,
        {second.name: second, spec.name: changed},
        native=[],
        deferred=[second.name, spec.name],
    )
    b = reconcile(
        enabled,
        {spec.name: changed, second.name: second},
        native=[],
        deferred=[spec.name, second.name],
    )
    assert render_tools(a) == render_tools(b)
    assert a.digest == b.digest


def test_load_replay_retains_the_call_without_a_second_schema_copy():
    manifest = _manifest(loaded=True)
    spec = manifest.definitions[0]
    result = ModelToolResult(
        call_id="load",
        name="tool_catalog.load",
        arguments={"names": [MCP_TOOL]},
        output={
            "loaded": [
                {
                    "name": MCP_TOOL,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                }
            ]
        },
    )
    rendered = managed_load_result(result, manifest)
    assert rendered.call_id == result.call_id and rendered.arguments == result.arguments
    assert "input_schema" not in rendered.output["loaded"][0]
    assert rendered.output["loaded"][0]["definition_digest"] == spec.definition_digest
    assert managed_load_result(
        result.model_copy(update={"is_error": True}), manifest
    ).is_error


def test_legacy_membership_uses_durable_selection_and_successful_results():
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    snapshot = {
        "available_tool_names": ["native", "deferred", "hint"],
        "tool_catalog": {
            "deferred": ["deferred", "hint"],
            "preloaded": ["deferred"],
            "suggested": ["hint"],
        },
        "operator_text": "The schema for prose_only has been loaded",
    }
    history = [
        {
            "name": "tool_catalog.load",
            "status": "failed",
            "arguments": {"names": ["failed"]},
        },
        {
            "name": "tool_catalog.load",
            "status": "complete",
            "arguments": {"names": ["no_result"]},
        },
        {
            "name": "tool_catalog.load",
            "status": "complete",
            "provider_result": json.dumps(
                {
                    "loaded": [
                        {
                            "name": "loaded",
                            "description": "Loaded",
                            "input_schema": schema,
                        },
                    ]
                }
            ),
        },
        {"name": "direct_call_without_schema", "status": "complete"},
    ]
    assert legacy_present_names(snapshot, history) == {"native", "deferred", "loaded"}


async def _owner(store):
    await store.create(Engagement(id="project", name="Synthetic tools"))
    return await store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Tools",
            model="inert",
            provider_profile_id="inert",
        )
    )


async def test_manifest_revisions_commit_once_and_reload_exactly_after_restart(
    tmp_path,
):
    location = tmp_path / "manifest.db"
    store = NebulaStore(location)
    owner = await _owner(store)
    first = _manifest()
    async with store.transaction(immediate=True) as transaction:
        assert await stage_manifest(transaction, owner, first) == first
    loaded = reconcile(
        first,
        {MCP_TOOL: _spec(MCP_TOOL, first.definitions[0].description)},
        native=[],
        deferred=[MCP_TOOL],
        introduce=[MCP_TOOL],
        reason="catalog_load",
    )
    async with store.transaction(immediate=True) as transaction:
        assert await stage_manifest(transaction, owner, loaded) == loaded
        assert await stage_manifest(transaction, owner, loaded) == loaded
    await store.database.dispose()
    restarted = NebulaStore(location)
    async with restarted.database.session() as session:
        assert (
            await read_manifest(
                session, project_id=owner.engagement_id, session_id=owner.id
            )
            == loaded
        )
        assert (
            await read_manifest(
                session,
                project_id=owner.engagement_id,
                session_id=owner.id,
                revision=first.revision,
            )
            == first
        )
        assert (
            await read_manifest(session, project_id="other", session_id=owner.id)
        ).revision == 0


async def test_manifest_conflict_and_failed_transaction_never_overwrite_knowledge(
    tmp_path,
):
    store = NebulaStore(tmp_path / "manifest.db")
    owner = await _owner(store)
    first = _manifest(loaded=True)
    with pytest.raises(RuntimeError, match="rollback fixture"):
        async with store.transaction(immediate=True) as transaction:
            await stage_manifest(transaction, owner, first)
            raise RuntimeError("rollback fixture")
    async with store.database.session() as session:
        assert (
            await read_manifest(
                session, project_id=owner.engagement_id, session_id=owner.id
            )
        ).revision == 0
    async with store.transaction(immediate=True) as transaction:
        await stage_manifest(transaction, owner, first)
    stale = _manifest(_spec(MCP_TOOL, "Stale definition"), loaded=True)
    with pytest.raises(ConflictError, match="knowledge changed"):
        async with store.transaction(immediate=True) as transaction:
            await stage_manifest(transaction, owner, stale)
    async with store.database.session() as session:
        assert (
            await read_manifest(
                session, project_id=owner.engagement_id, session_id=owner.id
            )
            == first
        )


async def test_manifest_table_participates_in_execution_project_fencing(execution):
    control, runtime, _, lease = execution
    owners = []
    for project in ("mine", "other"):
        owner = await control.create(
            ChatSession(
                id=f"manifest-{project}",
                engagement_id=project,
                title=project,
                model="inert",
                provider_profile_id="provider",
            )
        )
        manifest = _manifest(loaded=True).model_copy(
            update={"session_id": owner.id, "project_id": project}
        )
        async with control.transaction(immediate=True) as transaction:
            await stage_manifest(transaction, owner, manifest)
        owners.append(owner)
    with execution_context(lease):
        async with runtime.database.session() as session:
            rows = list(await session.scalars(select(ProviderToolManifestRow)))
            assert [(row.session_id, row.project_id) for row in rows] == [
                (owners[0].id, "mine")
            ]
            assert (
                await read_manifest(
                    session, project_id="other", session_id=owners[1].id
                )
            ).revision == 0
        with pytest.raises(LeaseLost, match="unowned project"):
            async with runtime.database.session() as session:
                session.add(
                    ProviderToolManifestRow(
                        session_id=owners[1].id,
                        project_id="other",
                        revision=2,
                        definitions=[],
                        digest="0" * 64,
                    )
                )


async def test_concurrent_manifest_writers_serialize_and_identical_retries_converge(
    tmp_path,
):
    store = NebulaStore(tmp_path / "manifest.db")
    owner = await _owner(store)
    first = _manifest(loaded=True)
    second = _manifest(_spec(MCP_TOOL, "Concurrent different definition"), loaded=True)

    async def write(candidate):
        try:
            async with store.transaction(immediate=True) as transaction:
                return await stage_manifest(transaction, owner, candidate)
        except ConflictError:
            return None

    results = await asyncio.gather(write(first), write(first), write(second))
    successes = [item for item in results if item is not None]
    assert successes and all(item.digest == successes[0].digest for item in successes)
    async with store.database.session() as session:
        latest = await read_manifest(
            session, project_id=owner.engagement_id, session_id=owner.id
        )
    assert latest.revision == 1
    assert latest == successes[0]


async def test_native_harness_session_cannot_enter_provider_manifest_storage(tmp_path):
    from nebula.v3.domain import ChatBackend

    store = NebulaStore(tmp_path / "native.db")
    await store.create(Engagement(id="project", name="Native unchanged"))
    owner = await store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Native",
            model="native",
            backend=ChatBackend.HARNESS,
            harness_profile_id="profile",
            harness_session_id="native",
        )
    )
    with pytest.raises(ValueError, match="native harnesses"):
        async with store.transaction(immediate=True) as transaction:
            await stage_manifest(transaction, owner, _manifest(loaded=True))
    async with store.database.session() as session:
        assert list(await session.scalars(select(ProviderToolManifestRow))) == []
