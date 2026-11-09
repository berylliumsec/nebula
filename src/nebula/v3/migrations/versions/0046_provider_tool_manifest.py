"""Keep provider-chat tool knowledge outside summarizable history."""

from alembic import op
import sqlalchemy as sa

revision = "0046_provider_tool_manifest"
down_revision = "0045_runtime_shutdown"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_tool_manifests",
        sa.Column(
            "session_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("definitions", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_provider_tools_project_session",
        "provider_tool_manifests",
        ["project_id", "session_id"],
    )


def downgrade():
    raise RuntimeError("Retain provider tool knowledge; use compatible code rollback")
