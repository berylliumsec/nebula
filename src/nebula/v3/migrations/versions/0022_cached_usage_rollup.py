"""Restore cache and billed-cost details in historical goal and child rollups.

Older rollup writers copied only the three token totals. The provider turns and
child charge rows retained the full usage, so repair a rollup only when those
source totals exactly reconcile with its saved totals. An in-flight child turn
may not have settled into its subagent yet; exclude that current turn only when
the remaining rounds reconcile. Leave any other mismatch untouched.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from alembic import op
import sqlalchemy as sa

revision = "0022_cached_usage_rollup"
down_revision = "0021_chat_session_recent_index"
branch_labels = None
depends_on = None


class _UsageRollup:
    def __init__(self, sources: Iterable[dict[str, Any]]) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.cached_input_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cost = Decimal(0)
        self.cost_known = True
        self.has_cost = False
        for source in sources:
            self.input_tokens += int(source.get("input_tokens") or 0)
            self.output_tokens += int(source.get("output_tokens") or 0)
            self.total_tokens += int(source.get("total_tokens") or 0)
            self.cached_input_tokens += int(source.get("cached_input_tokens") or 0)
            self.cache_creation_input_tokens += int(
                source.get("cache_creation_input_tokens") or 0
            )
            billed = source.get("cost_usd")
            if billed is not None:
                self.cost += Decimal(str(billed))
                self.has_cost = True
            elif any(
                source.get(field)
                for field in ("input_tokens", "output_tokens", "total_tokens")
            ):
                self.cost_known = False

    def matches(self, recorded: dict[str, Any]) -> bool:
        return all(
            int(recorded.get(field) or 0) == getattr(self, field)
            for field in ("input_tokens", "output_tokens", "total_tokens")
        )

    @property
    def cost_usd(self) -> float | None:
        if not self.cost_known or not self.has_cost:
            return None
        return round(float(self.cost), 12)


def _write_reconciled(
    bind: sa.Connection,
    entities: sa.Table,
    row: sa.RowMapping,
    source: _UsageRollup,
    now: datetime,
) -> None:
    payload = dict(row["payload"])
    recorded = dict(payload.get("usage") or {})
    if not source.matches(recorded):
        return
    details = {
        "cached_input_tokens": source.cached_input_tokens,
        "cache_creation_input_tokens": source.cache_creation_input_tokens,
        "cost_usd": source.cost_usd,
    }
    if all(recorded.get(key) == value for key, value in details.items()):
        return
    recorded.update(details)
    payload["usage"] = recorded
    payload["revision"] = row["revision"] + 1
    payload["updated_at"] = now.isoformat()
    result = bind.execute(
        entities.update()
        .where(entities.c.id == row["id"], entities.c.revision == row["revision"])
        .values(
            revision=row["revision"] + 1,
            updated_at=now,
            payload=payload,
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("usage rollup changed during migration")


def upgrade() -> None:
    bind = op.get_bind()
    entities = sa.Table("entities", sa.MetaData(), autoload_with=bind)
    by_session: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    by_goal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    snapshots_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    select_rows = sa.select(entities.c.id, entities.c.revision, entities.c.payload)

    for row in bind.execute(
        select_rows.where(entities.c.kind == "chat_turns")
    ).mappings():
        payload = row["payload"]
        usage = payload.get("usage") or {}
        if session_id := payload.get("session_id"):
            by_session[session_id].append((row["id"], usage))
        if goal_id := payload.get("goal_id"):
            by_goal[goal_id].append(usage)
    for row in bind.execute(
        select_rows.where(entities.c.kind == "chat_goal_usage_charges")
    ).mappings():
        payload = row["payload"]
        if goal_id := payload.get("goal_id"):
            by_goal[goal_id].append(payload.get("usage") or {})
    for row in bind.execute(
        select_rows.where(entities.c.kind == "context_snapshots")
    ).mappings():
        payload = row["payload"]
        if payload.get("owner_type") == "chat_session" and (
            session_id := payload.get("owner_id")
        ):
            snapshots_by_session[session_id].append(payload.get("usage") or {})

    now = datetime.now(timezone.utc)
    for row in bind.execute(
        select_rows.where(entities.c.kind == "chat_subagents")
    ).mappings():
        payload = row["payload"]
        sources = by_session.get(payload.get("child_session_id"), [])
        recorded = payload.get("usage") or {}
        source = _UsageRollup(usage for _, usage in sources)
        if not source.matches(recorded):
            current_turn_id = payload.get("child_turn_id")
            source = _UsageRollup(
                usage for turn_id, usage in sources if turn_id != current_turn_id
            )
        _write_reconciled(bind, entities, row, source, now)

    for row in bind.execute(
        select_rows.where(entities.c.kind == "chat_goals")
    ).mappings():
        payload = row["payload"]
        sources = by_goal.get(row["id"], [])
        recorded = payload.get("usage") or {}
        source = _UsageRollup(sources)
        if not source.matches(recorded):
            source = _UsageRollup(
                [*sources, *snapshots_by_session.get(payload.get("session_id"), [])]
            )
        _write_reconciled(bind, entities, row, source, now)


def downgrade() -> None:
    # Restored accounting is durable usage, not schema state to discard.
    pass
