"""Saved next-turn choices shared by chat UI and Nebula work control."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from .domain import (
    ChatBackend,
    ChatSession,
    Engagement,
    HarnessProfile,
    HarnessSession,
    NebulaModel,
    ProviderProfile,
    ScopePolicy,
)
from .mcp import resolve_mcp_profiles
from .native_hooks import discover_native_hooks, snapshot_native_hook
from .providers import ReasoningEffort
from .storage import NebulaStore


class ChatAssistantConfigPatch(NebulaModel):
    """Only choices that the ordinary Assistant settings can apply next turn."""

    backend: ChatBackend | None = None
    profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=500)
    reasoning_effort: ReasoningEffort | None = None
    harness_reasoning_effort: str | None = Field(default=None, max_length=100)
    harness_service_tier: str | None = Field(default=None, max_length=100)
    harness_mode: str | None = Field(default=None, max_length=100)
    harness_cyber_access_program: (
        Literal["automatic", "standard", "daybreakBlue", "daybreakRed"] | None
    ) = None
    mcp_server_ids: list[str] | None = Field(default=None, max_length=64)
    hook_ids: list[str] | None = Field(default=None, max_length=32)
    include_knowledge: bool | None = None
    allow_cloud_knowledge: bool | None = None
    tools_enabled: bool | None = None
    planning_enabled: bool | None = None
    allow_subagents: bool | None = None
    allow_agent_messaging: bool | None = None
    max_active_subagents: int | None = Field(default=None, ge=1, le=100)
    subagent_provider_id: str | None = Field(default=None, max_length=200)
    subagent_harness_profile_id: str | None = Field(default=None, max_length=200)
    subagent_model: str | None = Field(default=None, max_length=500)
    subagent_reasoning_effort: ReasoningEffort | None = None

    @model_validator(mode="after")
    def changes_something(self) -> "ChatAssistantConfigPatch":
        if not self.model_fields_set:
            raise ValueError("choose at least one Assistant setting to change")
        for field in ("mcp_server_ids", "hook_ids"):
            values = getattr(self, field)
            if values is not None and len(values) != len(set(values)):
                raise ValueError(f"{field} contains duplicates")
        return self


def chat_assistant_config(store: NebulaStore, session: ChatSession) -> dict[str, Any]:
    """Read Core's saved next-turn choice, or derive the last effective turn."""

    metadata = session.metadata
    saved = metadata.get("assistant_config")
    options = metadata.get("harness_runtime_options")
    if session.backend == ChatBackend.HARNESS and session.harness_session_id:
        harness = store.get(HarnessSession, session.harness_session_id)
        options = harness.metadata.get("runtime_options", options)
    runtime_options = options if isinstance(options, dict) else {}
    subagent = metadata.get("provider_subagent")
    subagent_options = subagent if isinstance(subagent, dict) else {}
    current: dict[str, Any] = {
        "backend": session.backend.value,
        "profile_id": session.provider_profile_id
        if session.backend == ChatBackend.PROVIDER
        else session.harness_profile_id,
        "model": session.model,
        "reasoning_effort": metadata.get("reasoning_effort"),
        "harness_reasoning_effort": runtime_options.get("reasoning_effort"),
        "harness_service_tier": runtime_options.get("service_tier"),
        "harness_mode": metadata.get("harness_mode"),
        "harness_cyber_access_program": runtime_options.get("cyber_access_program")
        or "automatic",
        "mcp_server_ids": list(metadata.get("mcp_server_ids") or []),
        "hook_ids": list(metadata.get("hook_ids") or []),
        "include_knowledge": metadata.get("include_knowledge", True),
        "allow_cloud_knowledge": metadata.get("allow_cloud_knowledge", False),
        "tools_enabled": metadata.get("tools_enabled", False),
        "planning_enabled": bool(metadata.get("planning_enabled", False)),
        "allow_subagents": bool(
            subagent_options
            if session.backend == ChatBackend.HARNESS
            else metadata.get("allow_subagents")
        ),
        "allow_agent_messaging": metadata.get("allow_agent_messaging") is True,
        "max_active_subagents": metadata.get("max_active_subagents")
        if session.backend == ChatBackend.PROVIDER
        else subagent_options.get("max_active"),
        "subagent_provider_id": subagent_options.get("provider_profile_id"),
        "subagent_harness_profile_id": subagent_options.get("harness_profile_id"),
        "subagent_model": subagent_options.get("model"),
        "subagent_reasoning_effort": metadata.get("subagent_reasoning_effort")
        or subagent_options.get("reasoning_effort"),
    }
    if isinstance(saved, dict):
        current.update({key: value for key, value in saved.items() if key in current})
    return current


