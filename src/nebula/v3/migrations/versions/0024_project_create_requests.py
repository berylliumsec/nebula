"""Persist project-create retry identities in the same transaction as projects."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0024_project_create_requests"
down_revision = "0023_workspace_provenance_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_create_requests",
        sa.Column("actor_id", sa.String(200), primary_key=True),
        sa.Column("request_id", sa.String(200), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("engagement_id", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM project_create_requests"))
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "project-create receipts contain authoritative retry state; "
            "restore a backup to downgrade"
        )
    op.drop_table("project_create_requests")
