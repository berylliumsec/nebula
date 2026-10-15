"""Atomic project-scoped reservation for Butler's ordinary chat transcript."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select, text

from .chat import ChatConfigurationError
from .database import EntityRow
from .domain import (
    ChatBackend,
    ChatSession,
    Engagement,
    HarnessProfile,
    NebulaModel,
    ProviderProfile,
    ScopePolicy,
)
from .harnesses import HarnessRuntimeService
from .mcp import resolve_mcp_profiles
from .storage import NebulaStore


class ButlerSessionReservation(NebulaModel):
    engagement_id: str = Field(min_length=1, max_length=200)
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
                raise ValueError("provider Butler sessions cannot select harness options")
        elif not self.harness_profile_id or self.provider_id:
            raise ValueError("harness Butler sessions require only harness_profile_id")
        return self


def reserve_butler_session(
    store: NebulaStore,
    harness_runtime: HarnessRuntimeService,
    request: ButlerSessionReservation,
) -> ChatSession:
    """Return the active project Butler session, or commit exactly one new one.

    The project lock covers both the empty transcript and its harness binding.
    A second Core process waits, then reads the committed session. A crash
    before commit leaves neither record; a crash afterward leaves both.
    """

    project = store.get(Engagement, request.engagement_id)
    scope = (
        store.get(ScopePolicy, project.scope_policy_id)
        if project.scope_policy_id
        else None
    )
    resolve_mcp_profiles(
        store,
        request.mcp_server_ids,
        engagement_id=request.engagement_id,
        bypass_permissions=bool(scope and scope.bypass_permissions),
    )
    with store.transaction() as transaction:
        dialect = transaction.session.bind.dialect.name
        if dialect == "sqlite":
            transaction.session.execute(text("BEGIN IMMEDIATE"))
        elif dialect == "postgresql":
            transaction.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:project_id))"),
                {"project_id": f"butler-active-session:{request.engagement_id}"},
            )
        row = transaction.session.scalar(
            select(EntityRow)
            .where(
                EntityRow.kind == ChatSession.entity_kind,
                EntityRow.engagement_id == request.engagement_id,
                EntityRow.payload["metadata"]["control_assistant"].as_boolean()
                .is_(True),
                EntityRow.payload["metadata"]["archived_at"].as_string().is_(None),
            )
            .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
            .limit(1)
        )
        if row is not None:
            return ChatSession.model_validate(row.payload)

        if request.backend == ChatBackend.PROVIDER:
            assert request.provider_id is not None
            profile = store.get(ProviderProfile, request.provider_id)
            if not profile.enabled:
                raise ChatConfigurationError(
                    f"provider {request.provider_id!r} is disabled"
                )
            model = (
                request.model
                or profile.metadata.get("default_model")
                or next(iter(profile.model_allowlist), None)
            )
            if not isinstance(model, str) or not model:
                raise ChatConfigurationError(
                    "Butler requires an explicit model or provider default model"
                )
            if profile.model_allowlist and model not in profile.model_allowlist:
                raise ChatConfigurationError(
                    f"model {model!r} is not allowed by provider {profile.id!r}"
                )
            chat = ChatSession(
                engagement_id=request.engagement_id,
                title="Butler",
                backend=ChatBackend.PROVIDER,
                provider_profile_id=profile.id,
                model=model,
                metadata={
                    "control_assistant": True,
                    "mcp_server_ids": list(request.mcp_server_ids),
                },
            )
        else:
            assert request.harness_profile_id is not None
            profile = store.get(HarnessProfile, request.harness_profile_id)
            harness_runtime._validate_harness_privacy(
                request.engagement_id,
                profile,
                request.mcp_server_ids,
                allow_remote_mcp=request.allow_cloud_tool_results,
            )
            session = harness_runtime.create_session(
                engagement_id=request.engagement_id,
                profile_id=profile.id,
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
                transaction=transaction,
            )
            chat = ChatSession(
                engagement_id=request.engagement_id,
                title="Butler",
                backend=ChatBackend.HARNESS,
                harness_profile_id=profile.id,
                harness_session_id=session.id,
                model=session.model,
                metadata={
                    "control_assistant": True,
                    "mcp_server_ids": list(session.mcp_server_ids),
                    "context_management": "runtime_managed",
                    "initial_title_state": "operator",
                    "harness_runtime_options": session.metadata.get("runtime_options", {}),
                },
            )
        transaction.add(chat)
        return chat
