"""Exact-version model guidance extracted from signed Toolbox releases."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CATALOG_PROTOCOL = "nebula.toolbox.catalog/v2"
INTERFACE_PROTOCOL = "nebula.toolbox.interface/v2"
MAX_INTERFACE_CATALOG_BYTES = 24_000_000
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^[^\s*]{1,200}$")
VALUE_TYPES = {
    "string",
    "integer",
    "number",
    "boolean",
    "path",
    "url",
    "domain",
    "target",
    "port",
    "duration",
}
MUTABLE_VERSIONS = {"latest", "main", "master", "head", "dev", "nightly"}


class ToolInterfaceError(ValueError):
    """An interface catalog is incomplete, inconsistent, or untrusted."""


def _exact_fields(value: Any, expected: set[str], *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ToolInterfaceError(f"{field} has invalid fields")
    return value


def _version(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or VERSION_PATTERN.fullmatch(value) is None
        or value.casefold() in MUTABLE_VERSIONS
    ):
        raise ToolInterfaceError(f"{field} is not an immutable exact version")
    return value


def _positional(value: Any, *, field: str) -> dict[str, Any]:
    positional = _exact_fields(
        value,
        {"id", "name", "type", "required", "repeatable", "description"},
        field=field,
    )
    if (
        not isinstance(positional["id"], str)
        or NAME_PATTERN.fullmatch(positional["id"]) is None
        or not isinstance(positional["name"], str)
        or not positional["name"]
        or positional["type"] not in VALUE_TYPES
        or not isinstance(positional["required"], bool)
        or not isinstance(positional["repeatable"], bool)
        or not isinstance(positional["description"], str)
        or not positional["description"]
    ):
        raise ToolInterfaceError(f"{field} is invalid")
    return positional


def _option(value: Any, *, field: str) -> dict[str, Any]:
    option = _exact_fields(
        value,
        {
            "id",
            "flags",
            "usage",
            "description",
            "section",
            "value",
            "repeatable",
            "conflicts_with",
            "requires",
            "implies",
        },
        field=field,
    )
    identifier = option["id"]
    flags = option["flags"]
    if (
        not isinstance(identifier, str)
        or not identifier
        or not isinstance(flags, list)
        or not flags
        or len(flags) != len(set(flags))
        or any(
            not isinstance(flag, str)
            or len(flag) < 2
            or not flag.startswith(("-", "+"))
            for flag in flags
        )
        or any(
            not isinstance(option[key], str) or not option[key]
            for key in ("usage", "description", "section")
        )
        or not isinstance(option["repeatable"], bool)
    ):
        raise ToolInterfaceError(f"{field} is invalid")
    for relation in ("conflicts_with", "requires", "implies"):
        values = option[relation]
        if (
            not isinstance(values, list)
            or len(values) != len(set(values))
            or any(not isinstance(item, str) or not item for item in values)
            or identifier in values
        ):
            raise ToolInterfaceError(f"{field}.{relation} is invalid")
    descriptor = option["value"]
    if descriptor is not None:
        required = {"name", "type", "required", "style"}
        optional = {"enum", "minimum", "maximum", "pattern", "default"}
        if (
            not isinstance(descriptor, dict)
            or required - set(descriptor)
            or set(descriptor) - required - optional
            or not isinstance(descriptor["name"], str)
            or not descriptor["name"]
            or descriptor["type"] not in VALUE_TYPES
            or not isinstance(descriptor["required"], bool)
            or descriptor["style"] not in {"separate", "equals", "attached"}
        ):
            raise ToolInterfaceError(f"{field}.value is invalid")
        enum = descriptor.get("enum")
        if enum is not None and (
            not isinstance(enum, list) or not enum or len(enum) != len(set(enum))
        ):
            raise ToolInterfaceError(f"{field}.value.enum is invalid")
        minimum = descriptor.get("minimum")
        maximum = descriptor.get("maximum")
        if (
            minimum is not None
            and (isinstance(minimum, bool) or not isinstance(minimum, (int, float)))
            or maximum is not None
            and (isinstance(maximum, bool) or not isinstance(maximum, (int, float)))
            or minimum is not None
            and maximum is not None
            and minimum > maximum
            or "pattern" in descriptor
            and not isinstance(descriptor["pattern"], str)
        ):
            raise ToolInterfaceError(f"{field}.value constraints are invalid")
        if isinstance(descriptor.get("pattern"), str):
            try:
                re.compile(descriptor["pattern"])
            except re.error as exc:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tool_interfaces.caught_failure_001",
                    "A handled toolbox operation raised an exception.",
                    exc,
                    stage="tool_interfaces",
                )
                raise ToolInterfaceError(f"{field}.value pattern is invalid") from exc
    return option


def _command(value: Any, *, tool_name: str, index: int) -> tuple[str, ...]:
    field = f"{tool_name}.commands[{index}]"
    command = _exact_fields(
        value,
        {"path", "synopsis", "positionals", "options", "help_documents"},
        field=field,
    )
    path = command["path"]
    if (
        not isinstance(path, list)
        or len(path) > 16
        or any(
            not isinstance(part, str) or NAME_PATTERN.fullmatch(part) is None
            for part in path
        )
        or not isinstance(command["synopsis"], str)
        or not command["synopsis"]
        or not isinstance(command["positionals"], list)
        or not isinstance(command["options"], list)
        or not isinstance(command["help_documents"], list)
        or not command["help_documents"]
    ):
        raise ToolInterfaceError(f"{field} is invalid")
    positionals = [
        _positional(item, field=f"{field}.positionals[{item_index}]")
        for item_index, item in enumerate(command["positionals"])
    ]
    positional_ids = [item["id"] for item in positionals]
    if len(positional_ids) != len(set(positional_ids)):
        raise ToolInterfaceError(f"{field} contains ambiguous positionals")
    optional_seen = False
    for item_index, positional in enumerate(positionals):
        optional_seen = optional_seen or not positional["required"]
        if optional_seen and positional["required"]:
            raise ToolInterfaceError(
                f"{field} requires a positional after an optional one"
            )
        if positional["repeatable"] and item_index != len(positionals) - 1:
            raise ToolInterfaceError(f"{field} repeats a non-final positional")
    options = [
        _option(item, field=f"{field}.options[{item_index}]")
        for item_index, item in enumerate(command["options"])
    ]
    option_ids = [item["id"] for item in options]
    flags = [flag for item in options for flag in item["flags"]]
    if len(option_ids) != len(set(option_ids)) or len(flags) != len(set(flags)):
        raise ToolInterfaceError(f"{field} contains ambiguous options")
    available = set(option_ids)
    for option in options:
        for relation in ("conflicts_with", "requires", "implies"):
            if not set(option[relation]).issubset(available):
                raise ToolInterfaceError(f"{field} contains a dangling option relation")
    for document_index, value in enumerate(command["help_documents"]):
        document = _exact_fields(
            value,
            {"command_path", "argv", "exit_code", "sha256", "text"},
            field=f"{field}.help_documents[{document_index}]",
        )
        if (
            document["command_path"] != path
            or not isinstance(document["argv"], list)
            or not document["argv"]
            or any(not isinstance(item, str) or not item for item in document["argv"])
            or not isinstance(document["exit_code"], int)
            or not isinstance(document["sha256"], str)
            or DIGEST_PATTERN.fullmatch(document["sha256"]) is None
            or not isinstance(document["text"], str)
            or not document["text"]
        ):
            raise ToolInterfaceError(f"{field} contains invalid help evidence")
    return tuple(path)


@dataclass(frozen=True)
class ToolInterfaceCatalog:
    payload: dict[str, Any]
    digest: str

    @property
    def tools(self) -> dict[str, dict[str, Any]]:
        return {item["name"]: item for item in self.payload["tools"]}

    def compact_index(self) -> dict[str, Any]:
        catalogued = [
            {
                "name": item["name"],
                "aliases": item["aliases"],
                "version": item["version"],
                "category": item["category"],
                "risk_class": item["risk_class"],
                "description": item["description"],
                "synopsis": item["synopsis"],
                "command_paths": [command["path"] for command in item["commands"]],
            }
            for item in sorted(self.tools.values(), key=lambda value: value["name"])
        ]
        uncatalogued = sorted(
            {
                item["name"]
                for item in self.payload["inventory"]
                if isinstance(item, dict)
                and item.get("catalogued") is False
                and isinstance(item.get("name"), str)
            }
        )
        return {
            "catalogued_tools": catalogued,
            "uncatalogued_executables": uncatalogued,
        }

    def canonical_command_path(
        self, tool_name: str, command_path: list[str]
    ) -> list[str]:
        """Resolve common model path forms without guessing between commands."""

        tool = self.tools.get(tool_name)
        if tool is None:
            raise ToolInterfaceError(f"unknown catalogued tool: {tool_name}")
        if len(command_path) > 17 or any(
            not isinstance(part, str) for part in command_path
        ):
            raise ToolInterfaceError(f"{tool_name} has an invalid command path")
        command_paths = [command["path"] for command in tool["commands"]]
        if command_path in command_paths:
            return list(command_path)

        prefixes = {
            tool["name"],
            tool["executable"],
            Path(tool["executable"]).name,
            *tool["aliases"],
        }
        if command_path and command_path[0] in prefixes:
            without_executable = command_path[1:]
            if without_executable in command_paths:
                return list(without_executable)

        if len(command_paths) == 1:
            return list(command_paths[0])

        raise ToolInterfaceError(
            f"{tool_name} has no command path {' '.join(command_path)!r}"
        )

    def command(self, tool_name: str, command_path: list[str]) -> dict[str, Any]:
        tool = self.tools.get(tool_name)
        if tool is None:
            raise ToolInterfaceError(f"unknown catalogued tool: {tool_name}")
        for command in tool["commands"]:
            if command["path"] == command_path:
                return {
                    "catalog_digest": self.digest,
                    "tool": {
                        key: tool[key]
                        for key in (
                            "name",
                            "aliases",
                            "version",
                            "executable",
                            "category",
                            "risk_class",
                            "description",
                            "synopsis",
                            "examples",
                            "notes",
                        )
                    },
                    "command": {
                        key: command[key]
                        for key in ("path", "synopsis", "positionals", "options")
                    },
                }
        raise ToolInterfaceError(
            f"{tool_name} has no command path {' '.join(command_path)!r}"
        )


COMMAND_SELECTION_SCHEMA = "nebula.toolbox.command-selection/v1"
COMMAND_SELECTOR_NAME = "environment.get_interface"
COMMAND_SELECTOR_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Exact executable inventory name, such as nmap.",
        },
        "command_path": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string"},
            "description": (
                "Subcommand tokens only; use [] for a top-level command and never "
                "include the executable name."
            ),
        },
        "requested_options": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "description": (
                "Flags, option IDs, or short behavior phrases needed for the "
                "operator request, such as -sT, disable DNS, or ports."
            ),
        },
    },
    "required": ["tool", "command_path", "requested_options"],
    "additionalProperties": False,
}


def select_command_interface(
    catalogs: Any, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Return a compact exact interface slice from one signed catalog."""

    if set(arguments) != {"tool", "command_path", "requested_options"}:
        raise ToolInterfaceError("command selection has invalid fields")
    tool_name = arguments["tool"]
    command_path = arguments["command_path"]
    requested = arguments["requested_options"]
    if (
        not isinstance(tool_name, str)
        or not tool_name
        or not isinstance(command_path, list)
        or any(not isinstance(item, str) for item in command_path)
        or not isinstance(requested, list)
        or any(not isinstance(item, str) or not item for item in requested)
    ):
        raise ToolInterfaceError("command selection has invalid values")

    matches = [catalog for catalog in catalogs if tool_name in catalog.tools]
    if not matches:
        raise ToolInterfaceError(f"unknown catalogued tool: {tool_name}")
    if len(matches) != 1:
        raise ToolInterfaceError(f"catalogued tool is ambiguous: {tool_name}")
    catalog = matches[0]
    canonical_path = catalog.canonical_command_path(tool_name, command_path)
    interface = catalog.command(tool_name, canonical_path)
    options = interface["command"]["options"]

    requested_matches: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for query in requested:
        ranked = _rank_interface_options(options, query)
        chosen = [option for _, option in ranked[:5]]
        selected_ids.update(option["id"] for option in chosen)
        requested_matches.append(
            {
                "query": query,
                "options": chosen,
                "matched": bool(chosen),
            }
        )

    selected_options = [option for option in options if option["id"] in selected_ids]
    return {
        "schema": COMMAND_SELECTION_SCHEMA,
        "catalog_digest": catalog.digest,
        "tool": interface["tool"],
        "command": {
            "path": canonical_path,
            "synopsis": interface["command"]["synopsis"],
            "positionals": interface["command"]["positionals"],
            "options": selected_options,
            "requested_option_matches": requested_matches,
            "total_catalogued_options": len(options),
        },
        "instruction": (
            "Use only returned option IDs and positional IDs. Keep this exact tool "
            "and command.path in the next structured environment.run_* invocation. "
            "Call environment.get_interface again for another command."
        ),
    }


