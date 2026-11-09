"""Make shared Butler memory and cursors part of fenced SQL authority."""

from alembic import op
import sqlalchemy as sa

revision = "0037_shared_butler_memory"
down_revision = "0036_runtime_capacity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "butler_memory_items",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("scope", sa.String(30), nullable=False),
        sa.Column("scope_key", sa.String(200), nullable=False),
        sa.Column("source_project_id", sa.String(200), nullable=False),
        sa.Column("content", sa.String(800), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(100), nullable=False),
        sa.Column("source_label", sa.String(500), nullable=False),
        sa.Column("source_ref", sa.String(1000)),
        sa.Column("created_at", sa.String(50), nullable=False),
        sa.Column("updated_at", sa.String(50), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "scope", "scope_key", "content_hash", name="uq_butler_memory_content"
        ),
    )
    op.create_index(
        "ix_butler_memory_scope",
        "butler_memory_items",
        ["scope", "scope_key", "updated_at"],
    )
    op.create_table(
        "butler_memory_cursors",
        sa.Column("project_id", sa.String(200), primary_key=True),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
    )


def downgrade():
    raise RuntimeError(
        "Keep current Butler memories and cursors during compatible code rollback"
    )
