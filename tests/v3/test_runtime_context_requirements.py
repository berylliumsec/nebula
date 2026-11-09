"""Readable SQL must not permit a runtime to discard durable context semantics."""

from pathlib import Path

import pytest
from sqlalchemy import update

from nebula.v3.database import ProjectOwnershipRow
from nebula.v3.domain import (
    ChatSession,
    Engagement,
    HarnessSession,
    HarnessTurn,
    ScopePolicy,
)
from nebula.v3.harness_instructions import instruction_snapshot
from nebula.v3.provider_tool_manifest import ToolManifest, stage_manifest
from nebula.v3.runtime_context_requirements import (
    HOST_SEARCH_FEATURE,
    PROVIDER_TOOLS_FEATURE,
    STANDING_INSTRUCTIONS_FEATURE,
)
from nebula.v3.runtime_coordination import CoordinationConflict
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
FEATURES = (PROVIDER_TOOLS_FEATURE, STANDING_INSTRUCTIONS_FEATURE, HOST_SEARCH_FEATURE)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def context(host, feature):
    coordinator, store = host
    if feature == PROVIDER_TOOLS_FEATURE:
        owner = await store.create(
            ChatSession(
                id="context-chat",
                engagement_id="one",
                title="Synthetic context",
                model="inert",
                provider_profile_id="inert",
            )
        )
        async with store.transaction(immediate=True) as transaction:
            await stage_manifest(
                transaction,
                owner,
                ToolManifest(session_id=owner.id, project_id="one", revision=1),
            )
    elif feature == STANDING_INSTRUCTIONS_FEATURE:
        native = await store.create(
            HarnessSession(
                engagement_id="one",
                harness_profile_id="inert",
                model="inert",
                status="closed",
            )
        )
        await store.create(
            HarnessTurn(
                id="context-native-turn",
                engagement_id="one",
                harness_session_id=native.id,
                origin="analysis",
                prompt="Synthetic retained context",
                metadata={
                    "standing_instructions": instruction_snapshot(
                        "Preserve the exact synthetic rule."
                    )
                },
            )
        )
    else:
        policy = await store.create(ScopePolicy(engagement_id="one", web_search=True))
        project = await store.get(Engagement, "one")
        await store.update(
            Engagement,
            project.id,
            {"scope_policy_id": policy.id},
            expected_revision=project.revision,
        )


async def capable(host):
    await host[0].stage(
        manifest("context-aware", features=["project.handoff/v1", *FEATURES]),
        Path("/isolated/context-aware"),
        "d" * 64,
    )
    return await worker(host, "context-aware")


@pytest.mark.parametrize("feature", FEATURES)
async def test_first_claim_requires_durable_project_context_support(host, feature):
    coordinator, _ = host
    old = await worker(host, "v1")
    current = await capable(host)
    await context(host, feature)
    with pytest.raises(CoordinationConflict, match=feature.replace(".", r"\.")):
        await coordinator.coordination.claim_project("one", old)
    lease = await coordinator.coordination.claim_project("one", current)
    assert lease.runtime_id == current


@pytest.mark.parametrize("feature", FEATURES)
async def test_transfer_rechecks_context_created_after_preflight(host, feature):
    coordinator, _ = host
    current = await capable(host)
    old = await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("one", current)
    await coordinator.request_handoff("one", old)
    await coordinator.checkpoint(
        lease, checkpoint={"synthetic": "settled"}, blockers=[]
    )
    await context(host, feature)
    with pytest.raises(CoordinationConflict, match=feature.replace(".", r"\.")):
        await coordinator.transfer("one")
    async with coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert (owner.runtime_id, owner.fencing_token) == (current, lease.fencing_token)


@pytest.mark.parametrize("feature", FEATURES)
async def test_expired_recovery_keeps_context_on_an_incompatible_target(host, feature):
    coordinator, _ = host
    current = await capable(host)
    old = await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("one", current)
    await context(host, feature)
    async with coordinator.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "one")
            .values(lease_expires_at_ms=0)
        )
    with pytest.raises(CoordinationConflict, match=feature.replace(".", r"\.")):
        await coordinator.begin_recovery(
            "one",
            old,
            expected_owner=lease,
            expected_handoff_revision=None,
            operation_id="context-recovery",
            actor_id="synthetic-operator",
        )
    async with coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert (owner.runtime_id, owner.fencing_token) == (current, lease.fencing_token)
