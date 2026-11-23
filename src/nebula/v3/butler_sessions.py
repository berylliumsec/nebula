"""Atomic project-scoped reservation for Butler's ordinary chat transcript."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, select, text

from .stored_entities import decode_stored
from .advisory_keys import per_id_key
from .chat import ChatConfigurationError
from .chat_assistant_config import chat_assistant_config
from .database import EntityRow
from .deployment_protocol import BUTLER_HOME_PROJECT, RESERVED_PROJECT_IDS
from .domain import (
    ButlerFocusRuntime,
    ChatBackend,
    ChatSession,
    Engagement,
    EngagementStatus,
    HarnessProfile,
    NebulaModel,
    ProviderProfile,
    ScopePolicy,
)
from .execution_scope import observer_reads
from .harnesses import HarnessRuntimeService
from .mcp import resolve_mcp_profiles
from .storage import NebulaStore, NotFoundError


class ButlerFocus(NebulaModel):
    """The project a Butler home session is about.

    The field is ``project``, not ``project_id``: request routing treats every
    ``*_id`` field as a project reference, and a reservation that named both
    Butler's home and the focus project would be refused as ambiguous.
    """

    project: str = Field(min_length=1, max_length=200)


class ButlerSessionReservation(NebulaModel):
    engagement_id: str = Field(min_length=1, max_length=200)
    # Only with engagement_id == BUTLER_HOME_PROJECT; omitted for a
    # Nebula-wide thread.
    focus: ButlerFocus | None = None
    backend: ChatBackend
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=500)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=64)
    harness_reasoning_effort: str | None = Field(default=None, max_length=100)
    harness_service_tier: str | None = Field(default=None, max_length=100)
    harness_cyber_access_program: (
        Literal["automatic", "standard", "daybreakBlue", "daybreakRed"] | None
    ) = None
    allow_cloud_tool_results: bool = False
    # Butler mode: start a separate Butler conversation that runs alongside
    # the project's current one instead of returning it.
    thread: bool = False
    title: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def runtime_is_coherent(self) -> "ButlerSessionReservation":
        if self.backend == ChatBackend.PROVIDER:
            if not self.provider_id or self.harness_profile_id:
                raise ValueError("provider Butler sessions require only provider_id")
            if any(
                value is not None
                for value in (
                    self.harness_reasoning_effort,
                    self.harness_service_tier,
                    self.harness_cyber_access_program,
                )
            ):
                raise ValueError(
                    "provider Butler sessions cannot select harness options"
                )
        elif not self.harness_profile_id or self.provider_id:
            raise ValueError("harness Butler sessions require only harness_profile_id")
        if self.title is not None and not self.thread:
            raise ValueError("only a Butler thread takes a title")
        if self.focus is not None and self.engagement_id != BUTLER_HOME_PROJECT:
            raise ValueError("only a Butler home session takes a focus project")
        if self.focus is not None and self.focus.project in RESERVED_PROJECT_IDS:
            raise ValueError("Butler cannot focus on a Core project")
        return self


def focus_runtime_id(project_id: str) -> str:
    return f"butler-focus-runtime:{project_id}"


async def record_focus_runtime(
    store: NebulaStore, session: ChatSession, focus: str
) -> None:
    """Share a home session's runtime with its focus project's worker.

    That worker cannot read Butler's home project, so without this row it
    would find no Butler for the project and its scheduled and recovery checks
    would stop before they start.
    """

    try:
        config = await chat_assistant_config(store, session, butler_default=False)
    except NotFoundError:
        # diagnostic-expected: a session whose harness binding is gone has no
        # runtime to share; the project's status then names the missing Butler.
        return
    profile_id, model = config["profile_id"], config["model"]
    if not isinstance(profile_id, str) or not isinstance(model, str):
        return
    harness = config["backend"] == ChatBackend.HARNESS.value
    values = {
        "focus_project": focus,
        "home_session_id": session.id,
        "backend": ChatBackend(config["backend"]),
        "profile_id": profile_id,
        "model": model,
        "harness_reasoning_effort": config["harness_reasoning_effort"]
        if harness
        else None,
        "harness_service_tier": config["harness_service_tier"] if harness else None,
        "harness_cyber_access_program": config["harness_cyber_access_program"]
        if harness
        else None,
    }
    entity_id = focus_runtime_id(focus)
    async with store.transaction(immediate=True) as transaction:
        (await transaction.lock_shared_rows())
        row = await transaction.session.get(EntityRow, entity_id)
        current = decode_stored(ButlerFocusRuntime, row.payload) if row else None
        if current is None:
            (
                await transaction.add(
                    ButlerFocusRuntime.model_validate({"id": entity_id, **values})
                )
            )
        elif any(getattr(current, key) != value for key, value in values.items()):
            (
                await transaction.update(
                    ButlerFocusRuntime,
                    entity_id,
                    values,
                    expected_revision=current.revision,
                )
            )


async def reserve_butler_session(
    store: NebulaStore,
    harness_runtime: HarnessRuntimeService,
    request: ButlerSessionReservation,
) -> ChatSession:
    """Return the active project Butler session, or commit exactly one new one.

    A home session focused on a project also shares its runtime with that
    project's worker (``record_focus_runtime``).
    """

    session = await _reserve_butler_session(store, harness_runtime, request)
    if request.focus is not None:
        (await record_focus_runtime(store, session, request.focus.project))
    return session


async def _reserve_butler_session(
    store: NebulaStore,
    harness_runtime: HarnessRuntimeService,
    request: ButlerSessionReservation,
) -> ChatSession:
    """Return the active project Butler session, or commit exactly one new one.

    The project lock covers both the empty transcript and its harness binding.
    A second Core process waits, then reads the committed session. A crash
    before commit leaves neither record; a crash afterward leaves both.
    """

    project = await store.get(Engagement, request.engagement_id)
    focus = request.focus.project if request.focus is not None else None
    if focus is not None:
        # The focus project belongs to another worker: read, never lock.
        with observer_reads():
            focused = await store.get(Engagement, focus)
        if focused.status == EngagementStatus.ARCHIVED:
            raise ChatConfigurationError("Restore the project before asking Butler")
    scope = (
        (await store.get(ScopePolicy, project.scope_policy_id))
        if project.scope_policy_id
        else None
    )
    (
        await resolve_mcp_profiles(
            store,
            request.mcp_server_ids,
            engagement_id=request.engagement_id,
            bypass_permissions=bool(scope and scope.bypass_permissions),
        )
    )
    async with store.transaction() as transaction:
        dialect = transaction.session.get_bind().dialect.name
        if dialect == "sqlite":
            # An execution worker's SQL scope already begins every SQLite
            # transaction with BEGIN IMMEDIATE and refuses textual statements.
            if store.database.execution_runtime_id is None:
                (await transaction.session.execute(text("BEGIN IMMEDIATE")))
        elif dialect == "postgresql":
            (
                await transaction.session.execute(
                    text(f"SELECT pg_advisory_xact_lock({per_id_key('project_id')})"),
                    {
                        "project_id": f"butler-active-session:{request.engagement_id}"
                        + (f":{focus}" if focus is not None else "")
                    },
                )
            )
        focus_column = EntityRow.payload["metadata"]["butler_focus_project"].as_string()
        row = await transaction.session.scalar(
            select(EntityRow)
            .where(
                EntityRow.kind == ChatSession.entity_kind,
                EntityRow.engagement_id == request.engagement_id,
                # Butler's home holds one current session per focus project.
                focus_column.is_(None) if focus is None else focus_column == focus,
                EntityRow.payload["metadata"]["control_assistant"]
                .as_boolean()
                .is_(True),
                EntityRow.payload["metadata"]["archived_at"].as_string().is_(None),
                EntityRow.payload["metadata"]["butler_recovery_case_id"]
                .as_string()
                .is_(None),
                # Butler's own subagents run in Butler mode in child chats.
                EntityRow.payload["metadata"]["subagent_id"].as_string().is_(None),
                # Butler mode threads run beside the current conversation.
                func.coalesce(
                    EntityRow.payload["metadata"]["butler_thread"].as_boolean(),
                    False,
                ).is_(False),
            )
            .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
            .limit(1)
        )
        if row is not None and not request.thread:
            return decode_stored(ChatSession, row.payload)
        title = (request.title or "Butler thread") if request.thread else "Butler"
        # A thread keeps the operator's question as its name.
        thread_metadata = (
            {"butler_thread": True, "initial_title_state": "operator"}
            if request.thread
            else {}
        )
        if focus is not None:
            thread_metadata["butler_focus_project"] = focus

        if request.backend == ChatBackend.PROVIDER:
            assert request.provider_id is not None
            provider_profile = await store.get(ProviderProfile, request.provider_id)
            if not provider_profile.enabled:
                raise ChatConfigurationError(
                    f"provider {request.provider_id!r} is disabled"
                )
            model = (
                request.model
                or provider_profile.metadata.get("default_model")
                or next(iter(provider_profile.model_allowlist), None)
            )
            if not isinstance(model, str) or not model:
                raise ChatConfigurationError(
                    "Butler requires an explicit model or provider default model"
                )
            if (
                provider_profile.model_allowlist
                and model not in provider_profile.model_allowlist
            ):
                raise ChatConfigurationError(
                    f"model {model!r} is not allowed by provider {provider_profile.id!r}"
                )
            chat = ChatSession(
                engagement_id=request.engagement_id,
                title=title,
                backend=ChatBackend.PROVIDER,
                provider_profile_id=provider_profile.id,
                model=model,
                metadata={
                    "control_assistant": True,
                    "mcp_server_ids": list(request.mcp_server_ids),
                    **thread_metadata,
                },
            )
        else:
            assert request.harness_profile_id is not None
            harness_profile = await store.get(
                HarnessProfile, request.harness_profile_id
            )
            (
                await harness_runtime._validate_harness_privacy(
                    request.engagement_id,
                    harness_profile,
                    request.mcp_server_ids,
                    allow_remote_mcp=request.allow_cloud_tool_results,
                )
            )
            home = request.engagement_id == BUTLER_HOME_PROJECT
            if focus is not None and not harness_profile.privacy.local_only:
                # The session's scope is Butler's project; the focus project's
                # local-only boundary is checked here and on every turn.
                with observer_reads():
                    scope = (
                        await store.get(ScopePolicy, focused.scope_policy_id)
                        if focused.scope_policy_id
                        else None
                    )
                if scope is not None and scope.local_only:
                    raise ChatConfigurationError(
                        f"{focused.name} keeps its data on local models; "
                        "choose a local harness for Butler."
                    )
            session = await harness_runtime.create_session(
                engagement_id=request.engagement_id,
                profile_id=harness_profile.id,
                model=request.model,
                mcp_server_ids=request.mcp_server_ids,
                reasoning_effort=request.harness_reasoning_effort,
                service_tier=request.harness_service_tier,
                cyber_access_program=(
                    None
                    if request.harness_cyber_access_program == "automatic"
                    else request.harness_cyber_access_program
                ),
                control_assistant=True,
                # Butler's own project gives the vendor CLI no shell, files or
                # command runtime: Butler reaches projects only through Core's
                # gateway, where reads and changes are checked.
                tools_enabled=not home,
                transaction=transaction,
            )
            chat = ChatSession(
                engagement_id=request.engagement_id,
                title=title,
                backend=ChatBackend.HARNESS,
                harness_profile_id=harness_profile.id,
                harness_session_id=session.id,
                model=session.model,
                metadata={
                    "control_assistant": True,
                    "mcp_server_ids": list(session.mcp_server_ids),
                    "context_management": "runtime_managed",
                    "initial_title_state": "operator",
                    "harness_runtime_options": session.metadata.get(
                        "runtime_options", {}
                    ),
                    **thread_metadata,
                },
            )
        (await transaction.add(chat))
        return chat
