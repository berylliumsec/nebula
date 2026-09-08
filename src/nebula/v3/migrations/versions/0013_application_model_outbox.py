"""Transactional application model projection queue."""

from alembic import op
import sqlalchemy as sa

revision = "0013_application_model_outbox"
down_revision = "0012_search_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "application_model_outbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("engagement_id", sa.String(200), nullable=False),
        sa.Column("model_session_id", sa.String(200), nullable=False),
        sa.Column("source_kind", sa.String(80), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("source_revision", sa.Integer, nullable=False),
        sa.Column("adapter_version", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "model_session_id",
            "source_kind",
            "source_id",
            "source_revision",
            "adapter_version",
            name="uq_application_model_source",
        ),
    )
    op.create_index(
        "ix_application_model_pending",
        "application_model_outbox",
        ["model_session_id", "status", "created_at"],
    )


def downgrade():
    op.drop_index("ix_application_model_pending", table_name="application_model_outbox")
    op.drop_table("application_model_outbox")
