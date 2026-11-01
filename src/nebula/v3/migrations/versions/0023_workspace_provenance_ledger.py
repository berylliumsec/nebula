"""Add compact, indexed workspace provenance storage without scanning old rows.

The cutover deliberately leaves historical JSON observations in ``entities``.
Core writes new observations with shared path chunks and uses only this index
for attribution, so opening a large existing database does not trigger a
backfill or load its previous full snapshots.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0023_workspace_provenance_ledger"
down_revision = "0022_cached_usage_rollup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_provenance_blobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "engagement_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_workspace_provenance_blobs_engagement",
        "workspace_provenance_blobs",
        ["engagement_id"],
    )
    op.create_table(
        "workspace_provenance_index",
        sa.Column(
            "observation_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "engagement_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("scope_id", sa.String(200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_workspace_provenance_overlap",
        "workspace_provenance_index",
        ["workspace_key", "started_at", "completed_at"],
    )
    op.create_index(
        "ix_workspace_provenance_index_engagement",
        "workspace_provenance_index",
        ["engagement_id"],
    )
    op.create_table(
        "workspace_provenance_owners",
        sa.Column(
            "engagement_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_key", sa.String(64), primary_key=True),
        sa.Column("path_key", sa.String(64), primary_key=True),
        sa.Column("state_key", sa.String(64), primary_key=True),
        sa.Column("path", sa.String(4096), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("confidence", sa.String(40), nullable=False),
        sa.Column(
            "observation_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_workspace_provenance_owners_engagement",
        "workspace_provenance_owners",
        ["engagement_id"],
    )
    op.create_index(
        "ix_workspace_provenance_owners_observation",
        "workspace_provenance_owners",
        ["observation_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in (
        "workspace_provenance_blobs",
        "workspace_provenance_index",
        "workspace_provenance_owners",
    ):
        count = bind.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one()
        if count:
            raise RuntimeError(
                "workspace provenance ledger contains authoritative data; "
                "restore a backup to downgrade"
            )
    op.drop_table("workspace_provenance_owners")
    op.drop_index(
        "ix_workspace_provenance_overlap", table_name="workspace_provenance_index"
    )
    op.drop_table("workspace_provenance_index")
    op.drop_table("workspace_provenance_blobs")
