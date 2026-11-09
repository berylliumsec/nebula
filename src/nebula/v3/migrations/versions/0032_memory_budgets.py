"""Durable memory policy, aggregate job reservations and application audit."""

from alembic import op
import sqlalchemy as sa

revision = "0032_memory_budgets"
down_revision = "0031_runtime_coordination"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_budget_policies",
        sa.Column("scope_key", sa.String(200), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "memory_jobs",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("project_id", sa.String(200)),
        sa.Column("resource_kind", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON()),
        sa.Column("launch_owner", sa.JSON()),
        sa.Column("launch_groups", sa.JSON()),
        sa.Column("pending_limits", sa.JSON()),
        sa.Column("application_state", sa.String(30), nullable=False),
        sa.Column("outcome", sa.String(40)),
        sa.Column("detail", sa.String(4000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_jobs_state", "memory_jobs", ["state", "created_at"])
    op.create_table(
        "memory_budget_audit",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("scope_key", sa.String(200), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("previous", sa.JSON()),
        sa.Column("proposed", sa.JSON()),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_memory_budget_audit_scope",
        "memory_budget_audit",
        ["scope_key", "created_at"],
    )


def downgrade():
    raise RuntimeError(
        "retain memory reservations and audit history during code rollback"
    )
