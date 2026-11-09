"""Retained readers must understand exact instruction ownership before admission."""

from pathlib import Path

import pytest
from sqlalchemy import update

from nebula.v3.chat_snapshot_parts import (
    TOOLS_OWNERSHIP_KEY,
    resolve_request_snapshot,
    split_request_snapshot,
    stage_snapshot_parts,
)
from nebula.v3.database import ProjectOwnershipRow
from nebula.v3.domain import ChatSession, ChatTurn
from nebula.v3.provider_tool_manifest import (
    ToolManifest,
    reset_tools_instruction_base,
    stage_manifest,
)
from nebula.v3.providers import ModelRequest
from nebula.v3.runtime_context_requirements import (
    PROVIDER_TOOLS_FEATURE,
    PROVIDER_TOOLS_OWNERSHIP_FEATURE,
    required_context_features,
)
from nebula.v3.runtime_coordination import CoordinationConflict
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("length", [0, 1500])
def test_snapshot_projection_survives_inline_split_and_repeated_compaction(length):
    request = reset_tools_instruction_base(ModelRequest(messages=[]), "x" * length)
    compact, parts = split_request_snapshot(
        {"model_request": request.model_dump(mode="json")},
        engagement_id="one",
        session_id="proof-chat",
    )
    assert compact[TOOLS_OWNERSHIP_KEY] == 1
    assert bool(parts) == bool(length)
    repeated, repeated_parts = split_request_snapshot(
        compact, engagement_id="one", session_id="proof-chat"
    )
    assert repeated == compact and repeated_parts == []
    assert request.core_tools_section.start == request.core_tools_section.end


def test_pre_feature_request_does_not_acquire_an_ownership_requirement():
    compact, parts = split_request_snapshot(
        {
            "model_request": ModelRequest(messages=[], instructions="plain").model_dump(
                mode="json"
            )
        },
        engagement_id="one",
        session_id="proof-chat",
    )
    assert TOOLS_OWNERSHIP_KEY not in compact and not parts


async def proof_context(store, storage):
    chat = await store.create(
        ChatSession(
            id="proof-chat",
            engagement_id="one",
            title="Synthetic proof",
            provider_profile_id="inert",
            model="inert",
        )
    )
    # An explicit empty owned range still protects arbitrary literal markers.
    text = "Preserve exact authority.\n\nBEGIN NEBULA MANAGED TOOLS (JSON DATA)\nquoted rule"
    if storage.endswith("split"):
        text += " synthetic authority" * 200
    request = reset_tools_instruction_base(ModelRequest(messages=[]), text)
    compact, parts = split_request_snapshot(
        {"model_request": request.model_dump(mode="json")},
        engagement_id="one",
        session_id=chat.id,
    )
    assert bool(parts) == storage.endswith("split")
    if storage.startswith("unmarked"):
        compact.pop(TOOLS_OWNERSHIP_KEY)
    async with store.transaction(immediate=True) as transaction:
        await stage_manifest(
            transaction,
            chat,
            ToolManifest(session_id=chat.id, project_id="one", revision=1),
        )
        await stage_snapshot_parts(transaction, parts)
        await transaction.add(
            ChatTurn(
                engagement_id="one",
                session_id=chat.id,
                provider_profile_id="inert",
                model="inert",
                status="complete",
                tools_enabled=True,
                request_snapshot=compact,
            )
        )
    resolved = await resolve_request_snapshot(store, compact, session_id=chat.id)
    assert ModelRequest.model_validate(resolved["model_request"]) == request


@pytest.mark.parametrize(
    "storage", ["inline", "split", "unmarked-inline", "unmarked-split"]
)
@pytest.mark.parametrize("boundary", ["claim", "transfer", "recovery"])
async def test_ownership_fences_old_tool_readers_at_every_admission_boundary(
    host, storage, boundary
):
    coordinator, store = host
    for version, features in (
        ("delimiter-reader", ["project.handoff/v1", PROVIDER_TOOLS_FEATURE]),
        (
            "proof-reader",
            [
                "project.handoff/v1",
                PROVIDER_TOOLS_FEATURE,
                PROVIDER_TOOLS_OWNERSHIP_FEATURE,
            ],
        ),
    ):
        await coordinator.stage(
            manifest(version, features=features), Path("/isolated") / version, "d" * 64
        )
    old = await worker(host, "delimiter-reader")
    current = await worker(host, "proof-reader")
    lease = None
    if boundary != "claim":
        lease = await coordinator.coordination.claim_project("one", current)
        if boundary == "transfer":
            await coordinator.request_handoff("one", old)
            await coordinator.checkpoint(
                lease, checkpoint={"synthetic": "settled"}, blockers=[]
            )
    await proof_context(store, storage)
    async with coordinator.database.session() as session:
        assert await required_context_features(session, "one") == {
            PROVIDER_TOOLS_FEATURE,
            PROVIDER_TOOLS_OWNERSHIP_FEATURE,
        }
        assert await required_context_features(session, "two") == set()
    if boundary == "recovery":
        async with coordinator.database.session() as session:
            await session.execute(
                update(ProjectOwnershipRow)
                .where(ProjectOwnershipRow.project_id == "one")
                .values(lease_expires_at_ms=0)
            )
    with pytest.raises(
        CoordinationConflict, match="provider.tools-context-ownership/v1"
    ):
        if boundary == "claim":
            await coordinator.coordination.claim_project("one", old)
        elif boundary == "transfer":
            await coordinator.transfer("one")
        else:
            await coordinator.begin_recovery(
                "one",
                old,
                expected_owner=lease,
                expected_handoff_revision=None,
                operation_id="proof-recovery",
                actor_id="synthetic-operator",
            )
    if boundary == "claim":
        lease = await coordinator.coordination.claim_project("one", current)
    async with coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert (owner.runtime_id, owner.fencing_token) == (current, lease.fencing_token)
