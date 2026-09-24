"""Seed a kind past the 1,000-row page so lookups have to reach the newest rows.

``NebulaStore.list_entities`` returns at most 1,000 rows, oldest first. A lookup
that read one such page and filtered it in Python stopped seeing new records
once a kind held that many. These helpers insert the filler in one statement,
dated before the records a test creates, so the test proves the lookup finds a
record the oldest page no longer contains.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import insert

from nebula.v3.database import EntityRow
from nebula.v3.domain import Entity, entity_engagement_id
from nebula.v3.storage import NebulaStore, _entity_lookup_fields

PAGE = 1_000


def seed_older_copies(
    store: NebulaStore,
    entity: Entity,
    count: int = PAGE,
    vary: Callable[[int], dict[str, Any]] | None = None,
) -> None:
    """Bulk-insert ``count`` copies of ``entity``, all dated before it.

    ``entity`` itself need not be stored; it is the template. ``vary(index)``
    returns field changes for one copy, e.g. a distinct transaction id so the
    copies do not satisfy the lookup under test.
    """

    start = entity.created_at - timedelta(days=1)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        created = start + timedelta(milliseconds=index)
        copy = entity.model_copy(
            update={
                **(vary(index) if vary is not None else {}),
                "id": f"{entity.id}-older-{index}",
                "created_at": created,
                "updated_at": created,
            }
        )
        rows.append(
            {
                "id": copy.id,
                "kind": copy.entity_kind,
                "engagement_id": entity_engagement_id(copy),
                "revision": copy.revision,
                "payload": copy.model_dump(mode="json"),
                **_entity_lookup_fields(copy),
                "created_at": created,
                "updated_at": created,
            }
        )
    with store.database.session() as session:
        session.execute(insert(EntityRow), rows)
