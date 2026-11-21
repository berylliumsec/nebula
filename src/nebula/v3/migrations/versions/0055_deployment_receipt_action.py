"""Index deployment receipts by action and time.

The supervisor's admission reconcile reads the latest ``api.promoted`` receipt
and the ``project.quiesced`` retirement candidates on every pass (about once a
second). With only the primary key, each lookup read the whole table, which
holds every receipt ever written (180k rows on a long-lived Core, nearly all
of them other actions).

Index-only and additive: no column or payload change, so older code keeps
reading and writing the same rows and the database maintains the index for
every writer.
"""

from alembic import op

revision = "0055_deployment_receipt_action"
down_revision = "0054_memory_job_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_deployment_receipt_action_time",
        "deployment_receipts",
        ["action", "occurred_at_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_deployment_receipt_action_time", "deployment_receipts")
