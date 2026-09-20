"""Recurring provider-chat occurrences using Core's existing scheduler contract."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from pydantic import BaseModel, Field

from .domain import (
    ChatSchedule,
    ChatSession,
    ProviderProfile,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError


class ScheduleCreate(BaseModel):
    interval_seconds: int = Field(ge=3_600, le=30 * 24 * 3_600)


class ScheduleWrite(BaseModel):
    expected_revision: int = Field(ge=1)
    enabled: bool | None = None


class ChatScheduleService:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    def get(self, session_id: str) -> ChatSchedule:
        self.store.get(ChatSession, session_id)
        matches = self.store.list_session_entities(ChatSchedule, session_id)
        if not matches:
            raise NotFoundError(f"chat schedule not found for session: {session_id}")
        return matches[0]

    def create(self, session_id: str, body: ScheduleCreate) -> ChatSchedule:
        session = self.store.get(ChatSession, session_id)
        if session.backend.value != "provider" or not session.provider_profile_id:
            raise ConflictError("schedules require a provider conversation")
        try:
            self.get(session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: absence is the precondition for creating a schedule
            pass
        else:
            raise ConflictError("conversation already has a schedule")
        return self.store.create(
            ChatSchedule(
                id=str(uuid4()),
                engagement_id=session.engagement_id,
                session_id=session.id,
                provider_profile_id=session.provider_profile_id,
                model=session.model,
                interval_seconds=body.interval_seconds,
                next_run_at=utc_now() + timedelta(seconds=body.interval_seconds),
            )
        )

    def write(self, session_id: str, body: ScheduleWrite) -> ChatSchedule:
        schedule = self.get(session_id)
        if schedule.revision != body.expected_revision:
            raise ConflictError(
                "schedule changed on another device; reload before retrying"
            )
        changes: dict = {}
        if body.enabled is not None:
            changes["enabled"] = body.enabled
            if body.enabled:
                changes["skip_reason"] = None
                changes["next_run_at"] = utc_now() + timedelta(
                    seconds=schedule.interval_seconds
                )
        return self.store.update(
            ChatSchedule, schedule.id, changes, expected_revision=schedule.revision
        )

    def due(self) -> list[ChatSchedule]:
        now = utc_now()
        items: list[ChatSchedule] = []
        offset = 0
        while page := self.store.list_entities(
            ChatSchedule, offset=offset, limit=1_000
        ):
            items.extend(
                item for item in page if item.enabled and item.next_run_at <= now
            )
            offset += len(page)
        return items

    def skip(self, schedule: ChatSchedule, reason: str) -> ChatSchedule:
        return self.store.update(
            ChatSchedule,
            schedule.id,
            {
                "skip_reason": reason,
                "last_status": "skipped",
                "last_run_at": utc_now(),
                "next_run_at": utc_now() + timedelta(seconds=schedule.interval_seconds),
            },
            expected_revision=schedule.revision,
        )

    def record_run(
        self, schedule: ChatSchedule, *, turn_id: str, status: str
    ) -> ChatSchedule:
        return self.store.update(
            ChatSchedule,
            schedule.id,
            {
                "last_turn_id": turn_id,
                "last_status": status,
                "last_run_at": utc_now(),
                "skip_reason": None,
                "next_run_at": utc_now() + timedelta(seconds=schedule.interval_seconds),
            },
            expected_revision=schedule.revision,
        )

    def reconcile(self, schedule: ChatSchedule) -> ChatSchedule | None:
        """Retire a schedule whose conversation is gone; pause one whose provider is.

        Older releases left schedule rows behind when their conversation was
        deleted, and a provider profile can be removed after a schedule was
        created. Neither may raise out of the scheduler tick, which would stop
        every other schedule from firing.
        """

        try:
            self.store.get(ChatSession, schedule.session_id)
        except NotFoundError:  # diagnostic-expected: the conversation was deleted; its schedule has nothing to run
            self.store.delete(ChatSchedule, schedule.id)
            return None
        try:
            self.store.get(ProviderProfile, schedule.provider_profile_id)
        except NotFoundError:  # diagnostic-expected: the provider was removed; keep the schedule for the operator to repoint
            return self.store.update(
                ChatSchedule,
                schedule.id,
                {
                    "enabled": False,
                    "last_status": "skipped",
                    "skip_reason": "Provider was removed; choose a provider for this conversation and enable the schedule again.",
                },
                expected_revision=schedule.revision,
            )
        return schedule

    def revalidate(self, schedule: ChatSchedule) -> str | None:
        profile = self.store.get(ProviderProfile, schedule.provider_profile_id)
        if not profile.enabled:
            return "Provider is disabled; the schedule is paused until it is enabled."
        session = self.store.get(ChatSession, schedule.session_id)
        if session.model != schedule.model or session.provider_profile_id != profile.id:
            return "Saved provider or model changed; update the schedule before it can run."
        return None
