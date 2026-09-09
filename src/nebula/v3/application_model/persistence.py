"""Project graph and immutable edit receipts, outside generic entity CRUD."""

from sqlalchemy import Column, ForeignKey, Integer, JSON, String, Table
from ..database import Base

graphs = Table(
    "application_graphs",
    Base.metadata,
    Column(
        "project_id",
        String(200),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("revision", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)
edits = Table(
    "application_graph_edits",
    Base.metadata,
    Column(
        "project_id",
        String(200),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("revision", Integer, primary_key=True),
    Column("idempotency_key", String(200), nullable=False),
    Column("digest", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)