def _rank_interface_options(
    options: list[dict[str, Any]], query: str
) -> list[tuple[int, dict[str, Any]]]:
    folded = query.strip().casefold()
    exact = [
        option
        for option in options
        if folded == option["id"].casefold()
        or any(folded == flag.casefold() for flag in option["flags"])
    ]
    if exact:
        return [(10_000, option) for option in exact]
    words = {
        item
        for item in re.findall(r"[a-z0-9]+", folded)
        if len(item) > 1 or item.isdigit()
    }
    ranked: list[tuple[int, dict[str, Any]]] = []
    for option in options:
        haystack = " ".join(
            [
                option["id"],
                *option["flags"],
                option["usage"],
                option["description"],
                option["section"],
            ]
        ).casefold()
        overlap = sum(1 for word in words if word in haystack)
        score = overlap * 10
        if folded and folded in haystack:
            score += 100
        if score:
            ranked.append((score, option))
    return sorted(ranked, key=lambda item: (-item[0], item[1]["id"]))


def selected_environment_capability(selection: dict[str, Any]) -> str:
    tool = selection.get("tool")
    risk = tool.get("risk_class") if isinstance(tool, dict) else None
    if risk in {"local_read", "workspace_write"}:
        return "environment.run_local"
    if risk in {"passive", "active_scan"}:
        return "environment.run_network"
    if risk in {
        "credential_use",
        "exploitation",
        "persistence",
        "destructive",
    }:
        return "environment.run_invasive"
    raise ToolInterfaceError("selected command has an unsupported risk class")


