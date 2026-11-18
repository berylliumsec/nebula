"""Record whether a re-runnable HTTP command saved anything before its worker died.

Additive: one new table. Older code never reads or writes it; a command whose
owner did not record a row keeps its previous settlement.
"""

from alembic import op
import sqlalchemy as sa

revision = "0053_runtime_command_writes"
down_revision = "0052_linked_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_command_writes",
        sa.Column("command_id", sa.String(200), primary_key=True),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("runtime_id", sa.String(200), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("wrote", sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Keep command write records; use compatible code rollback")
