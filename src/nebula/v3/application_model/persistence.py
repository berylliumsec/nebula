"""Project graph and immutable edit receipts, outside generic entity CRUD."""

from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, ForeignKey, Integer, JSON, String, Table

from ..database import Base

if TYPE_CHECKING:
    from sqlalchemy.orm import Mapped

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


class ApplicationGraphRow(Base):
    """Mapped reads of ``graphs``.

    Execution workers scope mapped reads to their project and refuse Core-table
    selects over project tables, so every read goes through these classes.
    """

    __table__ = graphs

    if TYPE_CHECKING:
        # The columns come from ``graphs``; declared here only for typing.
        project_id: Mapped[str]
        revision: Mapped[int]
        payload: Mapped[Any]


class ApplicationGraphEditRow(Base):
    """Mapped reads of ``edits``; see :class:`ApplicationGraphRow`."""

    __table__ = edits

    if TYPE_CHECKING:
        # The columns come from ``edits``; declared here only for typing.
        project_id: Mapped[str]
        revision: Mapped[int]
        idempotency_key: Mapped[str]
        digest: Mapped[str]
        payload: Mapped[Any]
