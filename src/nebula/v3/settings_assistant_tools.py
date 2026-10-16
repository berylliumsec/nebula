"""Bounded Nebula settings capabilities shared by provider and harness turns."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from .settings_assistant_api import SettingsAssistantApi

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
    ("butler-voice", "Butler Voice timeout", "butler-settings"),
    ("usage", "Usage and credits", "usage-settings"),
    ("appearance", "Appearance and theme", "appearance-settings"),
    ("providers", "Model providers", "provider-settings"),
    ("skills", "Shared skills", "native-skill-settings"),
    ("web-search", "Web search runtime", "web-search-runtime-settings"),
    ("storage", "Storage sources", "storage-integration-settings"),
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
PROJECT_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["bounded", "all_destinations", "allow_all"],
        },
        "allowed_cidrs": {"type": "array", "items": {"type": "string"}},
        "allowed_domains": {"type": "array", "items": {"type": "string"}},
        "allowed_urls": {"type": "array", "items": {"type": "string"}},
        "allowed_ports": {"type": "array", "items": {"type": "integer"}},
        "not_before": {"type": ["string", "null"]},
        "not_after": {"type": ["string", "null"]},
        "prohibited_actions": {"type": "array", "items": {"type": "string"}},
        "local_only": {"type": "boolean"},
        "tool_suggestions": {"type": "boolean"},
        "on_demand_tools": {"type": "boolean"},
        "web_search": {"type": "boolean"},
        "web_search_discloses_scope": {"type": "boolean"},
        "always_loaded_tools": {"type": "array", "items": {"type": "string"}},
        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 256},
    },
    "minProperties": 1,
}
HEADLESS_SESSION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"action": {"type": "string", "enum": ["start", "stop"]}},
    "required": ["action"],
}
NOTEBOOK_SETTINGS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {"enum": ["host", "docker"]},
        "mcp_enabled": {"type": "boolean"},
        "network_enabled": {"type": "boolean"},
    },
    "minProperties": 1,
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
SETTINGS_API_SCHEMA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "maxLength": 600},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
    },
}
SETTINGS_API_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 600},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
        "body": {"type": ["object", "array", "null"]},
    },
    "required": ["path", "method"],
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
                "effect": "save the selected project's command mode for new sessions and read it back",
            },
            {
                "name": "nebula.settings.set_project_scope",
                "arguments": list(PROJECT_SCOPE_SCHEMA["properties"]),
                "effect": "save the selected project's scope and network settings and read them back",
            },
            {
                "name": "nebula.settings.headless_session",
                "argument": "action",
                "values": ["start", "stop"],
                "effect": "start or stop the selected project's Core-owned headless security browser",
            },
            {
                "name": "nebula.settings.api_schema",
                "arguments": ["path", "method"],
                "effect": "discover the current Core build's supported Settings routes and request schemas",
            },
            {
                "name": "nebula.settings.api_request",
                "arguments": ["path", "method", "body"],
                "effect": "call an existing authenticated Settings route for the selected project and read back when available",
            },
            {
                "name": "nebula.settings.notebook",
                "effect": "read project notebook mode, Host consent, and Notebook MCP grant",
            },
            {
                "name": "nebula.settings.set_notebook",
                "arguments": ["mode", "mcp_enabled", "network_enabled"],
                "effect": "save notebook-relevant project settings in Core; first Host consent remains operator-only",
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
                    ["mode", "mcp_enabled", "network_enabled"]
                    if identifier == "mcp"
                    else ["take_notes", "suggest_next_steps"]
                    if identifier == "follow-up"
                    else ["default_model"]
                    if identifier in {"providers", "harnesses"}
                    else []
                ),
                "assistant_ui_actions": (
                    ["device_theme"]
                    if identifier == "appearance"
                    else ["project_execution_mode", "project_scope"]
                    if identifier == "network-scope"
                    else ["headless_session"]
                    if identifier == "automation-runtime"
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


def notebook_settings(store: NebulaStore, engagement_id: str) -> dict[str, Any]:
    from .notebook_mcp import NotebookMcpTools

    store.get(Engagement, engagement_id)
    profile = NotebookMcpTools.ensure_profile(store, engagement_id)
    policies = store.find_entities(
        AutomationProjectPolicy, {"engagement_id": engagement_id}, limit=1
    )
    policy = policies[0] if policies else None
    return {
        "project_id": engagement_id,
        "mode": policy.execution_mode if policy else "docker",
        "host_access_acknowledged": policy.host_access_acknowledged
        if policy
        else False,
        "mcp_enabled": profile.enabled,
        "network_enabled": policy.network_enabled if policy else True,
        "profile_id": profile.id,
        "revision": policy.revision if policy else None,
    }


async def set_notebook_settings(
    store: NebulaStore, notebooks: Any, engagement_id: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    from .domain import McpServerProfile
    from .notebook_mcp import NotebookMcpTools

    if notebooks is None or notebooks.automation_runtime is None:
        raise ValueError("Notebook settings are unavailable in this Core")
    mode = arguments.get("mode")
    enabled = arguments.get("mcp_enabled")
    network = arguments.get("network_enabled")
    if (
        not arguments
        or mode not in {None, "host", "docker"}
        or (enabled is not None and not isinstance(enabled, bool))
        or (network is not None and not isinstance(network, bool))
    ):
        raise ValueError(
            "set mode, mcp_enabled, or network_enabled using listed values"
        )
    current = notebooks.policy(engagement_id)
    if mode == "host" and not current.host_access_acknowledged:
        raise ValueError(
            "Host notebook execution needs the operator's one-time acknowledgement in Project settings"
        )
    if mode is not None:
        await notebooks.prepare_policy_change(engagement_id, mode)
    if mode is not None or network is not None:
        notebooks.automation_runtime.update_project_policy(
            engagement_id,
            execution_mode=mode or current.execution_mode,
            host_access_acknowledged=False,
            approval_policy=current.approval_policy,
            network_enabled=current.network_enabled if network is None else network,
            runner_profile_id=current.runner_profile_id,
            vpn_profile_id=current.vpn_profile_id,
            max_timeout_ms=current.max_timeout_ms,
            expected_revision=current.revision,
        )
    if enabled is not None:
        profile = NotebookMcpTools.ensure_profile(store, engagement_id)
        store.update(
            McpServerProfile,
            profile.id,
            {"enabled": enabled},
            expected_revision=profile.revision,
        )
    return {**notebook_settings(store, engagement_id), "saved": True}


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


def request_project_scope(arguments: dict[str, Any]) -> dict[str, Any]:
    if not arguments or set(arguments) - set(PROJECT_SCOPE_SCHEMA["properties"]):
        raise ValueError("choose project scope fields from nebula.settings.catalog")
    mode = arguments.get("mode")
    if mode is not None and mode not in {"bounded", "all_destinations", "allow_all"}:
        raise ValueError("choose bounded, all_destinations, or allow_all")
    for name, value in arguments.items():
        expected = PROJECT_SCOPE_SCHEMA["properties"][name]
        if name == "mode":
            continue
        field_type = expected["type"]
        if field_type == "boolean" and type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")
        if field_type == "integer" and (
            type(value) is not int or not 1 <= value <= 256
        ):
            raise ValueError(f"{name} must be an integer from 1 to 256")
        if field_type == "array" and (
            not isinstance(value, list)
            or any(
                type(item)
                is not (int if expected["items"]["type"] == "integer" else str)
                for item in value
            )
        ):
            raise ValueError(f"{name} has invalid entries")
        if (
            field_type == ["string", "null"]
            and value is not None
            and not isinstance(value, str)
        ):
            raise ValueError(f"{name} must be a date string or null")
    return {
        "state": "pending_browser_apply",
        "client_action": {"kind": "project_scope", "changes": arguments},
    }


def request_headless_session(arguments: dict[str, Any]) -> dict[str, Any]:
    action = arguments.get("action")
    if set(arguments) != {"action"} or action not in {"start", "stop"}:
        raise ValueError("choose start or stop for the headless session")
    return {
        "state": "pending_browser_apply",
        "client_action": {"kind": "headless_session", "action": action},
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
        "execution_mode": policy.execution_mode if policy else "host",
        "policy_source": "stored" if policy else "default",
        "policy_description": (
            "Host mode is stored in Core with host-access acknowledgment."
            if policy
            and policy.execution_mode == "host"
            and policy.host_access_acknowledged
            else "Host mode is selected, but execution awaits one-time host-access acknowledgment."
            if policy and policy.execution_mode == "host"
            else "Docker mode is stored in Core. It may be the automatically created default or an operator selection; Core does not record which."
            if policy
            else "No execution policy is stored for this project; Host will be selected pending acknowledgment."
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
    store: NebulaStore,
    artifact_store: Any,
    engagement_id: str,
    *,
    settings_api: SettingsAssistantApi | None = None,
    notebooks: Any = None,
    work: Any = None,
    all_work: bool = False,
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

    async def select_project_scope(arguments: dict[str, Any]) -> dict[str, Any]:
        return request_project_scope(arguments)

    async def select_headless_session(arguments: dict[str, Any]) -> dict[str, Any]:
        return request_headless_session(arguments)

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

    async def settings_schema(arguments: dict[str, Any]) -> dict[str, Any]:
        if settings_api is None:
            raise ValueError("Core Settings API is unavailable")
        return settings_api.schema(
            path=arguments.get("path"), method=arguments.get("method")
        )

    async def settings_request(arguments: dict[str, Any]) -> dict[str, Any]:
        if settings_api is None:
            raise ValueError("Core Settings API is unavailable")
        return await settings_api.request(
            engagement_id,
            path=arguments.get("path"),
            method=arguments.get("method"),
            body=arguments.get("body"),
        )

    async def read_notebook(_arguments: dict[str, Any]) -> dict[str, Any]:
        return notebook_settings(store, engagement_id)

    async def save_notebook(arguments: dict[str, Any]) -> dict[str, Any]:
        return await set_notebook_settings(store, notebooks, engagement_id, arguments)

    plugins = [
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.notebook",
                description="Read this project's Notebook mode, Host consent, network policy, and MCP grant before troubleshooting or changing them.",
                input_schema=CATALOG_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            read_notebook,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.set_notebook",
                description="Save Notebook mode, MCP grant or project network availability, then read back Core state. First-time Host consent must come from the operator.",
                input_schema=NOTEBOOK_SETTINGS_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.WORKSPACE_WRITE,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            save_notebook,
        ),
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
                name="nebula.settings.api_schema",
                description="Discover authenticated Core Settings routes in this build, or inspect the exact request schema for one listed path and method. Use before changing settings without a dedicated tool.",
                input_schema=SETTINGS_API_SCHEMA_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            settings_schema,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.api_request",
                description="Call one listed Core Settings route for the selected project. Read current state and schema first; preserve unrelated fields and revision. Core validates and saves. Report saved state only when read_back is true or a subsequent GET confirms it; no additional confirmation for an explicit operator request.",
                input_schema=SETTINGS_API_REQUEST_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.WORKSPACE_WRITE,
                idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
            ),
            settings_request,
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
                description="Set Docker or Host mode for the selected project's future command sessions. The requesting browser saves through Core and reads back the result without another confirmation. This tool only returns a pending action, not a saved change.",
                input_schema=PROJECT_MODE_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            select_project_execution_mode,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.set_project_scope",
                description="Set the selected project's scope. Use mode=all_destinations for all URLs, network destinations and ports while keeping other permissions; mode=allow_all also bypasses Nebula permission checks. Preserve unrelated fields, save through Core, and read back. A direct operator instruction needs no extra confirmation. This tool returns a pending browser action, not a saved change.",
                input_schema=PROJECT_SCOPE_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.WORKSPACE_WRITE,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            select_project_scope,
        ),
        AnalysisTool(
            ToolSpec(
                name="nebula.settings.headless_session",
                description="Start or stop the selected project's Core-owned headless security browser. This is separate from the Kali command runtime. The requesting browser applies the action and reads back the session state; this tool returns only a pending action.",
                input_schema=HEADLESS_SESSION_SCHEMA,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.WORKSPACE_WRITE,
                idempotency=IdempotencyBehavior.SAFE,
            ),
            select_headless_session,
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
    if work is not None:
        from .control_assistant_work import (
            WORK_TOOLS,
            _ALL_WORK_TOOLS,
            dispatch_work_tool,
        )

        for name, description, schema, read_only in (
            _ALL_WORK_TOOLS if all_work else WORK_TOOLS
        ):

            async def run_work(
                arguments: dict[str, Any], *, tool_name: str = name
            ) -> dict[str, Any]:
                return await dispatch_work_tool(
                    work, engagement_id, tool_name, arguments
                )

            plugins.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=RiskClass.LOCAL_READ
                        if read_only
                        else RiskClass.WORKSPACE_WRITE,
                        idempotency=IdempotencyBehavior.SAFE
                        if read_only
                        else IdempotencyBehavior.NON_IDEMPOTENT,
                    ),
                    run_work,
                )
            )
    return plugins
