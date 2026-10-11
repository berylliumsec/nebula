"""Bounded Nebula settings capabilities shared by provider and harness turns."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from .domain import (
    AutomationProjectPolicy,
    Engagement,
    HarnessProfile,
    ProviderProfile,
    RiskClass,
)
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

# Packaged with Core and imported by the browser build. IDs are the only
# navigation arguments accepted from a model; the browser resolves the route.
APP_DESTINATIONS: tuple[dict[str, Any], ...] = tuple(
    json.loads(
        resources.files("nebula.v3")
        .joinpath("assistant_destinations.json")
        .read_text(encoding="utf-8")
    )
)

DEVICE_THEMES = (
    "light",
    "dark",
    "glacier",
    "amber",
    "zero-light",
    "zero-dark",
    "zero-classic",
    "studio-dark",
)
PROJECT_MODES = ("docker", "host")

CATALOG_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}
SEARCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"query": {"type": "string", "minLength": 3, "maxLength": 1000}},
    "required": ["query"],
}
OPEN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "setting_id": {
            "type": "string",
            "enum": [item[0] for item in SETTING_DESTINATIONS],
        }
    },
    "required": ["setting_id"],
}
NAVIGATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "destination_id": {
            "type": "string",
            "enum": [item["id"] for item in APP_DESTINATIONS],
        }
    },
    "required": ["destination_id"],
}
THEME_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "theme": {
            "type": "string",
            "enum": list(DEVICE_THEMES),
        }
    },
    "required": ["theme"],
}
PROJECT_MODE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {"type": "string", "enum": list(PROJECT_MODES)},
        "from": {"type": "string", "enum": list(PROJECT_MODES)},
    },
    "required": ["mode"],
}
POST_TOOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "setting": {"type": "string", "enum": ["take_notes", "suggest_next_steps"]},
        "enabled": {"type": "boolean"},
    },
    "required": ["setting", "enabled"],
}
DEFAULT_MODEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["provider", "harness"]},
        "profile_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "model": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["kind", "profile_id", "model"],
}


def catalog() -> dict[str, Any]:
    return {
        "actions": [
            {
                "name": "nebula.navigate.open",
                "argument": "destination_id",
                "values": [item["id"] for item in APP_DESTINATIONS],
                "effect": "navigate the requesting browser to a listed Nebula page",
            },
            {
                "name": "nebula.settings.open",
                "argument": "setting_id",
                "values": [item[0] for item in SETTING_DESTINATIONS],
                "effect": "open the setting on the requesting browser",
            },
            {
                "name": "nebula.settings.set_device_theme",
                "argument": "theme",
                "values": list(DEVICE_THEMES),
                "effect": "apply a theme on the requesting browser",
            },
            {
                "name": "nebula.settings.set_project_mode",
                "argument": "mode",
                "values": ["docker", "host"],
                "effect": "show the selected project's policy, confirm Host access if needed, and save for new sessions",
            },
            {
                "name": "nebula.settings.set_post_tool",
                "arguments": ["setting", "enabled"],
                "effect": "save one project post-tool preference in Core",
            },
            {
                "name": "nebula.settings.set_default_model",
                "arguments": ["kind", "profile_id", "model"],
                "effect": "save one listed default model in Core",
            },
        ],
        "settings": [
            {
                "id": identifier,
                "label": label,
                "url": f"/settings#{target}",
                "direct_changes": (
                    ["take_notes", "suggest_next_steps"]
                    if identifier == "follow-up"
                    else ["default_model"]
                    if identifier in {"providers", "harnesses"}
                    else []
                ),
                "assistant_ui_actions": (
                    ["device_theme"]
                    if identifier == "appearance"
                    else ["project_execution_mode"]
                    if identifier == "network-scope"
                    else []
                ),
            }
            for identifier, label, target in SETTING_DESTINATIONS
        ],
        "destinations": list(APP_DESTINATIONS),
        "device_appearance": {
            "values": list(DEVICE_THEMES),
            "authority": "browser preference; the assistant UI applies explicit theme commands on the active device",
        },
    }


def open_setting(arguments: dict[str, Any]) -> dict[str, Any]:
    setting_id = arguments.get("setting_id")
    if not isinstance(setting_id, str) or setting_id not in {
        item[0] for item in SETTING_DESTINATIONS
    }:
        raise ValueError("choose a setting_id from nebula.settings.catalog")
    return {
        "state": "pending_browser_apply",
        "client_action": {"kind": "open_setting", "setting_id": setting_id},
    }


def open_destination(arguments: dict[str, Any]) -> dict[str, Any]:
    destination_id = arguments.get("destination_id")
    if not isinstance(destination_id, str) or destination_id not in {
        item["id"] for item in APP_DESTINATIONS
    }:
        raise ValueError("choose a destination_id from nebula.settings.catalog")
    return {
        "state": "pending_browser_apply",
        "client_action": {"kind": "navigate", "destination_id": destination_id},
    }


def request_device_theme(arguments: dict[str, Any]) -> dict[str, Any]:
    theme = arguments.get("theme")
    if theme not in DEVICE_THEMES:
        raise ValueError("choose a listed device theme")
    return {
        "state": "pending_browser_apply",
        "client_action": {"kind": "device_theme", "theme": theme},
    }


def request_project_execution_mode(arguments: dict[str, Any]) -> dict[str, Any]:
    mode = arguments.get("mode")
    source = arguments.get("from")
    if mode not in PROJECT_MODES:
        raise ValueError("choose docker or host mode")
    if source is not None and source not in PROJECT_MODES:
        raise ValueError("choose docker or host as the source mode")
    return {
        "state": "pending_browser_apply",
        "client_action": {
            "kind": "project_execution_mode",
            "mode": mode,
            **({"from": source} if source else {}),
        },
    }


def project_context(store: NebulaStore, engagement_id: str) -> dict[str, Any]:
    """Read the selected project's runtime facts without creating a policy."""

    engagement = store.get(Engagement, engagement_id)
    policies = store.find_entities(
        AutomationProjectPolicy,
        {"engagement_id": engagement.id},
        limit=1,
    )
    policy = policies[0] if policies else None
    return {
        "project_id": engagement.id,
        "project_name": engagement.name,
        "execution_mode": policy.execution_mode if policy else "docker",
        "policy_source": "stored" if policy else "default",
        "policy_description": (
            "Host mode is stored in Core with host-access acknowledgment."
            if policy and policy.execution_mode == "host"
            else "Docker mode is stored in Core. It may be the automatically created default or an operator selection; Core does not record which."
            if policy
            else "No execution policy is stored for this project; Docker is the effective default."
        ),
        "linked_workspace_path": engagement.workspace_path,
        "workspace_kind": "linked_host_folder"
        if engagement.workspace_path
        else "nebula_managed",
        "docker_access": "Commands can access the project workspace, including a linked host folder, but not arbitrary host paths outside it.",
        "host_access": "Host mode runs new command sessions as the Nebula server's OS user, subject to that user's filesystem permissions.",
        "existing_sessions": "Existing command sessions keep their frozen execution mode; start a new session after a policy change.",
        "setting_url": "/settings#engagement-policy-settings",
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
    if setting not in {"take_notes", "suggest_next_steps"} or not isinstance(
        enabled, bool
    ):
        raise ValueError(
            "choose take_notes or suggest_next_steps and a boolean enabled value"
        )
    engagement = store.get(Engagement, engagement_id)
    service = ExecutionAIService(store=store, artifact_store=artifact_store)
    current = service.get_config(engagement.id)
    saved = service.set_config(
        engagement.id, current.model_copy(update={setting: enabled})
    )
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
                "id": item.id,
                "name": item.name,
                "models": item.model_allowlist,
                "default_model": item.metadata.get("default_model"),
            }
            for item in providers
            if item.enabled and item.model_allowlist
        ],
        "harnesses": [
            {
                "id": item.id,
                "name": item.name,
                "models": item.capabilities.models,
                "default_model": item.default_model,
            }
            for item in harnesses
            if item.enabled and item.capabilities.models
        ],
    }


