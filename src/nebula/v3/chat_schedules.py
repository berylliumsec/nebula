"""Recurring provider-chat occurrences using Core's existing scheduler contract."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, NamedTuple, cast
from uuid import uuid4

from pydantic import BaseModel, Field

from .chat_subagents import subagent_limit
from .providers import REASONING_EFFORTS, ReasoningEffort
from .domain import (
    ChatSchedule,
    ChatSession,
    ChatTurn,
    McpServerProfile,
    ProviderProfile,
    SshEnvironment,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError

ARCHIVED_SKIP_REASON = "Conversation is archived; unarchive it to resume the schedule."


class ScheduledTurnSettings(NamedTuple):
    """The settings a turn Core starts on its own sends, mirroring a manual send."""

    tools_enabled: bool
    mcp_server_ids: list[str]
    ssh_environment_ids: list[str] | None
    hook_ids: list[str]
    reasoning_effort: ReasoningEffort | None
    allow_subagents: bool
    max_active_subagents: int | None
    allow_cloud_tool_results: bool


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
            # An operator's explicit choice replaces any archive-driven pause.
            changes["paused_by"] = None
            if body.enabled:
                changes["skip_reason"] = None
                changes["next_run_at"] = utc_now() + timedelta(
                    seconds=schedule.interval_seconds
                )
        updated = self.store.update(
            ChatSchedule, schedule.id, changes, expected_revision=schedule.revision
        )
        if body.enabled:
            # Enabling a schedule is a write to the conversation: like sending
            # or queueing a message, it returns an archived chat to the list so
            # the occurrences it produces are visible.
            from .chat import unarchive_chat_session

            unarchive_chat_session(self.store, session_id)
        return updated

    def pause_for_archive(self, session_id: str) -> None:
        """Park an enabled schedule while its conversation is archived."""

        try:
            schedule = self.get(session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: most conversations have no schedule to pause
            return
        if not schedule.enabled:
            return
        self.store.update(
            ChatSchedule,
            schedule.id,
            {
                "enabled": False,
                "paused_by": "archive",
                "skip_reason": ARCHIVED_SKIP_REASON,
            },
            expected_revision=schedule.revision,
        )

    def resume_after_unarchive(self, session_id: str) -> None:
        """Resume a schedule that archiving paused; leave an operator's pause alone."""

        try:
            schedule = self.get(session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: most conversations have no schedule to resume
            return
        if schedule.enabled or schedule.paused_by != "archive":
            return
        self.store.update(
            ChatSchedule,
            schedule.id,
            {
                "enabled": True,
                "paused_by": None,
                "skip_reason": None,
                "next_run_at": utc_now() + timedelta(seconds=schedule.interval_seconds),
            },
            expected_revision=schedule.revision,
        )

    def turn_settings(self, session_id: str) -> ScheduledTurnSettings:
        """Settings for a turn Core starts: what the operator last chose.

        Goal dispatch, goal continuation and scheduled occurrences have no
        composer to read. The conversation holds the operator's current MCP
        servers, hooks, reasoning effort and subagents, saved as they change
        and by every send. A turn writes its settings back there, so copying
        an older turn would undo a later choice. The newest turn's snapshot
        supplies the tools toggle and SSH hosts, which only a send records,
        and any setting an older conversation never saved; before any turn,
        the conversation's saved tools toggle applies. Servers and hosts
        removed or disabled since are dropped rather than failing every turn.
        """

        session = self.store.get(ChatSession, session_id)
        saved = session.metadata
        turns = self.store.list_session_entities(ChatTurn, session_id)
        latest = next((item for item in reversed(turns) if item.request_snapshot), None)
        snapshot = latest.request_snapshot if latest is not None else {}
        if latest is None:
            tools_enabled = bool(saved.get("tools_enabled", False))
        else:
            tools_enabled = bool(snapshot.get("include_oci_tools", False))

        def chosen(key: str) -> Any:
            return saved[key] if key in saved else snapshot.get(key)

        mcp_server_ids: list[str] = []
        for server_id in chosen("mcp_server_ids") or []:
            if not isinstance(server_id, str):
                continue
            try:
                server = self.store.get(McpServerProfile, server_id)
            except NotFoundError:  # diagnostic-expected: the server was removed since it was chosen; the turn runs without it
                continue
            if server.enabled:
                mcp_server_ids.append(server_id)
        ssh_environment_ids: list[str] | None = None
        hosts = snapshot.get("ssh_environment_snapshot")
        if isinstance(hosts, list):
            ssh_environment_ids = []
            for host in hosts:
                host_id = host.get("id") if isinstance(host, dict) else None
                if not isinstance(host_id, str):
                    continue
                try:
                    environment = self.store.get(SshEnvironment, host_id)
                except NotFoundError:  # diagnostic-expected: the host was removed since the last turn; the occurrence runs without it
                    continue
                if environment.enabled:
                    ssh_environment_ids.append(host_id)
        effort = saved.get("reasoning_effort")
        return ScheduledTurnSettings(
            tools_enabled=tools_enabled,
            mcp_server_ids=mcp_server_ids,
            ssh_environment_ids=ssh_environment_ids,
            hook_ids=[
                item for item in saved.get("hook_ids") or [] if isinstance(item, str)
            ],
            reasoning_effort=(
                cast(ReasoningEffort, effort) if effort in REASONING_EFFORTS else None
            ),
            allow_subagents=bool(chosen("allow_subagents")),
            max_active_subagents=subagent_limit(chosen("max_active_subagents")),
            # The operator already confirmed tool-result transfer for the turn
            # this occurrence continues, as subagent goal continuation does.
            allow_cloud_tool_results=tools_enabled,
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
            session = self.store.get(ChatSession, schedule.session_id)
        except NotFoundError:  # diagnostic-expected: the conversation was deleted; its schedule has nothing to run
            self.store.delete(ChatSchedule, schedule.id)
            return None
        if (
            session.backend.value == "provider"
            and session.provider_profile_id
            and (
                session.provider_profile_id != schedule.provider_profile_id
                or session.model != schedule.model
            )
        ):
            # The schedule follows the conversation: an operator who picks
            # another model or provider in the chat expects occurrences to use
            # it, and the UI offers no other way to repoint the schedule.
            schedule = self.store.update(
                ChatSchedule,
                schedule.id,
                {
                    "provider_profile_id": session.provider_profile_id,
                    "model": session.model,
                },
                expected_revision=schedule.revision,
            )
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
        session = self.store.get(ChatSession, schedule.session_id)
        if "archived_at" in session.metadata:
            return ARCHIVED_SKIP_REASON
        if session.backend.value != "provider" or not session.provider_profile_id:
            return "Conversation no longer runs on a provider; the schedule is paused until it does."
        profile = self.store.get(ProviderProfile, schedule.provider_profile_id)
        if not profile.enabled:
            return "Provider is disabled; the schedule is paused until it is enabled."
        return None
