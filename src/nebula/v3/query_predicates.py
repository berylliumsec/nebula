"""Stable SQL shapes for the filtered harness title lookup indexes."""

from typing import Any
from sqlalchemy import Boolean, and_, func, literal


def visible_harness_chat_title(row, dialect: str):
    # Literal paths are intentional: PostgreSQL prepared plans and SQLite's
    # partial-index matcher must be able to prove the index predicate.
    values = []
    value: Any
    for key in ("temporary_assistant", "side_chat"):
        if dialect == "sqlite":
            value = func.json_extract(
                row.payload, literal("$.metadata." + key, literal_execute=True)
            )
        else:
            value = func.json_extract_path_text(
                row.payload,
                literal("metadata", literal_execute=True),
                literal(key, literal_execute=True),
            ).cast(Boolean)
        values.append(
            func.coalesce(value, literal(False, literal_execute=True)).is_(False)
        )
    return and_(row.kind == literal("chat_sessions", literal_execute=True), *values)
