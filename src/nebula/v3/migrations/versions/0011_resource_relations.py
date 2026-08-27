"""Add the authoritative typed resource-relation store.

Revision ID: 0011_resource_relations
Revises: 0010_browser_automation_indexes
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0011_resource_relations"
down_revision = "0010_browser_automation_indexes"
branch_labels = None
depends_on = None

RESOURCE_KINDS = {
    "engagements": "project",
    "chat_sessions": "conversation",
    "observations": "note",
    "knowledge": "source",
    "library_items": "library_item",
    "assets": "asset",
    "evidence": "evidence",
    "findings": "finding",
    "reports": "report",
    "command_executions": "terminal_command",
    "browser_sessions": "browser_session",
    "browser_traffic_exchanges": "browser_exchange",
    "agent_runs": "mission",
    "operator_executions": "execution",
    "approvals": "approval",
    "artifacts": "artifact",
}


def _edge(
    project_id: str,
    source: tuple[str, str, int],
    predicate: str,
    target: tuple[str, str, int],
) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "project_id": project_id,
        "source_kind": source[0],
        "source_id": source[1],
        "source_revision": source[2],
        "predicate": predicate,
        "target_kind": target[0],
        "target_id": target[1],
        "target_revision": target[2],
        "attribution": "migration:legacy-array-backfill",
        "provenance": {"source": "legacy-array"},
        "revision": 1,
    }


def _backfill() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    entities = sa.Table("entities", metadata, autoload_with=bind)
    relations = sa.Table("resource_relations", metadata, autoload_with=bind)
    rows = list(bind.execute(sa.select(entities)).mappings())
    by_id = {str(row["id"]): row for row in rows}
    candidates: list[dict[str, object]] = []

    def endpoint(entity_id: str, expected_kind: str, project_id: str):
        row = by_id.get(entity_id)
        if row is None or row["kind"] != expected_kind:
            raise RuntimeError(
                f"dangling legacy relation: expected {expected_kind}/{entity_id}"
            )
        if row["engagement_id"] != project_id:
            raise RuntimeError(
                f"cross-project legacy relation: {entity_id} does not belong to {project_id}"
            )
        return (RESOURCE_KINDS[expected_kind], entity_id, int(row["revision"]))

    for row in rows:
        project_id = row["engagement_id"]
        payload = row["payload"] or {}
        if not project_id:
            continue
        source_kind = RESOURCE_KINDS.get(str(row["kind"]))
        if source_kind is None:
            continue
        source = (source_kind, str(row["id"]), int(row["revision"]))
        if row["kind"] == "findings":
            for target_id in payload.get("asset_ids", []):
                candidates.append(
                    _edge(
                        project_id,
                        source,
                        "affects",
                        endpoint(target_id, "assets", project_id),
                    )
                )
            for evidence_id in payload.get("evidence_ids", []):
                evidence = endpoint(evidence_id, "evidence", project_id)
                candidates.append(_edge(project_id, evidence, "supports", source))
        elif row["kind"] == "evidence" and payload.get("finding_id"):
            candidates.append(
                _edge(
                    project_id,
                    source,
                    "supports",
                    endpoint(payload["finding_id"], "findings", project_id),
                )
            )
        elif row["kind"] == "reports":
            for finding_id in payload.get("finding_ids", []):
                candidates.append(
                    _edge(
                        project_id,
                        source,
                        "includes",
                        endpoint(finding_id, "findings", project_id),
                    )
                )
            for note_id in payload.get("observation_ids", []):
                candidates.append(
                    _edge(
                        project_id,
                        source,
                        "includes",
                        endpoint(note_id, "observations", project_id),
                    )
                )

    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for item in candidates:
        key = tuple(
            item[name]
            for name in (
                "project_id",
                "source_kind",
                "source_id",
                "predicate",
                "target_kind",
                "target_id",
            )
        )
        unique[key] = item
    if unique:
        bind.execute(relations.insert(), list(unique.values()))


def upgrade() -> None:
    op.create_table(
        "resource_relations",
        sa.Column("id", sa.String(length=200), primary_key=True),
        sa.Column("project_id", sa.String(length=200), nullable=False),
        sa.Column("source_kind", sa.String(length=80), nullable=False),
        sa.Column("source_id", sa.String(length=4096), nullable=False),
        sa.Column("source_revision", sa.Integer()),
        sa.Column("predicate", sa.String(length=80), nullable=False),
        sa.Column("target_kind", sa.String(length=80), nullable=False),
        sa.Column("target_id", sa.String(length=4096), nullable=False),
        sa.Column("target_revision", sa.Integer()),
        sa.Column("attribution", sa.String(length=200)),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "project_id",
            "source_kind",
            "source_id",
            "predicate",
            "target_kind",
            "target_id",
            name="uq_resource_relations_edge",
        ),
    )
    op.create_index(
        "ix_resource_relations_source",
        "resource_relations",
        ["project_id", "source_kind", "source_id", "predicate"],
    )
    op.create_index(
        "ix_resource_relations_target",
        "resource_relations",
        ["project_id", "target_kind", "target_id", "predicate"],
    )
    op.create_index(
        "ix_resource_relations_lineage",
        "resource_relations",
        ["project_id", "created_at"],
    )
    _backfill()


def downgrade() -> None:
    op.drop_table("resource_relations")
