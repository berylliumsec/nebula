"""Execution capabilities required by durable project context, beyond SQL shape."""

from sqlalchemy import or_, select
from sqlalchemy.orm import aliased

from .database import (
    EntityRow,
    ProviderToolManifestRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
    RuntimeWorkBatchProjectRow,
)
from .deployment_protocol import ReleaseManifest
from .chat_snapshot_parts import TOOLS_OWNERSHIP_KEY
from .runtime_coordination import CoordinationConflict

PROVIDER_TOOLS_FEATURE = "provider.tools-context/v1"
PROVIDER_TOOLS_OWNERSHIP_FEATURE = "provider.tools-context-ownership/v1"
STANDING_INSTRUCTIONS_FEATURE = "harness.standing-instructions/v1"
HOST_SEARCH_FEATURE = "host.web-search-query/v1"
WORK_BATCH_FEATURE = "project.work-batch/v1"


async def required_context_features(session, project_id: str) -> set[str]:
    required = set()
    if (
        await session.scalar(
            select(RuntimeWorkBatchProjectRow.batch_id)
            .where(
                RuntimeWorkBatchProjectRow.project_id == project_id,
                RuntimeWorkBatchProjectRow.state.in_(("waiting", "barrier")),
            )
            .limit(1)
        )
        is not None
    ):
        required.add(WORK_BATCH_FEATURE)
    if (
        await session.scalar(
            select(ProviderToolManifestRow.session_id)
            .where(ProviderToolManifestRow.project_id == project_id)
            .limit(1)
        )
        is not None
    ):
        # Historical revisions also own fork/recovery knowledge, including an
        # empty current revision after source revocation.
        required.add(PROVIDER_TOOLS_FEATURE)
    turn = EntityRow
    part = aliased(EntityRow)
    snapshot = turn.payload["request_snapshot"]
    referenced_proof = (
        select(part.id)
        .where(
            part.id == snapshot["snapshot_parts"]["model_request"].as_string(),
            part.kind == "chat_snapshot_parts",
            part.engagement_id == project_id,
            part.engagement_id == turn.engagement_id,
            part.payload["session_id"].as_string()
            == turn.payload["session_id"].as_string(),
            part.payload["value"]["core_tools_section"]["version"].as_integer() == 1,
        )
        .exists()
    )
    if (
        await session.scalar(
            select(turn.id)
            .where(
                turn.kind == "chat_turns",
                turn.engagement_id == project_id,
                or_(
                    snapshot[TOOLS_OWNERSHIP_KEY].as_integer() == 1,
                    snapshot["model_request"]["core_tools_section"][
                        "version"
                    ].as_integer()
                    == 1,
                    referenced_proof,
                ),
            )
            .limit(1)
        )
        is not None
    ):
        # The inline projection covers new writers; exact inline/referenced
        # request proofs also fence previously stored unprojected snapshots.
        required.add(PROVIDER_TOOLS_OWNERSHIP_FEATURE)
    if (
        await session.scalar(
            select(EntityRow.id)
            .where(
                EntityRow.engagement_id == project_id,
                EntityRow.kind.in_(("harness_sessions", "harness_turns")),
                EntityRow.payload["metadata"]["standing_instructions"]["sha256"]
                .as_string()
                .is_not(None),
            )
            .limit(1)
        )
        is not None
    ):
        required.add(STANDING_INSTRUCTIONS_FEATURE)
    project = aliased(EntityRow)
    if (
        await session.scalar(
            select(EntityRow.id)
            .join(
                project, project.payload["scope_policy_id"].as_string() == EntityRow.id
            )
            .where(
                project.id == project_id,
                project.kind == "engagements",
                EntityRow.kind == "scope_policies",
                EntityRow.engagement_id == project_id,
                EntityRow.payload["web_search"].as_boolean().is_(True),
                EntityRow.payload["local_only"].as_boolean().is_not(True),
            )
            .limit(1)
        )
        is not None
    ):
        required.add(HOST_SEARCH_FEATURE)
    return required


async def require_runtime_context(session, project_id: str, runtime_id: str) -> None:
    required = await required_context_features(session, project_id)
    if not required:
        return
    process = await session.get(RuntimeProcessRow, runtime_id)
    release = await session.get(RuntimeReleaseRow, process.version) if process else None
    available = (
        set(ReleaseManifest.model_validate(release.manifest).features)
        if release
        else set()
    )
    missing = required - available
    if missing:
        raise CoordinationConflict(
            "Retain this project's execution version: the target cannot preserve "
            + ", ".join(sorted(missing))
        )
