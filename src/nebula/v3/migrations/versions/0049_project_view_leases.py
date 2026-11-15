"""Count an open project view as worker demand, and say why admission waits.

Additive and rollback-safe: an older release never reads these tables, so its
admission keeps the pre-lease behaviour; downgrade drops them.
"""

from alembic import op
import sqlalchemy as sa

revision = "0049_project_view_leases"
down_revision = "0048_chat_scoped_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_view_leases",
        sa.Column("actor_id", sa.String(200), primary_key=True),
        sa.Column("lease_id", sa.String(100), primary_key=True),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("renewed_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_project_view_lease_project",
        "project_view_leases",
        ["project_id", "expires_at_ms"],
    )
    op.create_table(
        "project_admission_holds",
        sa.Column("project_id", sa.String(200), primary_key=True),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )


def downgrade():
    op.drop_table("project_admission_holds")
    op.drop_index("ix_project_view_lease_project", table_name="project_view_leases")
    op.drop_table("project_view_leases")
