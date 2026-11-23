"""The project Work stage a chat is at, read from the Work item assigned to it.

A project defines its own Work stages (``Engagement.work_stages``); each Work
item names one by key (``WorkItem.stage``) and can be assigned to a chat
(``WorkItem.assignee_session_id``). A chat's stage is the stage of its most
recently updated open (not done) assigned item, labelled as the project
defines it. A chat with no open assigned item, an item without a stage, or a
stage key the project no longer lists has no stage. Nothing is stored: each
chat, list and board read joins it, one bounded query per page.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import EntityRow
from .domain import ChatSession, Engagement, WorkItem
from .stored_entities import decode_stored
from .storage import NebulaStore

WORK_STAGE_KEY = "work_stage"
"""Read-time session metadata: the chat's project Work stage."""


async def work_stages_for_chats(
    database: AsyncSession,
    session_ids: Iterable[str],
    engagement_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Each listed chat's Work stage, by chat ID; chats without one are absent."""

    ids = {value for value in session_ids if value}
    projects = {value for value in engagement_ids if value}
    if not ids or not projects:
        return {}
    assignee = EntityRow.payload["assignee_session_id"].as_string()
    rows = (
        await database.scalars(
            select(EntityRow.payload)
            .where(
                EntityRow.kind == WorkItem.entity_kind,
                EntityRow.engagement_id.in_(projects),
                assignee.in_(ids),
                EntityRow.payload["status"].as_string() != "done",
            )
            .order_by(EntityRow.updated_at.desc(), EntityRow.id.desc())
        )
    ).all()
    newest: dict[str, WorkItem] = {}
    for payload in rows:
        item = decode_stored(WorkItem, payload)
        if item.assignee_session_id and item.assignee_session_id not in newest:
            newest[item.assignee_session_id] = item
    staged = {sid: item for sid, item in newest.items() if item.stage}
    if not staged:
        return {}
    labels: dict[tuple[str, str], str] = {}
    for payload in (
        await database.scalars(
            select(EntityRow.payload).where(
                EntityRow.kind == Engagement.entity_kind,
                EntityRow.id.in_({item.engagement_id for item in staged.values()}),
            )
        )
    ).all():
        project = decode_stored(Engagement, payload)
        for stage in project.work_stages:
            labels[(project.id, stage.key)] = stage.label
    found: dict[str, dict[str, Any]] = {}
    for sid, item in staged.items():
        label = labels.get((item.engagement_id, str(item.stage)))
        if label is None:
            continue
        found[sid] = {
            "key": item.stage,
            "label": label,
            "item_id": item.id,
            "item_title": item.title,
            "item_status": item.status,
        }
    return found


async def with_work_stage(
    store: NebulaStore, sessions: Sequence[ChatSession]
) -> list[ChatSession]:
    """Each chat with its Work stage in its metadata, when it has one."""

    if not sessions:
        return []
    async with store.database.session() as database:
        stages = await work_stages_for_chats(
            database,
            [session.id for session in sessions],
            {session.engagement_id for session in sessions},
        )
    if not stages:
        return list(sessions)
    return [
        session.model_copy(
            update={
                "metadata": {**session.metadata, WORK_STAGE_KEY: stages[session.id]}
            }
        )
        if session.id in stages
        else session
        for session in sessions
    ]
