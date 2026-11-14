"""Stable SQL shapes for the filtered lookup indexes on entities."""

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


# Storage rows that still need work: a dispatch the loop has not sent, an open
# incident, an action without a final outcome. Settled rows are kept as
# history, so these reads must not page through it (see the partial indexes
# in 0049_storage_open_rows). Each entry is (field, values, values-are-open).
STORAGE_OPEN_ROWS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "storage_dispatches": ("status", ("pending",), True),
    "storage_incidents": ("state", ("resolved",), False),
    "storage_actions": ("status", ("expired", "complete", "failed"), False),
}


def storage_payload_text(row, key: str, dialect: str):
    """A top-level payload string in the exact form the storage indexes use."""
    if dialect == "sqlite":
        return func.json_extract(row.payload, literal("$." + key, literal_execute=True))
    return func.json_extract_path_text(row.payload, literal(key, literal_execute=True))


def open_storage_row(row, kind: str, dialect: str):
    # Literal values, as above, so both planners can match the partial index.
    field, values, open_values = STORAGE_OPEN_ROWS[kind]
    value = storage_payload_text(row, field, dialect)
    literals = [literal(item, literal_execute=True) for item in values]
    if len(literals) == 1:
        test = value == literals[0] if open_values else value != literals[0]
    else:
        test = value.in_(literals) if open_values else value.not_in(literals)
    return and_(row.kind == literal(kind, literal_execute=True), test)