def normalize_selected_invocation_values(
    selection: dict[str, Any], invocation: dict[str, Any]
) -> dict[str, Any]:
    """Coerce unambiguous scalar strings using the signed value descriptors."""

    command = selection.get("command")
    if not isinstance(command, dict):
        raise ToolInterfaceError("selected command is invalid")
    option_descriptors = {
        item.get("id"): item.get("value")
        for item in command.get("options", [])
        if isinstance(item, dict)
    }
    positional_descriptors = {
        item.get("id"): item
        for item in command.get("positionals", [])
        if isinstance(item, dict)
    }

    def normalize_value(value: Any, descriptor: Any) -> Any:
        value_type = descriptor.get("type") if isinstance(descriptor, dict) else None
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if value_type in {"integer", "port"} and re.fullmatch(r"[0-9]+", stripped):
            return int(stripped)
        if value_type == "number" and re.fullmatch(
            r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", stripped
        ):
            return float(stripped)
        if value_type == "boolean" and stripped.casefold() in {"true", "false"}:
            return stripped.casefold() == "true"
        return value

    options = []
    for item in invocation.get("options", []):
        if not isinstance(item, dict):
            options.append(item)
            continue
        options.append(
            {
                **item,
                "value": normalize_value(
                    item.get("value"), option_descriptors.get(item.get("id"))
                ),
            }
            if "value" in item
            else dict(item)
        )
    positionals = []
    for item in invocation.get("positionals", []):
        if not isinstance(item, dict):
            positionals.append(item)
            continue
        positionals.append(
            {
                **item,
                "value": normalize_value(
                    item.get("value"), positional_descriptors.get(item.get("id"))
                ),
            }
            if "value" in item
            else dict(item)
        )
    return {**invocation, "options": options, "positionals": positionals}


