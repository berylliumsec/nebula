"""Linked memory: settings, status and storage (docs/design/linked-memory.md).

Linked memory curates memory cards and typed links from completed goals. This
release implements stage 1, shadow curation: cards and links are built and
stored, and nothing is injected into any turn.

Every table is owned by one project through ``engagement_id``. Only the
project's execution worker, which holds the lease, writes it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import Field
from sqlalchemy import func, select, update

from .database import (
    LinkedMemoryCardRow,
    LinkedMemoryCardSourceRow,
    LinkedMemoryCursorRow,
    LinkedMemoryGoalRunRow,
    LinkedMemoryLinkRow,
    LinkedMemorySettingsRow,
    OperationEventRow,
)
from .domain import Engagement, NebulaModel, utc_now
from .linked_memory_rules import (
    AVAILABLE_STAGES,
    CURATOR_VERSION,
    OPEN_RUN_STATES,
    card_content_hash,
    kill_switch_engaged,
    project_stream_id,
)
from .storage import ConflictError, NebulaStore

# Stages from the design. Only ``off`` and ``shadow`` exist in this release.
LinkedMemoryStage = Literal["off", "shadow"]


def _now() -> str:
    return utc_now().isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class LinkedMemorySettings(NebulaModel):
    engagement_id: str
    stage: LinkedMemoryStage = "off"
    effective_stage: LinkedMemoryStage = "off"
    kill_switch: bool = False
    revision: int = 0
    updated_by: str | None = None
    updated_at: str | None = None
    available_stages: list[str] = Field(default_factory=lambda: list(AVAILABLE_STAGES))


class LinkedMemoryWrite(NebulaModel):
    """A revision-checked stage change. Revision 0 means none was saved yet."""

    stage: LinkedMemoryStage
    expected_revision: int = Field(ge=0)


@dataclass(frozen=True)
class GoalRun:
    goal_id: str
    engagement_id: str
    session_ids: list[str]
    state: str
    position: dict[str, Any]
    chunks: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    reason: str | None
    last_error: str | None
    revision: int
    updated_at: str

    @classmethod
    def from_row(cls, row: LinkedMemoryGoalRunRow) -> "GoalRun":
        return cls(
            goal_id=row.goal_id,
            engagement_id=row.engagement_id,
            session_ids=list(row.session_ids or []),
            state=row.state,
            position=dict(row.position or {}),
            chunks=row.chunks,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=row.cost_usd,
            reason=row.reason,
            last_error=row.last_error,
            revision=row.revision,
            updated_at=row.updated_at,
        )

    def public(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "state": self.state,
            "sessions": len(self.session_ids),
            "chunks": self.chunks,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "reason": self.reason,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class StoredCard:
    id: str
    kind: str
    title: str
    summary: str
    applies_when: str | None
    authority: str
    state: str
    goal_id: str | None
    content_hash: str

    @classmethod
    def from_row(cls, row: LinkedMemoryCardRow) -> "StoredCard":
        return cls(
            id=row.id,
            kind=row.kind,
            title=row.title,
            summary=row.summary,
            applies_when=row.applies_when,
            authority=row.authority,
            state=row.state,
            goal_id=row.goal_id,
            content_hash=row.content_hash,
        )

    def candidate(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "applies_when": self.applies_when,
            "authority": self.authority,
            "content_hash": self.content_hash,
        }


class LinkedMemoryStore:
    """Durable linked-memory state. Workers write; the API reads."""

    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.database = store.database

    # Settings -----------------------------------------------------------

    async def settings(self, project_id: str) -> LinkedMemorySettings:
        await self.store.get(Engagement, project_id)
        async with self.database.session() as session:
            row = await session.get(LinkedMemorySettingsRow, project_id)
        killed = kill_switch_engaged()
        stage = row.stage if row is not None else "off"
        if stage not in AVAILABLE_STAGES:
            stage = "off"
        return LinkedMemorySettings(
            engagement_id=project_id,
            stage=cast(LinkedMemoryStage, stage),
            effective_stage=cast(LinkedMemoryStage, "off" if killed else stage),
            kill_switch=killed,
            revision=row.revision if row is not None else 0,
            updated_by=row.updated_by if row is not None else None,
            updated_at=row.updated_at if row is not None else None,
        )

    async def effective_stage(self, project_id: str) -> str:
        if kill_switch_engaged():
            return "off"
        async with self.database.session() as session:
            row = await session.get(LinkedMemorySettingsRow, project_id)
        if row is None or row.stage not in AVAILABLE_STAGES:
            return "off"
        return row.stage

    async def write_settings(
        self, project_id: str, write: LinkedMemoryWrite, *, actor: str
    ) -> LinkedMemorySettings:
        await self.store.get(Engagement, project_id)
        now = _now()
        async with self.database.session() as session:
            row = await session.get(
                LinkedMemorySettingsRow, project_id, with_for_update=True
            )
            actual = row.revision if row is not None else 0
            if write.expected_revision != actual:
                raise ConflictError(
                    "Linked memory settings changed; reload before saving"
                )
            if row is None:
                session.add(
                    LinkedMemorySettingsRow(
                        engagement_id=project_id,
                        stage=write.stage,
                        revision=1,
                        updated_by=actor[:200],
                        updated_at=now,
                    )
                )
            else:
                await session.execute(
                    update(LinkedMemorySettingsRow)
                    .where(
                        LinkedMemorySettingsRow.engagement_id == project_id,
                        LinkedMemorySettingsRow.revision == actual,
                    )
                    .values(
                        stage=write.stage,
                        revision=actual + 1,
                        updated_by=actor[:200],
                        updated_at=now,
                    )
                )
        if write.stage != "off" and await self.cursor(project_id) is None:
            # Curate goals that complete from now on, never the backlog of
            # goals that finished before linked memory was turned on.
            await self.advance_cursor(
                project_id, await self.latest_event_sequence(project_id)
            )
        return await self.settings(project_id)

    async def enabled_projects(self) -> list[str]:
        """Projects with a stage other than off that this process can read."""

        if kill_switch_engaged():
            return []
        async with self.database.session() as session:
            rows = await session.scalars(
                select(LinkedMemorySettingsRow).where(
                    LinkedMemorySettingsRow.stage != "off"
                )
            )
            return [row.engagement_id for row in rows]

    # Event cursor --------------------------------------------------------

    async def cursor(self, project_id: str) -> int | None:
        """The last goal event considered, or None before the first enable."""

        async with self.database.session() as session:
            row = await session.get(LinkedMemoryCursorRow, project_id)
            return int(row.last_event_sequence) if row is not None else None

    async def latest_event_sequence(self, project_id: str) -> int:
        async with self.database.session() as session:
            value = await session.scalar(
                select(func.max(OperationEventRow.sequence)).where(
                    OperationEventRow.operation_id == project_stream_id(project_id)
                )
            )
        return int(value or 0)

    async def advance_cursor(self, project_id: str, sequence: int) -> None:
        async with self.database.session() as session:
            row = await session.get(
                LinkedMemoryCursorRow, project_id, with_for_update=True
            )
            if row is None:
                session.add(
                    LinkedMemoryCursorRow(
                        engagement_id=project_id, last_event_sequence=sequence
                    )
                )
            elif sequence > row.last_event_sequence:
                await session.execute(
                    update(LinkedMemoryCursorRow)
                    .where(LinkedMemoryCursorRow.engagement_id == project_id)
                    .values(last_event_sequence=sequence)
                )

    # Goal runs -------------------------------------------------------------

    async def run(self, goal_id: str) -> GoalRun | None:
        async with self.database.session() as session:
            row = await session.get(LinkedMemoryGoalRunRow, goal_id)
            return GoalRun.from_row(row) if row is not None else None

    async def create_run(
        self,
        project_id: str,
        goal_id: str,
        session_ids: list[str],
        *,
        state: str = "pending",
        reason: str | None = None,
    ) -> GoalRun:
        """Create the goal's run once; a replayed event returns the existing one."""

        now = _now()
        async with self.database.session() as session:
            existing = await session.get(LinkedMemoryGoalRunRow, goal_id)
            if existing is not None:
                return GoalRun.from_row(existing)
            row = LinkedMemoryGoalRunRow(
                goal_id=goal_id,
                engagement_id=project_id,
                session_ids=list(session_ids),
                state=state,
                position={"session": 0, "after": 0},
                chunks=0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                mcp_queries=0,
                reason=reason,
                last_error=None,
                created_at=now,
                updated_at=now,
                revision=1,
            )
            session.add(row)
            return GoalRun.from_row(row)

    async def open_runs(self, project_id: str) -> list[GoalRun]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(LinkedMemoryGoalRunRow)
                .where(
                    LinkedMemoryGoalRunRow.engagement_id == project_id,
                    LinkedMemoryGoalRunRow.state.in_(OPEN_RUN_STATES),
                )
                .order_by(LinkedMemoryGoalRunRow.created_at)
            )
            return [GoalRun.from_row(row) for row in rows]

    async def set_run_state(
        self,
        run: GoalRun,
        state: str,
        *,
        reason: str | None = None,
        last_error: str | None = None,
    ) -> GoalRun:
        values: dict[str, Any] = {
            "state": state,
            "reason": (reason or None) and reason[:500],
            "updated_at": _now(),
            "revision": run.revision + 1,
        }
        if last_error is not None:
            values["last_error"] = last_error[:1000]
        async with self.database.session() as session:
            result = await session.execute(
                update(LinkedMemoryGoalRunRow)
                .where(
                    LinkedMemoryGoalRunRow.goal_id == run.goal_id,
                    LinkedMemoryGoalRunRow.engagement_id == run.engagement_id,
                    LinkedMemoryGoalRunRow.revision == run.revision,
                )
                .values(**values)
            )
            if not result.rowcount:
                raise ConflictError("Linked memory goal run changed")
            row = await session.get(LinkedMemoryGoalRunRow, run.goal_id)
            assert row is not None
            await session.refresh(row)
            return GoalRun.from_row(row)

    # Cards -----------------------------------------------------------------

    async def candidate_cards(self, project_id: str, limit: int) -> list[StoredCard]:
        """The project's newest active cards, bounded for the helper."""

        async with self.database.session() as session:
            rows = await session.scalars(
                select(LinkedMemoryCardRow)
                .where(
                    LinkedMemoryCardRow.engagement_id == project_id,
                    LinkedMemoryCardRow.state == "active",
                )
                .order_by(LinkedMemoryCardRow.updated_at.desc())
                .limit(limit)
            )
            return [StoredCard.from_row(row) for row in rows]

    async def commit_chunk(
        self,
        run: GoalRun,
        *,
        cards: list[dict[str, Any]],
        links: list[dict[str, Any]],
        position: dict[str, Any],
        usage: dict[str, Any],
        model: str,
        final_state: str | None = None,
        reason: str | None = None,
    ) -> tuple[GoalRun, dict[str, int]]:
        """Write one chunk's cards, sources and links with the run position.

        One short transaction: a crash before it loses only this chunk, and a
        crash after it resumes from the new position.
        """

        now = _now()
        project_id = run.engagement_id
        counts = {"cards": 0, "attached": 0, "links": 0, "superseded": 0}
        async with self.database.session() as session:
            current = await session.get(
                LinkedMemoryGoalRunRow, run.goal_id, with_for_update=True
            )
            if current is None or current.revision != run.revision:
                raise ConflictError("Linked memory goal run changed")
            keys: dict[str, str] = {}
            for card in cards:
                content_hash = card_content_hash(
                    card["kind"], card["title"], card["summary"]
                )
                existing = await session.scalar(
                    select(LinkedMemoryCardRow).where(
                        LinkedMemoryCardRow.engagement_id == project_id,
                        LinkedMemoryCardRow.content_hash == content_hash,
                    )
                )
                if existing is not None:
                    card_id = existing.id
                    counts["attached"] += 1
                else:
                    card_id = _id("lmc")
                    session.add(
                        LinkedMemoryCardRow(
                            id=card_id,
                            engagement_id=project_id,
                            scope="project",
                            kind=card["kind"],
                            title=card["title"][:120],
                            summary=card["summary"][:400],
                            applies_when=(card.get("applies_when") or None)
                            and card["applies_when"][:300],
                            authority=card["authority"],
                            audience="agents",
                            state="quarantined"
                            if card.get("quarantined")
                            else "active",
                            goal_id=run.goal_id,
                            content_hash=content_hash,
                            curator_version=CURATOR_VERSION,
                            curator_model=model[:200],
                            verified_at=now,
                            created_at=now,
                            updated_at=now,
                            revision=1,
                        )
                    )
                    counts["cards"] += 1
                keys[card["key"]] = card_id
                source = card["source"]
                excerpt = source["excerpt"][:500]
                excerpt_hash = hashlib.sha256(excerpt.encode()).hexdigest()
                duplicate = await session.scalar(
                    select(LinkedMemoryCardSourceRow.id).where(
                        LinkedMemoryCardSourceRow.card_id == card_id,
                        LinkedMemoryCardSourceRow.excerpt_hash == excerpt_hash,
                    )
                )
                if duplicate is None:
                    session.add(
                        LinkedMemoryCardSourceRow(
                            id=_id("lms"),
                            card_id=card_id,
                            engagement_id=project_id,
                            kind=source["kind"],
                            ref=source["ref"],
                            excerpt=excerpt,
                            excerpt_hash=excerpt_hash,
                            captured_at=now,
                        )
                    )
            await session.flush()
            for link in links:
                from_card = keys.get(link["from"])
                to_card = link["to"]
                if from_card is None or from_card == to_card:
                    continue
                target = await session.get(LinkedMemoryCardRow, to_card)
                if target is None or target.engagement_id != project_id:
                    continue
                present = await session.scalar(
                    select(LinkedMemoryLinkRow.id).where(
                        LinkedMemoryLinkRow.from_card == from_card,
                        LinkedMemoryLinkRow.to_card == to_card,
                        LinkedMemoryLinkRow.type == link["type"],
                    )
                )
                if present is not None:
                    continue
                session.add(
                    LinkedMemoryLinkRow(
                        id=_id("lml"),
                        engagement_id=project_id,
                        from_card=from_card,
                        to_card=to_card,
                        type=link["type"],
                        state=link["state"],
                        reason=link["reason"][:300],
                        basis=link["basis"][:30],
                        confidence=int(link.get("confidence", 50)),
                        curator_version=CURATOR_VERSION,
                        curator_model=model[:200],
                        created_at=now,
                        updated_at=now,
                    )
                )
                counts["links"] += 1
                if (
                    link["type"] == "supersedes"
                    and link["state"] == "accepted"
                    and target.state == "active"
                ):
                    await session.execute(
                        update(LinkedMemoryCardRow)
                        .where(
                            LinkedMemoryCardRow.id == to_card,
                            LinkedMemoryCardRow.engagement_id == project_id,
                        )
                        .values(
                            state="superseded",
                            updated_at=now,
                            revision=target.revision + 1,
                        )
                    )
                    counts["superseded"] += 1
            values: dict[str, Any] = {
                "position": position,
                "chunks": current.chunks + 1,
                "input_tokens": current.input_tokens
                + int(usage.get("input_tokens", 0)),
                "output_tokens": current.output_tokens
                + int(usage.get("output_tokens", 0)),
                "cost_usd": current.cost_usd + float(usage.get("cost_usd") or 0.0),
                "updated_at": now,
                "revision": current.revision + 1,
                "state": final_state or "running",
                "reason": (reason or None) and reason[:500],
            }
            await session.execute(
                update(LinkedMemoryGoalRunRow)
                .where(
                    LinkedMemoryGoalRunRow.goal_id == run.goal_id,
                    LinkedMemoryGoalRunRow.engagement_id == project_id,
                    LinkedMemoryGoalRunRow.revision == run.revision,
                )
                .values(**values)
            )
            row = await session.get(LinkedMemoryGoalRunRow, run.goal_id)
            assert row is not None
            await session.refresh(row)
            return GoalRun.from_row(row), counts

    # Status ------------------------------------------------------------------

    async def status(self, project_id: str) -> dict[str, Any]:
        settings = await self.settings(project_id)
        async with self.database.session() as session:
            card_counts: dict[str, int] = {
                str(state): int(count)
                for state, count in (
                    await session.execute(
                        select(LinkedMemoryCardRow.state, func.count())
                        .where(LinkedMemoryCardRow.engagement_id == project_id)
                        .group_by(LinkedMemoryCardRow.state)
                    )
                ).all()
            }
            link_counts: dict[str, int] = {
                str(state): int(count)
                for state, count in (
                    await session.execute(
                        select(LinkedMemoryLinkRow.state, func.count())
                        .where(LinkedMemoryLinkRow.engagement_id == project_id)
                        .group_by(LinkedMemoryLinkRow.state)
                    )
                ).all()
            }
            run_rows = list(
                await session.scalars(
                    select(LinkedMemoryGoalRunRow)
                    .where(LinkedMemoryGoalRunRow.engagement_id == project_id)
                    .order_by(LinkedMemoryGoalRunRow.updated_at.desc())
                    .limit(200)
                )
            )
        runs = [GoalRun.from_row(row) for row in run_rows]
        backlog = [run for run in runs if run.state in OPEN_RUN_STATES]
        errors = [run for run in runs if run.last_error]
        return {
            "settings": settings.model_dump(mode="json"),
            "backlog": len(backlog),
            "cards": {
                "active": int(card_counts.get("active", 0)),
                "quarantined": int(card_counts.get("quarantined", 0)),
                "superseded": int(card_counts.get("superseded", 0)),
                "total": int(sum(card_counts.values())),
            },
            "links": {
                "accepted": int(link_counts.get("accepted", 0)),
                "proposed": int(link_counts.get("proposed", 0)),
                "total": int(sum(link_counts.values())),
            },
            "spend": {
                "input_tokens": sum(run.input_tokens for run in runs),
                "output_tokens": sum(run.output_tokens for run in runs),
                "cost_usd": round(sum(run.cost_usd for run in runs), 6),
            },
            "runs": [run.public() for run in runs[:20]],
            "last_error": errors[0].last_error if errors else None,
        }

    async def cards(
        self, project_id: str, *, limit: int = 50, state: str | None = None
    ) -> list[dict[str, Any]]:
        """Cards with their sources and outgoing links, newest first."""

        async with self.database.session() as session:
            statement = select(LinkedMemoryCardRow).where(
                LinkedMemoryCardRow.engagement_id == project_id
            )
            if state is not None:
                statement = statement.where(LinkedMemoryCardRow.state == state)
            rows = list(
                await session.scalars(
                    statement.order_by(LinkedMemoryCardRow.updated_at.desc()).limit(
                        limit
                    )
                )
            )
            ids = [row.id for row in rows]
            sources = (
                list(
                    await session.scalars(
                        select(LinkedMemoryCardSourceRow).where(
                            LinkedMemoryCardSourceRow.card_id.in_(ids),
                            LinkedMemoryCardSourceRow.engagement_id == project_id,
                        )
                    )
                )
                if ids
                else []
            )
            links = (
                list(
                    await session.scalars(
                        select(LinkedMemoryLinkRow).where(
                            LinkedMemoryLinkRow.from_card.in_(ids),
                            LinkedMemoryLinkRow.engagement_id == project_id,
                        )
                    )
                )
                if ids
                else []
            )
        by_card: dict[str, dict[str, Any]] = {}
        for row in rows:
            by_card[row.id] = {
                "id": row.id,
                "kind": row.kind,
                "title": row.title,
                "summary": row.summary,
                "applies_when": row.applies_when,
                "authority": row.authority,
                "state": row.state,
                "goal_id": row.goal_id,
                "curator_model": row.curator_model,
                "created_at": row.created_at,
                "sources": [],
                "links": [],
            }
        for source in sources:
            by_card[source.card_id]["sources"].append(
                {"kind": source.kind, "ref": source.ref, "excerpt": source.excerpt}
            )
        for link in links:
            by_card[link.from_card]["links"].append(
                {
                    "to": link.to_card,
                    "type": link.type,
                    "state": link.state,
                    "reason": link.reason,
                    "basis": link.basis,
                }
            )
        return [by_card[row.id] for row in rows]