def set_default_model(store: NebulaStore, arguments: dict[str, Any]) -> dict[str, Any]:
    kind = arguments.get("kind")
    profile_id = arguments.get("profile_id")
    model = arguments.get("model")
    if (
        kind not in {"provider", "harness"}
        or not isinstance(profile_id, str)
        or not isinstance(model, str)
    ):
        raise ValueError("kind, profile_id, and model are required")
    if kind == "provider":
        provider = store.get(ProviderProfile, profile_id)
        choices = provider.model_allowlist
        if not provider.enabled or model not in choices:
            raise ValueError("choose an enabled provider and one of its allowed models")
        store.update(
            ProviderProfile,
            provider.id,
            {
                "metadata": {**provider.metadata, "default_model": model},
            },
            expected_revision=provider.revision,
        )
        saved_provider = store.get(ProviderProfile, provider.id)
        observed = saved_provider.metadata.get("default_model")
        profile_name = provider.name
    else:
        harness = store.get(HarnessProfile, profile_id)
        choices = harness.capabilities.models
        if not harness.enabled or model not in choices:
            raise ValueError(
                "choose an enabled harness and one of its advertised models"
            )
        store.update(
            HarnessProfile,
            harness.id,
            {"default_model": model},
            expected_revision=harness.revision,
        )
        saved_harness = store.get(HarnessProfile, harness.id)
        observed = saved_harness.default_model
        profile_name = harness.name
    if observed != model:
        raise RuntimeError("Core did not confirm the saved default model")
    return {
        "kind": kind,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "default_model": observed,
        "saved": True,
    }


