"""Add the privacy-filtered universal-search projection.

Revision ID: 0012_search_documents
Revises: 0011_resource_relations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from nebula.v3.search import _safe_url, _text, project_search_document

revision = "0012_search_documents"
down_revision = "0011_resource_relations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_documents",
        sa.Column("id", sa.String(length=4096), primary_key=True),
        sa.Column("project_id", sa.String(length=200), nullable=True),
        sa.Column("resource_kind", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.String(length=4096), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=500), nullable=False),
        sa.Column(
            "description", sa.String(length=2000), nullable=False, server_default=""
        ),
        sa.Column(
            "breadcrumb", sa.String(length=1000), nullable=False, server_default=""
        ),
        sa.Column("content", sa.String(length=8000), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_search_documents_project_kind",
        "search_documents",
        ["project_id", "resource_kind"],
    )
    op.create_index("ix_search_documents_updated", "search_documents", ["updated_at"])
    op.create_index("ix_search_documents_label", "search_documents", ["label"])
    bind = op.get_bind()
    metadata = sa.MetaData()
    entities = sa.Table("entities", metadata, autoload_with=bind)
    documents = sa.Table("search_documents", metadata, autoload_with=bind)
    values = []
    for row in bind.execute(sa.select(entities)).mappings():
        projection = project_search_document(str(row["kind"]), row["payload"] or {})
        if projection is None:
            continue
        values.append(
            {
                "id": row["id"],
                "project_id": row["id"]
                if row["kind"] == "engagements"
                else row["engagement_id"],
                "resource_kind": projection.resource_kind.value,
                "resource_id": row["id"],
                "revision": row["revision"],
                "label": projection.label or row["id"],
                "description": projection.description,
                "breadcrumb": projection.breadcrumb,
                "content": projection.content,
                "updated_at": row["updated_at"],
            }
        )
        if row["kind"] == "browser_sessions":
            tabs = (row["payload"] or {}).get("tabs", [])
            for tab in tabs if isinstance(tabs, list) else []:
                if not isinstance(tab, dict) or not tab.get("id"):
                    continue
                tab_id = str(tab["id"])
                safe_url = _safe_url(tab.get("url"))
                values.append(
                    {
                        "id": f"{row['id']}::tab::{tab_id}",
                        "project_id": row["engagement_id"],
                        "resource_kind": "browser_tab",
                        "resource_id": f"{row['id']}/{tab_id}",
                        "revision": row["revision"],
                        "label": _text(tab.get("title"), 500) or "Browser tab",
                        "description": safe_url,
                        "breadcrumb": "Browser tabs",
                        "content": safe_url,
                        "updated_at": row["updated_at"],
                    }
                )
    if values:
        bind.execute(documents.insert(), values)


def downgrade() -> None:
    op.drop_table("search_documents")