def load_interface_catalog(payload: bytes) -> ToolInterfaceCatalog:
    if not payload or len(payload) > MAX_INTERFACE_CATALOG_BYTES:
        raise ToolInterfaceError("Toolbox interface catalog has an invalid size")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.tool_interfaces.caught_failure_002",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="tool_interfaces",
        )
        raise ToolInterfaceError("Toolbox interface catalog is invalid JSON") from exc
    if not isinstance(decoded, dict) or set(decoded) != {
        "protocol",
        "interface_protocol",
        "toolbox_version",
        "tools",
        "inventory",
    }:
        raise ToolInterfaceError(
            "Toolbox interface catalog has invalid top-level fields"
        )
    if (
        decoded["protocol"] != CATALOG_PROTOCOL
        or decoded["interface_protocol"] != INTERFACE_PROTOCOL
    ):
        raise ToolInterfaceError("Toolbox interface catalog protocol is unsupported")
    tools = decoded["tools"]
    inventory = decoded["inventory"]
    if (
        not isinstance(tools, list)
        or not tools
        or len(tools) > 500
        or not isinstance(inventory, list)
        or len(inventory) > 10_000
    ):
        raise ToolInterfaceError("Toolbox interface catalog collections are invalid")
    names: set[str] = set()
    claimed_names: set[str] = set()
    for tool_index, raw_tool in enumerate(tools):
        if not isinstance(raw_tool, dict):
            raise ToolInterfaceError("Toolbox interface entry is invalid")
        required_tool_fields = {
            "protocol",
            "name",
            "version",
            "executable",
            "aliases",
            "category",
            "risk_class",
            "description",
            "homepage",
            "synopsis",
            "examples",
            "notes",
            "commands",
            "coverage",
        }
        if set(raw_tool) not in (
            required_tool_fields,
            required_tool_fields | {"package_version"},
        ):
            raise ToolInterfaceError("Toolbox interface entry has invalid fields")
        tool = raw_tool
        name = tool.get("name")
        aliases = tool.get("aliases")
        commands = tool.get("commands")
        coverage = tool.get("coverage")
        executable = tool.get("executable")
        examples = tool.get("examples")
        notes = tool.get("notes")
        if (
            tool.get("protocol") != INTERFACE_PROTOCOL
            or not isinstance(name, str)
            or NAME_PATTERN.fullmatch(name) is None
            or name in names
            or not isinstance(aliases, list)
            or any(not isinstance(alias, str) or not alias for alias in aliases)
            or len(aliases) != len(set(aliases))
            or name in aliases
            or claimed_names.intersection({name, *aliases})
            or not isinstance(executable, str)
            or not executable.startswith("/")
            or ".." in Path(executable).parts
            or any(
                not isinstance(tool[key], str) or not tool[key]
                for key in (
                    "category",
                    "risk_class",
                    "description",
                    "homepage",
                    "synopsis",
                )
            )
            or not isinstance(commands, list)
            or not commands
            or not isinstance(examples, list)
            or not examples
            or not isinstance(notes, list)
            or not notes
            or any(not isinstance(note, str) or not note for note in notes)
            or not isinstance(coverage, dict)
            or coverage.get("complete") is not True
            or coverage.get("unmapped_options") != []
            or coverage.get("documented_options") != coverage.get("structured_options")
        ):
            raise ToolInterfaceError(f"incomplete Toolbox interface entry: {name}")
        _version(tool.get("version"), field=f"tools[{tool_index}].version")
        if "package_version" in tool:
            _version(
                tool["package_version"],
                field=f"tools[{tool_index}].package_version",
            )
        for example_index, example in enumerate(examples):
            example = _exact_fields(
                example,
                {"purpose", "arguments"},
                field=f"{name}.examples[{example_index}]",
            )
            if (
                not isinstance(example["purpose"], str)
                or not example["purpose"]
                or not isinstance(example["arguments"], list)
                or any(not isinstance(item, str) for item in example["arguments"])
            ):
                raise ToolInterfaceError(f"{name} contains an invalid example")
        expected_coverage_fields = {
            "help_documents",
            "documented_options",
            "structured_options",
            "unmapped_options",
            "complete",
        }
        if set(coverage) != expected_coverage_fields or any(
            not isinstance(coverage[key], int) or coverage[key] < 0
            for key in (
                "help_documents",
                "documented_options",
                "structured_options",
            )
        ):
            raise ToolInterfaceError(f"{name} contains invalid coverage metadata")
        command_paths: set[tuple[str, ...]] = set()
        help_documents = 0
        structured_flags: set[str] = set()
        for command_index, command in enumerate(commands):
            command_path = _command(command, tool_name=name, index=command_index)
            if command_path in command_paths:
                raise ToolInterfaceError(f"{name} contains a duplicate command path")
            help_documents += len(command["help_documents"])
            structured_flags.update(
                flag for option in command["options"] for flag in option["flags"]
            )
            command_paths.add(command_path)
        if coverage["help_documents"] != help_documents or coverage[
            "structured_options"
        ] != len(structured_flags):
            raise ToolInterfaceError(
                f"{name} coverage counts do not match its commands"
            )
        names.add(name)
        claimed_names.update({name, *aliases})
    inventory_keys: set[tuple[str, str]] = set()
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "path",
            "catalogued",
            "interface",
            "aliases",
        }:
            raise ToolInterfaceError("Toolbox executable inventory entry is invalid")
        name = item["name"]
        path = item["path"]
        aliases = item["aliases"]
        key = (name, path)
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(path, str)
            or not path.startswith("/")
            or ".." in Path(path).parts
            or not isinstance(item["catalogued"], bool)
            or item["interface"] is not None
            and item["interface"] not in names
            or not isinstance(aliases, list)
            or any(not isinstance(alias, str) or not alias for alias in aliases)
            or len(aliases) != len(set(aliases))
            or key in inventory_keys
        ):
            raise ToolInterfaceError("Toolbox executable inventory entry is invalid")
        inventory_keys.add(key)
    return ToolInterfaceCatalog(
        payload=decoded,
        digest=hashlib.sha256(payload).hexdigest(),
    )


def load_interface_catalog_file(
    path: Path, expected_digest: str
) -> ToolInterfaceCatalog:
    if not path.is_file() or path.is_symlink():
        raise ToolInterfaceError("stored Toolbox interface catalog is unavailable")
    catalog = load_interface_catalog(path.read_bytes())
    if catalog.digest != expected_digest:
        raise ToolInterfaceError("stored Toolbox interface catalog digest mismatch")
    return catalog


__all__ = [
    "CATALOG_PROTOCOL",
    "INTERFACE_PROTOCOL",
    "MAX_INTERFACE_CATALOG_BYTES",
    "ToolInterfaceCatalog",
    "ToolInterfaceError",
    "load_interface_catalog",
    "load_interface_catalog_file",
]
