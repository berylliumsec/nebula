"""A resume receipt identifies its new segment, retaining old reader support."""

from alembic import op
import sqlalchemy as sa

revision = "0040_runtime_chat_cursor"
down_revision = "0039_runtime_chat_binding"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "runtime_chat_bindings",
        sa.Column("stream_after", sa.BigInteger(), nullable=True),
    )


def downgrade():
    raise RuntimeError(
        "Retain resume cursors; roll back compatible code without SQL restore"
    )
