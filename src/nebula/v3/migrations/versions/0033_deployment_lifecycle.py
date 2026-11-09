"""Add host deployment inventory, handoff checkpoints and independent controls."""

from alembic import op
import sqlalchemy as sa

revision = "0033_deployment_lifecycle"
down_revision = "0032_memory_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "runtime_commands",
        sa.Column("lane", sa.String(20), nullable=False, server_default="work"),
    )
    op.create_table(
        "runtime_releases",
        sa.Column("version", sa.String(200), primary_key=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("install_path", sa.String(4096), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("staged_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "runtime_processes",
        sa.Column("runtime_id", sa.String(200), primary_key=True),
        sa.Column("version", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("process_birth", sa.String(200), nullable=False),
        sa.Column("endpoint", sa.String(4096), nullable=False),
        sa.Column("credential_reference", sa.String(200), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "deployment_host",
        sa.Column("key", sa.String(20), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("api_runtime_id", sa.String(200)),
        sa.Column("target_version", sa.String(200)),
        sa.Column("rollback_version", sa.String(200)),
        sa.Column("detail", sa.String(4000), nullable=False),
    )
    op.create_table(
        "project_handoffs",
        sa.Column("project_id", sa.String(200), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_runtime_id", sa.String(200), nullable=False),
        sa.Column("source_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("target_runtime_id", sa.String(200), nullable=False),
        sa.Column("target_version", sa.String(200), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column(
            "admission_held", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "runtime_resources",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("runtime_id", sa.String(200), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("restoration", sa.String(30), nullable=False),
        sa.Column("detail", sa.String(4000), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_runtime_resource_owner", "runtime_resources", ["runtime_id", "state"]
    )
    op.create_table(
        "deployment_receipts",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
    )


def downgrade():
    raise RuntimeError("code rollback retains ownership, handoffs and ongoing SQL work")
