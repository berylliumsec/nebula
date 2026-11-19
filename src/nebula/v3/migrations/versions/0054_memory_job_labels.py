"""Add operator-facing labels to memory jobs (docs/design/host-command-queue.md).

Additive: one nullable column. Older Core maps only the columns it declares,
so it neither reads nor writes the labels.
"""

from alembic import op
import sqlalchemy as sa

revision = "0054_memory_job_labels"
down_revision = "0053_runtime_command_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_jobs", sa.Column("labels", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_jobs", "labels")
