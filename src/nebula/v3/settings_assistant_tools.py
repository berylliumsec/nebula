"""Bounded Nebula settings capabilities shared by provider and harness turns."""

from __future__ import annotations

from typing import Any

from .domain import Engagement, HarnessProfile, ProviderProfile, RiskClass
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


# These are settings destinations, not grants to mutate the underlying records.
# A new direct mutation must be implemented through the owning service below.
SETTING_DESTINATIONS: tuple[tuple[str, str, str], ...] = (
    ("appearance", "Appearance and theme", "appearance-settings"),
    ("providers", "Model providers", "provider-settings"),
    ("skills", "Shared skills", "native-skill-settings"),
    ("web-search", "Web search runtime", "web-search-runtime-settings"),
    ("follow-up", "Post-tool assistant", "post-tool-assistant-settings"),
    ("harnesses", "Assistant harnesses", "harness-settings"),
    ("mcp", "MCP servers", "mcp-settings"),
    ("ssh-environments", "Environments", "ssh-environment-settings"),
    ("automation-runtime", "Automation runtime", "automation-runtime-settings"),
    ("vpn-egress", "VPN egress profiles", "automation-runtime-settings"),
    ("runners", "Sandbox runners", "runtime-settings"),
    ("network-scope", "Project policy and network scope", "engagement-policy-settings"),
    ("operators", "Operator profiles", "operator-settings"),
    ("devices", "Paired devices", "device-pairing-settings"),
    ("release", "Release and updates", "release-settings-group"),
    ("diagnostics", "Diagnostics and logging", "diagnostics-settings"),
    ("setup", "Terminal and Core setup", "setup-settings"),
)

CATALOG_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}
POST_TOOL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "setting": {"type": "string", "enum": ["take_notes", "suggest_next_steps"]},
        "enabled": {"type": "boolean"},
    },
    "required": ["setting", "enabled"],
}
DEFAULT_MODEL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["provider", "harness"]},
        "profile_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "model": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["kind", "profile_id", "model"],
}


def catalog() -> dict[str, Any]:
    return {
        "settings": [
            {
                "id": identifier, "label": label,
                "url": f"/settings#{target}",
                "direct_changes": (
                    ["take_notes", "suggest_next_steps"] if identifier == "follow-up" else
                    ["default_model"] if identifier in {"providers", "harnesses"} else []
                ),
            }
            for identifier, label, target in SETTING_DESTINATIONS
        ],
        "device_appearance": {
            "values": ["light", "dark", "studio-dark", "zero-light", "zero-dark"],
            "authority": "browser preference; use the assistant's Appearance controls on that device",
        },
    }


def set_post_tool_preference(
    store: NebulaStore,
    artifact_store: Any,
    engagement_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from .execution_ai import ExecutionAIService

    setting = arguments.get("setting")
    enabled = arguments.get("enabled")
    if setting not in {"take_notes", "suggest_next_steps"} or not isinstance(enabled, bool):
        raise ValueError("choose take_notes or suggest_next_steps and a boolean enabled value")
    engagement = store.get(Engagement, engagement_id)
    service = ExecutionAIService(store=store, artifact_store=artifact_store)
    current = service.get_config(engagement.id)
    saved = service.set_config(engagement.id, current.model_copy(update={setting: enabled}))
    observed = service.get_config(engagement.id)
    if observed != saved:
        raise RuntimeError("Core did not confirm the saved post-tool setting")
    return {
        "setting": setting,
        "enabled": getattr(observed, setting),
        "project_id": engagement.id,
        "project_name": engagement.name,
        "saved": True,
    }


def model_choices(store: NebulaStore) -> dict[str, Any]:
    providers = store.find_entities(ProviderProfile, {}, limit=None)
    harnesses = store.find_entities(HarnessProfile, {}, limit=None)
    return {
        "providers": [
            {
                "id": item.id, "name": item.name,
                "models": item.model_allowlist,
                "default_model": item.metadata.get("default_model"),
            }
            for item in providers if item.enabled and item.model_allowlist
        ],
        "harnesses": [
            {
                "id": item.id, "name": item.name,
                "models": item.capabilities.models,
                "default_model": item.default_model,
            }
            for item in harnesses if item.enabled and item.capabilities.models
        ],
    }


def set_default_model(store: NebulaStore, arguments: dict[str, Any]) -> dict[str, Any]:
    kind = arguments.get("kind")
    profile_id = arguments.get("profile_id")
    model = arguments.get("model")
    if kind not in {"provider", "harness"} or not isinstance(profile_id, str) or not isinstance(model, str):
        raise ValueError("kind, profile_id, and model are required")
    if kind == "provider":
        profile = store.get(ProviderProfile, profile_id)
        choices = profile.model_allowlist
        if not profile.enabled or model not in choices:
            raise ValueError("choose an enabled provider and one of its allowed models")
        store.update(ProviderProfile, profile.id, {
            "metadata": {**profile.metadata, "default_model": model},
        }, expected_revision=profile.revision)
        saved = store.get(ProviderProfile, profile.id)
        observed = saved.metadata.get("default_model")
    else:
        profile = store.get(HarnessProfile, profile_id)
        choices = profile.capabilities.models
        if not profile.enabled or model not in choices:
            raise ValueError("choose an enabled harness and one of its advertised models")
        store.update(HarnessProfile, profile.id, {"default_model": model}, expected_revision=profile.revision)
        saved = store.get(HarnessProfile, profile.id)
        observed = saved.default_model
    if observed != model:
        raise RuntimeError("Core did not confirm the saved default model")
    return {"kind": kind, "profile_id": profile.id, "profile_name": profile.name,
            "default_model": observed, "saved": True}


def provider_plugins(store: NebulaStore, artifact_store: Any, engagement_id: str) -> list[AnalysisTool]:
    async def list_settings(_arguments: dict[str, Any]) -> dict[str, Any]:
        return catalog()

    async def set_post_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        return set_post_tool_preference(store, artifact_store, engagement_id, arguments)

    async def list_models(_arguments: dict[str, Any]) -> dict[str, Any]:
        return model_choices(store)

    async def save_default_model(arguments: dict[str, Any]) -> dict[str, Any]:
        return set_default_model(store, arguments)

    return [
        AnalysisTool(ToolSpec(
            name="nebula.settings.catalog", description="List Nebula setting destinations and bounded direct changes. Use this before answering where a setting is or what can be changed.",
            input_schema=CATALOG_SCHEMA, output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.LOCAL_READ, idempotency=IdempotencyBehavior.SAFE,
        ), list_settings),
        AnalysisTool(ToolSpec(
            name="nebula.settings.set_post_tool", description="Change one post-tool note or next-step preference for the current project and read it back. The analysis runtime must already be configured before enabling either preference.",
            input_schema=POST_TOOL_SCHEMA, output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.WORKSPACE_WRITE, idempotency=IdempotencyBehavior.SAFE,
        ), set_post_tool),
        AnalysisTool(ToolSpec(
            name="nebula.settings.models", description="List enabled provider and harness profiles and their allowed or advertised models before changing a default.",
            input_schema=CATALOG_SCHEMA, output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.LOCAL_READ, idempotency=IdempotencyBehavior.SAFE,
        ), list_models),
        AnalysisTool(ToolSpec(
            name="nebula.settings.set_default_model", description="Set a provider or harness default model for future chats. First call nebula.settings.models and select a listed model. Read back the saved default.",
            input_schema=DEFAULT_MODEL_SCHEMA, output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.WORKSPACE_WRITE, idempotency=IdempotencyBehavior.SAFE,
        ), save_default_model),
    ]