def provider_plugins(
    store: NebulaStore, artifact_store: Any, engagement_id: str
) -> list[AnalysisTool]:
    async def list_settings(_arguments: dict[str, Any]) -> dict[str, Any]:
        return catalog()

    async def open_setting_on_device(arguments: dict[str, Any]) -> dict[str, Any]:
        return open_setting(arguments)

    async def navigate_on_device(arguments: dict[str, Any]) -> dict[str, Any]:
        return open_destination(arguments)

    async def select_device_theme(arguments: dict[str, Any]) -> dict[str, Any]:
        return request_device_theme(arguments)

    async def select_project_execution_mode(
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return request_project_execution_mode(arguments)

    async def set_post_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        return set_post_tool_preference(store, artifact_store, engagement_id, arguments)

    async def list_models(_arguments: dict[str, Any]) -> dict[str, Any]:
        return model_choices(store)

    async def read_project_context(_arguments: dict[str, Any]) -> dict[str, Any]:
        return project_context(store, engagement_id)

    async def search_help(arguments: dict[str, Any]) -> dict[str, Any]:
        from .product_knowledge import search_product_knowledge

        query = arguments.get("query")
        if not isinstance(query, str) or not 3 <= len(query) <= 1000:
            raise ValueError("provide a product-help query of 3 to 1000 characters")
        return {
            "matches": search_product_knowledge(query),
            "source": "bundled Nebula product knowledge",
        }

    async def save_default_model(arguments: dict[str, Any]) -> dict[str, Any]:
        return set_default_model(store, arguments)

    return [
        AnalysisTool(
            ToolSpec(
                name="nebula.help.search",
                description="Search the packaged, source-linked Nebula product documentation index. Use for usage or troubleshooting questions; cite the returned source and check live project facts separately with nebula.help.project_context.",
                input_schema=SEARCH_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            search_help,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.help.project_context",
                description="Read the selected project's Docker or Host mode and linked workspace. Call before explaining host-file access, command isolation, or a failed host-path edit; this tool changes nothing.",
                input_schema=CATALOG_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            read_project_context,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.catalog",
                description="List Nebula setting destinations and bounded direct changes. Use this before answering where a setting is or what can be changed.",
                input_schema=CATALOG_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            list_settings,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.open",
                description="Open one listed Nebula setting on the requesting browser. Call with its setting_id for any request to show or navigate to a setting; call nebula.settings.catalog first only when the destination is unclear. Returns a pending browser action, not proof that a lens opened.",
                input_schema=OPEN_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            open_setting_on_device,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.navigate.open",
                description="Open a listed Nebula page or Workbench view in the requesting browser, including All projects. Call with a destination_id from nebula.settings.catalog; the browser supplies the selected project ID. Returns a pending browser action, not proof of navigation.",
                input_schema=NAVIGATE_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            navigate_on_device,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.set_device_theme",
                description="Request a listed device theme on the requesting browser, including Glacier, Amber, and Zero Classic. Use zero-dark for 'Zero theme'. Returns a pending browser action; the browser applies and confirms it. Do not claim it was saved from this tool result alone.",
                input_schema=THEME_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            select_device_theme,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.set_project_mode",
                description="Request Docker or Host mode for the selected project's future command sessions. The requesting browser opens Project Policy, asks for Host access approval if needed, saves through Core, and reads back the result. This tool only returns a pending action, not a saved change.",
                input_schema=PROJECT_MODE_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            select_project_execution_mode,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.set_post_tool",
                description="Change one post-tool note or next-step preference for the current project and read it back. The analysis runtime must already be configured before enabling either preference.",
                input_schema=POST_TOOL_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.WORKSPACE_WRITE,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            set_post_tool,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.models",
                description="List enabled provider and harness profiles and their allowed or advertised models before changing a default.",
                input_schema=CATALOG_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            list_models,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.set_default_model",
                description="Set a provider or harness default model for future chats. First call nebula.settings.models and select a listed model. Read back the saved default.",
                input_schema=DEFAULT_MODEL_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.WORKSPACE_WRITE,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            save_default_model,
        ),
    ]
