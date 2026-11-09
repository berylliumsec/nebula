"""Immutable input for the one reviewed conditional search-projection write."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SearchProjectionWrite:
    source_id: str
    source_revision: int
    document_id: str
    fields: tuple[tuple[str, str | int | datetime | None], ...]