def update_chat_assistant_config(
    store: NebulaStore,
    session: ChatSession,
    patch: ChatAssistantConfigPatch,
    *,
    expected_revision: int,
    workspace_resolver: Callable[[str], Path] | None = None,
) -> ChatSession:
    """Validate every supplied choice, save atomically, and retain chat binding."""

    if (
        session.metadata.get("side_chat") is True
        or session.metadata.get("temporary_assistant") is True
    ):
        raise ValueError("Assistant settings are available on saved main chats")
    if isinstance(session.metadata.get("archived_at"), str):
        raise ValueError("unarchive this chat before changing Assistant settings")
    if expected_revision != session.revision:
        raise ValueError(
            "conversation changed; read its Assistant settings and try again"
        )
    current = chat_assistant_config(store, session)
    changes = patch.model_dump(exclude_unset=True, mode="json")
    selected = {**current, **changes}
    backend = selected["backend"]
    if backend not in {"provider", "harness"}:
        raise ValueError("choose Provider or Agent harness")
    if ("backend" in changes or "profile_id" in changes) and "model" not in changes:
        raise ValueError("choose a listed model when changing runtime")
    profile_id = selected["profile_id"]
    model = selected["model"]
    if not isinstance(profile_id, str) or not isinstance(model, str):
        raise ValueError("choose an enabled runtime and listed model")
    if selected["planning_enabled"] and selected["harness_mode"] in {
        "plan",
        "planning",
    }:
        raise ValueError(
            "Plan before acting cannot be combined with harness Plan only mode"
        )
    if backend == "provider":
        provider_profile = store.get(ProviderProfile, profile_id)
        if (
            not provider_profile.enabled
            or model not in provider_profile.model_allowlist
        ):
            raise ValueError("choose an enabled provider and one of its allowed models")
        if session.metadata.get(
            "control_assistant"
        ) is True and not provider_profile.tools_verified_for(model):
            raise ValueError("Butler needs a provider model verified for tools")
        if selected["tools_enabled"] and not provider_profile.tools_verified_for(model):
            raise ValueError("this provider model is not verified for command tools")
    else:
        harness_profile = store.get(HarnessProfile, profile_id)
        if (
            not harness_profile.enabled
            or model not in harness_profile.capabilities.models
        ):
            raise ValueError(
                "choose an enabled harness and one of its advertised models"
            )
        options = next(
            (
                item
                for item in harness_profile.capabilities.model_options
                if item.model == model
            ),
            None,
        )
        effort = selected["harness_reasoning_effort"]
        tier = selected["harness_service_tier"]
        cyber = selected["harness_cyber_access_program"]
        mode = selected["harness_mode"]
        if effort and (
            options is None
            or effort not in {item.id for item in options.reasoning_efforts}
        ):
            raise ValueError(
                "the selected harness model does not advertise that effort"
            )
        if tier and (
            options is None or tier not in {item.id for item in options.service_tiers}
        ):
            raise ValueError("the selected harness model does not advertise that speed")
        if (
            cyber
            and cyber != "automatic"
            and (options is None or cyber not in (options.cyber_access_programs or []))
        ):
            raise ValueError(
                "this Codex account and model do not advertise that cyber access"
            )
        if mode and mode not in harness_profile.capabilities.modes:
            raise ValueError("the selected harness does not advertise that mode")
    project = store.get(Engagement, session.engagement_id)
    scope = (
        store.get(ScopePolicy, project.scope_policy_id)
        if project.scope_policy_id
        else None
    )
    resolve_mcp_profiles(
        store,
        selected["mcp_server_ids"],
        engagement_id=session.engagement_id,
        bypass_permissions=bool(scope and scope.bypass_permissions),
    )
    if selected["hook_ids"]:
        if backend != "provider":
            raise ValueError("lifecycle hooks are available only for provider chats")
        if workspace_resolver is None:
            raise ValueError("the project workspace is unavailable for hook discovery")
        catalog = discover_native_hooks(workspace_resolver(session.engagement_id))
        for hook_id in selected["hook_ids"]:
            snapshot_native_hook(hook_id, catalog)
    if backend == "harness" and selected["allow_subagents"]:
        provider_id = selected["subagent_provider_id"]
        harness_id = selected["subagent_harness_profile_id"]
        child_model = selected["subagent_model"]
        if bool(provider_id) == bool(harness_id) or not child_model:
            raise ValueError("choose one listed subagent runtime and model")
        if provider_id:
            child_profile = store.get(ProviderProfile, provider_id)
            if (
                not child_profile.enabled
                or child_model not in child_profile.model_allowlist
            ):
                raise ValueError("choose an enabled subagent provider and listed model")
        else:
            child_harness = store.get(HarnessProfile, harness_id)
            if (
                not child_harness.enabled
                or child_model not in child_harness.capabilities.models
            ):
                raise ValueError("choose an enabled subagent harness and listed model")
    if selected["allow_agent_messaging"] and (
        session.metadata.get("subagent_id") or session.metadata.get("side_chat")
    ):
        raise ValueError("agent messaging is available only to main chats")
    metadata = {**session.metadata, "assistant_config": selected}
    for key in (
        "mcp_server_ids",
        "hook_ids",
        "reasoning_effort",
        "allow_agent_messaging",
        "subagent_reasoning_effort",
        "include_knowledge",
        "allow_cloud_knowledge",
        "tools_enabled",
        "planning_enabled",
        "harness_mode",
    ):
        metadata[key] = selected[key]
    if backend == "provider":
        metadata["allow_subagents"] = selected["allow_subagents"]
        metadata["max_active_subagents"] = selected["max_active_subagents"]
    else:
        if selected["allow_subagents"]:
            metadata["provider_subagent"] = {
                **(
                    {"provider_profile_id": selected["subagent_provider_id"]}
                    if selected["subagent_provider_id"]
                    else {"harness_profile_id": selected["subagent_harness_profile_id"]}
                ),
                "model": selected["subagent_model"] or "",
                **(
                    {"max_active": selected["max_active_subagents"]}
                    if selected["max_active_subagents"] is not None
                    else {}
                ),
                **(
                    {"reasoning_effort": selected["subagent_reasoning_effort"]}
                    if selected["subagent_reasoning_effort"] is not None
                    else {}
                ),
            }
        else:
            metadata.pop("provider_subagent", None)
    return store.update(
        ChatSession,
        session.id,
        {"metadata": metadata},
        expected_revision=session.revision,
    )
