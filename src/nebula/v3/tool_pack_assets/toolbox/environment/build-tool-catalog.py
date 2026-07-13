#!/usr/bin/env python3
"""Build the exact-version Nebula Toolbox command interface catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


SOURCE_PROTOCOL = "nebula.toolbox.interface-source/v2"
INTERFACE_PROTOCOL = "nebula.toolbox.interface/v2"
CATALOG_PROTOCOL = "nebula.toolbox.catalog/v2"
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
VERSION_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FLAG = re.compile(
    r"(?<![A-Za-z0-9_])(--?[A-Za-z0-9?][A-Za-z0-9_.?+-]*|\+[A-Za-z0-9][A-Za-z0-9_-]*)"
)
SECTION = re.compile(r"^[A-Z][A-Z0-9 /_-]{2,}:$")
RISK_CLASSES = {
    "local_read",
    "workspace_write",
    "passive",
    "active_scan",
    "credential_use",
    "exploitation",
    "persistence",
    "destructive",
}
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
MAX_DOCUMENT_BYTES = 512_000
MAX_CATALOG_BYTES = 24_000_000
MAX_DISCOVERED_COMMANDS = 256
TRUSTED_PATHS = (
    "/opt/nebula/venv/bin",
    "/opt/nebula/nmap/bin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/bin",
    "/sbin",
)
INTERNAL_EXECUTABLES = {"nebula-egress", "nebula-toolbox", "build-tool-catalog"}


class CatalogBuildError(RuntimeError):
    """The reviewed interface sources and installed environment disagree."""


def _load_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CatalogBuildError(f"cannot read versions from {path}") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CatalogBuildError(f"invalid version line {line_number}")
        key, value = line.split("=", 1)
        if (
            not VERSION_KEY.fullmatch(key)
            or not value
            or any(character.isspace() for character in value)
        ):
            raise CatalogBuildError(f"unsafe version line {line_number}")
        if key in versions:
            raise CatalogBuildError(f"duplicate version key: {key}")
        versions[key] = value
    if "TOOLBOX_VERSION" not in versions:
        raise CatalogBuildError("TOOLBOX_VERSION is missing")
    return versions


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CatalogBuildError(f"cannot load interface YAML from {path}") from exc
    if not isinstance(payload, dict):
        raise CatalogBuildError(f"{path} must contain one YAML object")
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogBuildError(f"cannot load JSON from {path}") from exc
    if not isinstance(payload, dict):
        raise CatalogBuildError(f"{path} must contain one JSON object")
    return payload


def _string(value: Any, *, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CatalogBuildError(f"{field} must be a non-empty string")
    return value


def _argv(value: Any, *, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 64
        or any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument) > 4096
            for argument in value
        )
    ):
        raise CatalogBuildError(f"{field} must be a bounded argv array")
    executable = Path(value[0])
    if not executable.is_absolute() or ".." in executable.parts:
        raise CatalogBuildError(f"{field} must start with an absolute executable")
    return list(value)


def _run(argv: list[str], *, field: str) -> tuple[int, str]:
    environment = {
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "MANPAGER": "cat",
        "PATH": os.environ.get("PATH", ""),
        "NUCLEI_TEMPLATES": os.environ.get("NUCLEI_TEMPLATES", ""),
        "SEMGREP_ENABLE_VERSION_CHECK": "0",
        "SEMGREP_SEND_METRICS": "off",
    }
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CatalogBuildError(f"cannot run {field}: {argv[0]}") from exc
    raw = completed.stdout[: MAX_DOCUMENT_BYTES + 1]
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise CatalogBuildError(f"{field} output exceeds {MAX_DOCUMENT_BYTES} bytes")
    text = ANSI.sub("", raw.decode("utf-8", errors="replace")).replace("\r\n", "\n")
    return completed.returncode, text.rstrip()


def _examples(value: Any, *, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 30:
        raise CatalogBuildError(f"{name}.examples must be a non-empty array")
    result: list[dict[str, Any]] = []
    for index, example in enumerate(value):
        if not isinstance(example, dict) or set(example) != {"purpose", "arguments"}:
            raise CatalogBuildError(f"{name}.examples[{index}] is invalid")
        arguments = example["arguments"]
        if not isinstance(arguments, list) or any(
            not isinstance(argument, str) or "\x00" in argument
            for argument in arguments
        ):
            raise CatalogBuildError(f"{name}.examples[{index}].arguments is invalid")
        result.append(
            {
                "purpose": _string(
                    example["purpose"], field=f"{name}.examples[{index}].purpose"
                ),
                "arguments": list(arguments),
            }
        )
    return result


def _notes(value: Any, *, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 30
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise CatalogBuildError(f"{name}.notes must be a non-empty string array")
    return list(value)


def _positionals(value: Any, *, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 32:
        raise CatalogBuildError(f"{field} must be an array")
    result: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise CatalogBuildError(f"{field}[{index}] must be an object")
        required = {"id", "name", "type", "required", "repeatable", "description"}
        if set(item) != required:
            raise CatalogBuildError(f"{field}[{index}] has invalid fields")
        identifier = _string(item["id"], field=f"{field}[{index}].id", maximum=128)
        if not NAME.fullmatch(identifier) or identifier in identifiers:
            raise CatalogBuildError(f"{field}[{index}].id is invalid or duplicated")
        value_type = item["type"]
        if value_type not in VALUE_TYPES:
            raise CatalogBuildError(f"{field}[{index}].type is invalid")
        if not isinstance(item["required"], bool) or not isinstance(
            item["repeatable"], bool
        ):
            raise CatalogBuildError(f"{field}[{index}] flags must be boolean")
        identifiers.add(identifier)
        result.append(dict(item))
    seen_optional = False
    for item in result:
        seen_optional = seen_optional or not item["required"]
        if seen_optional and item["required"]:
            raise CatalogBuildError(
                f"{field} cannot require a value after an optional one"
            )
        if item["repeatable"] and item is not result[-1]:
            raise CatalogBuildError(
                f"{field} permits repetition only on the final positional"
            )
    return result


def _expanded_flags(signature: str) -> list[str]:
    result: list[str] = []
    for match in FLAG.finditer(signature):
        flag = match.group(1).rstrip("=:")
        if flag not in result:
            result.append(flag)
    # Help commonly compresses distinct one-dash options as ``-sS/sT/sA`` or
    # ``-4/-6``.  These are separate switches, not aliases.  Recover the
    # omitted dash without carrying any characters from the first switch.
    for token in re.findall(r"\S+/\S+", signature):
        parts = [part.strip("[](),:") for part in token.split("/")]
        if not parts or not parts[0].startswith("-"):
            continue
        for part in parts[1:]:
            candidate = part if part.startswith("-") else f"-{part}"
            candidate = candidate.rstrip("=:")
            if FLAG.fullmatch(candidate) and candidate not in result:
                result.append(candidate)
    return result


def _flags_are_distinct_choices(signature: str, flags: list[str]) -> bool:
    """Return true when slash notation denotes mutually exclusive switches."""

    return (
        "/" in signature
        and len(flags) > 1
        and all(flag.startswith("-") and not flag.startswith("--") for flag in flags)
    )


def _option_id(flags: list[str], command_path: list[str]) -> str:
    preferred = next((flag for flag in flags if flag.startswith("--")), flags[0])
    identifier = re.sub(r"[^a-z0-9]+", "_", preferred.lstrip("-+").casefold()).strip(
        "_"
    )
    if not identifier:
        identifier = hashlib.sha256("\0".join(flags).encode()).hexdigest()[:12]
    prefix = ".".join(command_path)
    return f"{prefix}:{identifier}" if prefix else identifier


def _case_discriminator(flags: list[str]) -> str:
    flag = next((value for value in flags if value.startswith("--")), flags[0])
    characters = [
        f"upper_{character.casefold()}" if character.isupper() else character
        for character in flag.lstrip("-+")
        if character.isalnum()
    ]
    return "_".join(characters) or hashlib.sha256(flag.encode()).hexdigest()[:8]


def _value_type(name: str, description: str) -> str:
    text = f"{name} {description}".casefold()
    if "port" in text:
        return "port"
    if any(word in text for word in ("timeout", "duration", "delay", "seconds")):
        return "duration"
    if any(word in text for word in ("file", "directory", "folder", "path")):
        return "path"
    if "url" in text or "uri" in text:
        return "url"
    if "domain" in text or "hostname" in text:
        return "domain"
    if any(word in text for word in ("integer", "number", "count", "threads", "rate")):
        return "integer"
    return "string"


def _option_value(
    signature: str, flags: list[str], description: str
) -> dict[str, Any] | None:
    remainder = signature
    for flag in flags:
        remainder = remainder.replace(flag, " ")
    remainder = remainder.strip(" ,/[]()")
    metavariable = ""
    angle = re.search(r"<([^>]+)>", signature)
    upper = re.search(r"(?:=|\s)([A-Z][A-Z0-9_.|/-]{1,80})(?:\s|$)", signature)
    if angle:
        metavariable = angle.group(1)
    elif upper:
        metavariable = upper.group(1)
    elif remainder and not re.fullmatch(r"[/,| -]+", remainder):
        token = remainder.split()[0].strip("=,:[]()")
        if token and not token.startswith("-") and len(token) <= 80:
            metavariable = token
    if not metavariable:
        return None
    optional = f"[{metavariable}]" in signature or f"[={metavariable}]" in signature
    style = "equals" if "=" in signature.split(maxsplit=1)[0] else "separate"
    return {
        "name": metavariable,
        "type": _value_type(metavariable, description),
        "required": not optional,
        "style": style,
    }


def _option_blocks(text: str, command_path: list[str]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    section = "options"
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if SECTION.fullmatch(stripped):
            section = stripped[:-1].casefold().replace(" ", "_")
            current = None
            continue
        if "example" in section or section in {"usage", "example_usage"}:
            continue
        if stripped.startswith(("-", "+")) and not set(stripped) <= {"-", "+", "="}:
            parts = re.split(r"\s{2,}", stripped, maxsplit=1)
            signature = parts[0]
            description = parts[1].strip() if len(parts) == 2 else ""
            flags = _expanded_flags(signature)
            if not flags:
                continue
            choices = (
                [[flag] for flag in flags]
                if _flags_are_distinct_choices(signature, flags)
                else [flags]
            )
            for choice in choices:
                current = {
                    "id": _option_id(choice, command_path),
                    "flags": choice,
                    "usage": signature,
                    "description": description or "See the exact-version help text.",
                    "section": section,
                    "value": _option_value(signature, choice, description),
                    "repeatable": "..." in signature
                    or "multiple" in description.casefold()
                    or "repeat" in description.casefold(),
                    "conflicts_with": [
                        _option_id([other], command_path)
                        for other in flags
                        if other not in choice
                    ],
                    "requires": [],
                    "implies": [],
                }
                options.append(current)
        elif (
            current is not None
            and stripped
            and not stripped.startswith(("usage:", "example:"))
        ):
            current["description"] = f"{current['description']} {stripped}"[:4096]
    merged: list[dict[str, Any]] = []
    for option in options:
        existing = next(
            (
                item
                for item in merged
                if set(item["flags"]).intersection(option["flags"])
            ),
            None,
        )
        if existing is None:
            merged.append(option)
            continue
        existing["flags"] = list(dict.fromkeys([*existing["flags"], *option["flags"]]))
        if option["description"] not in existing["description"]:
            existing["description"] = (
                f"{existing['description']} {option['description']}"[:4096]
            )
        existing["repeatable"] = existing["repeatable"] or option["repeatable"]
        if existing["value"] is None:
            existing["value"] = option["value"]
    return merged


def _exact_synopsis(documents: list[dict[str, Any]], fallback: str) -> str:
    for document in documents:
        lines = document["text"].splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.casefold().startswith("usage:"):
                continue
            usage = stripped.split(":", 1)[1].strip()
            continuation: list[str] = []
            for candidate in lines[index + 1 :]:
                value = candidate.strip()
                if not value or SECTION.fullmatch(value):
                    break
                if candidate[:1].isspace() and not value.startswith(("-", "+")):
                    continuation.append(value)
                else:
                    break
            resolved = " ".join([usage, *continuation]).strip()[:4096]
            if resolved:
                return resolved
    return fallback


def _inferred_positionals(synopsis: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for match in re.finditer(r"<([^<>]{1,100})>(\.\.\.)?", synopsis):
        raw_name = match.group(1).strip()
        if raw_name.casefold() in {"option", "options", "command", "args"}:
            continue
        identifier = re.sub(r"[^a-z0-9]+", "_", raw_name.casefold()).strip("_")
        if (
            not identifier
            or identifier in identifiers
            or not NAME.fullmatch(identifier)
        ):
            continue
        prefix = synopsis[max(0, match.start() - 1) : match.start()]
        suffix = synopsis[match.end() : match.end() + 1]
        optional = prefix == "[" or suffix == "]"
        identifiers.add(identifier)
        result.append(
            {
                "id": identifier,
                "name": raw_name,
                "type": _value_type(raw_name, synopsis),
                "required": not optional,
                "repeatable": bool(match.group(2)),
                "description": f"Exact-version positional operand <{raw_name}> from: {synopsis}",
            }
        )
    # Usage strings often express alternative branches in one line. The
    # structured request contract is linear, so once a branch makes an operand
    # optional, later inferred operands cannot safely be declared mandatory.
    # Likewise, only the final linear operand can be represented as repeatable.
    optional_seen = False
    for index, positional in enumerate(result):
        optional_seen = optional_seen or not positional["required"]
        if optional_seen:
            positional["required"] = False
        if positional["repeatable"] and index != len(result) - 1:
            positional["repeatable"] = False
    return result


def _parse_literal(value: str) -> str | int | float | bool:
    cleaned = value.strip().strip("'\"`.,;()[]")
    if cleaned.casefold() in {"true", "false"}:
        return cleaned.casefold() == "true"
    try:
        return int(cleaned)
    except ValueError:
        try:
            return float(cleaned)
        except ValueError:
            return cleaned


def _typed_literal(value: str, value_type: str) -> str | int | float | bool:
    parsed = _parse_literal(value)
    if value_type == "string" or value_type in {
        "path",
        "url",
        "domain",
        "target",
        "duration",
    }:
        return str(value).strip().strip("'\"`.,;()[]")
    return parsed


def _enrich_option_semantics(options: list[dict[str, Any]]) -> None:
    by_flag = {flag: option["id"] for option in options for flag in option["flags"]}
    for option in options:
        descriptor = option.get("value")
        signature = option["usage"]
        description = option["description"]
        if descriptor is not None:
            enum_match = re.search(
                r"(?:<|\{)([^<>\{\}]+[|,][^<>\{\}]+)(?:>|\})", signature
            )
            if enum_match:
                values = list(
                    dict.fromkeys(
                        _typed_literal(value, descriptor["type"])
                        for value in re.split(r"[|,]", enum_match.group(1))
                        if value.strip()
                    )
                )
                if values:
                    descriptor["enum"] = values
            default_match = re.search(
                r"\bdefault(?:s?\s+to|\s+is|:|=)?\s+([^\s,;)]+)",
                description,
                flags=re.IGNORECASE,
            )
            if default_match:
                descriptor["default"] = _parse_literal(default_match.group(1))
            range_match = re.search(
                r"\b(?:range|between)\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*(?:-|\.\.|to)\s*(-?\d+(?:\.\d+)?)",
                description,
                flags=re.IGNORECASE,
            )
            if range_match:
                descriptor["minimum"] = float(range_match.group(1))
                descriptor["maximum"] = float(range_match.group(2))
        mentioned = {
            by_flag[flag]
            for flag in _expanded_flags(description)
            if flag in by_flag and by_flag[flag] != option["id"]
        }
        lowered = description.casefold()
        if mentioned and any(
            phrase in lowered
            for phrase in (
                "conflict",
                "incompatible",
                "mutually exclusive",
                "cannot be used with",
                "not be used with",
            )
        ):
            option["conflicts_with"] = sorted(
                set(option["conflicts_with"]).union(mentioned)
            )
        if mentioned and any(
            phrase in lowered
            for phrase in ("requires", "required with", "only with", "depends on")
        ):
            option["requires"] = sorted(set(option["requires"]).union(mentioned))
        if mentioned and any(
            phrase in lowered
            for phrase in ("implies", "also enables", "automatically enables")
        ):
            option["implies"] = sorted(set(option["implies"]).union(mentioned))


def _rebind_choice_conflicts(options: list[dict[str, Any]]) -> None:
    """Bind slash-choice conflicts after case-sensitive IDs are finalized."""

    by_flag = {flag: option["id"] for option in options for flag in option["flags"]}
    for option in options:
        option["conflicts_with"] = []
        flags = _expanded_flags(option["usage"])
        if not _flags_are_distinct_choices(option["usage"], flags):
            continue
        option["conflicts_with"] = sorted(
            {
                by_flag[flag]
                for flag in flags
                if flag in by_flag and by_flag[flag] != option["id"]
            }
        )


def _documented_flags(text: str) -> set[str]:
    """Collect help-line switches independently from the structured parser.

    Coverage must compare two independently produced sets; counting the options
    that were already parsed would make the publication gate tautological.
    """

    documented: set[str] = set()
    section = "options"
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if SECTION.fullmatch(stripped):
            section = stripped[:-1].casefold().replace(" ", "_")
            continue
        if "example" in section or section in {"usage", "example_usage"}:
            continue
        if not stripped.startswith(("-", "+")) or set(stripped) <= {"-", "+", "="}:
            continue
        signature = re.split(r"\s{2,}", stripped, maxsplit=1)[0]
        documented.update(_expanded_flags(signature))
    return documented


def _help_document(
    argv: list[str], command_path: list[str], *, field: str
) -> dict[str, Any]:
    exit_code, text = _run(argv, field=field)
    if exit_code not in (0, 1, 2, 64, 128, 129, 255) or not text:
        raise CatalogBuildError(f"{field} returned unusable help ({exit_code})")
    return {
        "command_path": command_path,
        "argv": argv,
        "exit_code": exit_code,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def _discover_commands(kind: str, executable: str) -> list[tuple[list[str], list[str]]]:
    if kind == "git":
        # ``main`` is Git's user-facing command set. ``others`` also exposes
        # internal helpers such as sh-i18n--envsubst which have no command
        # interface or help document and must not be advertised to models.
        code, text = _run(
            [executable, "--list-cmds=main,nohelpers"],
            field="git.command_discovery",
        )
        if code != 0:
            raise CatalogBuildError("git command discovery failed")
        command_names = sorted(set(text.split()))
        return [
            ([name], [executable, name, "-h"])
            for name in command_names[:MAX_DISCOVERED_COMMANDS]
        ]
    if kind == "openssl":
        code, text = _run(
            [executable, "list", "-commands"], field="openssl.command_discovery"
        )
        if code != 0:
            raise CatalogBuildError("OpenSSL command discovery failed")
        command_names = sorted(set(text.split()))
        return [
            ([name], [executable, name, "-help"])
            for name in command_names[:MAX_DISCOVERED_COMMANDS]
        ]
    if kind == "help-subcommands":
        code, text = _run([executable, "--help"], field="subcommand discovery")
        if code not in (0, 1, 2):
            raise CatalogBuildError("subcommand discovery failed")
        names: set[str] = set()
        in_commands = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.casefold().startswith("commands:"):
                in_commands = True
                continue
            if in_commands and stripped and not line.startswith(" "):
                in_commands = False
            if in_commands:
                match = re.match(r"([a-z][a-z0-9_-]+)\s{2,}", stripped)
                if match and match.group(1) not in {"help"}:
                    names.add(match.group(1))
        return [([name], [executable, name, "--help"]) for name in sorted(names)]
    raise CatalogBuildError(f"unknown command discovery mode: {kind}")


def _apply_overrides(commands: list[dict[str, Any]], raw: Any, *, name: str) -> None:
    if not isinstance(raw, dict):
        raise CatalogBuildError(f"{name}.option_overrides must be an object")
    index = {
        option["id"]: option for command in commands for option in command["options"]
    }
    for identifier, changes in raw.items():
        if identifier not in index or not isinstance(changes, dict):
            raise CatalogBuildError(
                f"{name} override references unknown option {identifier}"
            )
        allowed = {
            "description",
            "repeatable",
            "conflicts_with",
            "requires",
            "implies",
            "value",
        }
        if set(changes) - allowed:
            raise CatalogBuildError(
                f"{name} override {identifier} has unsupported fields"
            )
        index[identifier].update(changes)


def _build_tool(path: Path, versions: dict[str, str]) -> dict[str, Any]:
    raw = _load_yaml(path)
    required = {
        "protocol",
        "name",
        "version_key",
        "executable",
        "category",
        "risk_class",
        "description",
        "homepage",
        "synopsis",
        "version_probe",
        "version_exit_codes",
        "documentation",
        "positionals",
        "examples",
        "notes",
        "option_overrides",
    }
    optional = {"package_version_key", "command_discovery"}
    if set(raw) - required - optional or required - set(raw):
        raise CatalogBuildError(f"{path.name} has missing or unknown top-level fields")
    if raw["protocol"] != SOURCE_PROTOCOL:
        raise CatalogBuildError(f"{path.name} has an unsupported source protocol")
    name = _string(raw["name"], field=f"{path.name}.name", maximum=128)
    if not NAME.fullmatch(name) or path.stem != name:
        raise CatalogBuildError(f"interface file and tool name differ: {path.name}")
    version_key = _string(raw["version_key"], field=f"{name}.version_key")
    if version_key not in versions:
        raise CatalogBuildError(f"{name} references unknown {version_key}")
    version = versions[version_key]
    executable = Path(_string(raw["executable"], field=f"{name}.executable"))
    if not executable.is_absolute() or ".." in executable.parts:
        raise CatalogBuildError(f"{name}.executable must be absolute")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise CatalogBuildError(f"{name}.executable is not installed: {executable}")
    risk_class = _string(raw["risk_class"], field=f"{name}.risk_class")
    if risk_class not in RISK_CLASSES:
        raise CatalogBuildError(f"{name} has invalid risk class {risk_class}")
    version_probe = _argv(raw["version_probe"], field=f"{name}.version_probe")
    allowed_codes = raw["version_exit_codes"]
    if (
        not isinstance(allowed_codes, list)
        or not allowed_codes
        or any(
            not isinstance(code, int) or code < 0 or code > 255
            for code in allowed_codes
        )
    ):
        raise CatalogBuildError(f"{name}.version_exit_codes is invalid")
    version_code, version_output = _run(version_probe, field=f"{name}.version_probe")
    if version_code not in allowed_codes or version not in version_output:
        raise CatalogBuildError(
            f"{name} reports a different version; expected literal {version!r}; "
            f"exit={version_code}; output={version_output[:300]!r}"
        )
    raw_docs = raw["documentation"]
    if not isinstance(raw_docs, list) or not raw_docs:
        raise CatalogBuildError(f"{name}.documentation must be a non-empty array")
    documents: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for index, item in enumerate(raw_docs):
        if not isinstance(item, dict) or set(item) != {"command_path", "argv"}:
            raise CatalogBuildError(f"{name}.documentation[{index}] is invalid")
        command_path = item["command_path"]
        if not isinstance(command_path, list) or any(
            not isinstance(part, str) or not NAME.fullmatch(part)
            for part in command_path
        ):
            raise CatalogBuildError(
                f"{name}.documentation[{index}].command_path is invalid"
            )
        argv = _argv(item["argv"], field=f"{name}.documentation[{index}].argv")
        documents.append(
            _help_document(argv, command_path, field=f"{name}.documentation[{index}]")
        )
        seen_paths.add(tuple(command_path))
    if discovery := raw.get("command_discovery"):
        for command_path, argv in _discover_commands(
            str(discovery), executable.as_posix()
        ):
            if tuple(command_path) in seen_paths:
                continue
            documents.append(
                _help_document(
                    argv,
                    command_path,
                    field=f"{name}.discovered.{'.'.join(command_path)}",
                )
            )
            seen_paths.add(tuple(command_path))
    commands: list[dict[str, Any]] = []
    root_positionals = _positionals(raw["positionals"], field=f"{name}.positionals")
    for command_path in sorted(seen_paths):
        command_docs = [
            doc for doc in documents if tuple(doc["command_path"]) == command_path
        ]
        options: list[dict[str, Any]] = []
        for document in command_docs:
            options.extend(_option_blocks(document["text"], list(command_path)))
        deduplicated: list[dict[str, Any]] = []
        for option in options:
            existing = next(
                (
                    item
                    for item in deduplicated
                    if set(item["flags"]).intersection(option["flags"])
                ),
                None,
            )
            if existing is None:
                deduplicated.append(option)
            else:
                existing["flags"] = list(
                    dict.fromkeys([*existing["flags"], *option["flags"]])
                )
                if option["description"] not in existing["description"]:
                    existing["description"] = (
                        f"{existing['description']} {option['description']}"[:4096]
                    )
        id_groups: dict[str, list[dict[str, Any]]] = {}
        for option in deduplicated:
            id_groups.setdefault(option["id"], []).append(option)
        for identifier, colliding in id_groups.items():
            if len(colliding) < 2:
                continue
            discriminated = [
                f"{identifier}__{_case_discriminator(option['flags'])}"
                for option in colliding
            ]
            duplicate_discriminators = {
                value for value in discriminated if discriminated.count(value) > 1
            }
            rewritten = [
                (
                    f"{value}__"
                    f"{hashlib.sha256(chr(0).join(option['flags']).encode()).hexdigest()[:8]}"
                    if value in duplicate_discriminators
                    else value
                )
                for value, option in zip(discriminated, colliding, strict=True)
            ]
            if len(rewritten) != len(set(rewritten)):
                raise CatalogBuildError(f"{name} has ambiguous option id {identifier}")
            for option, replacement in zip(colliding, rewritten, strict=True):
                option["id"] = replacement
        _rebind_choice_conflicts(deduplicated)
        _enrich_option_semantics(deduplicated)
        fallback_synopsis = (
            raw["synopsis"]
            if not command_path
            else " ".join([name, *command_path, "[options]"])
        )
        synopsis = _exact_synopsis(command_docs, fallback_synopsis)
        commands.append(
            {
                "path": list(command_path),
                "synopsis": synopsis,
                "positionals": (
                    root_positionals
                    if not command_path
                    else _inferred_positionals(synopsis)
                ),
                "options": sorted(deduplicated, key=lambda option: option["id"]),
                "help_documents": command_docs,
            }
        )
    _apply_overrides(commands, raw["option_overrides"], name=name)
    documented_flags = {
        flag for document in documents for flag in _documented_flags(document["text"])
    }
    structured_flags = {
        flag
        for command in commands
        for option in command["options"]
        for flag in option["flags"]
    }
    unmapped_flags = sorted(documented_flags - structured_flags)
    if unmapped_flags:
        raise CatalogBuildError(
            f"{name} has documented switches without structured definitions: "
            f"{unmapped_flags}"
        )
    result: dict[str, Any] = {
        "protocol": INTERFACE_PROTOCOL,
        "name": name,
        "version": version,
        "executable": executable.as_posix(),
        "aliases": [executable.name] if executable.name != name else [],
        "category": _string(raw["category"], field=f"{name}.category", maximum=64),
        "risk_class": risk_class,
        "description": _string(raw["description"], field=f"{name}.description"),
        "homepage": _string(raw["homepage"], field=f"{name}.homepage"),
        "synopsis": _string(raw["synopsis"], field=f"{name}.synopsis"),
        "examples": _examples(raw["examples"], name=name),
        "notes": _notes(raw["notes"], name=name),
        "commands": commands,
        "coverage": {
            "help_documents": len(documents),
            "documented_options": len(documented_flags),
            "structured_options": len(structured_flags),
            "unmapped_options": unmapped_flags,
            "complete": True,
        },
    }
    if package_key := raw.get("package_version_key"):
        if not isinstance(package_key, str) or package_key not in versions:
            raise CatalogBuildError(f"{name} has an unknown package version key")
        result["package_version"] = versions[package_key]
    return result


def _inventory(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalogued = {Path(tool["executable"]).resolve(): tool["name"] for tool in tools}
    result: dict[Path, dict[str, Any]] = {}
    for directory_name in TRUSTED_PATHS:
        directory = Path(directory_name)
        if not directory.is_dir():
            continue
        for candidate in sorted(
            directory.iterdir(), key=lambda item: item.name.casefold()
        ):
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            if candidate.name in INTERNAL_EXECUTABLES:
                continue
            existing = result.get(resolved)
            if existing is None:
                result[resolved] = {
                    "name": candidate.name,
                    "path": candidate.as_posix(),
                    "catalogued": resolved in catalogued,
                    "interface": catalogued.get(resolved),
                    "aliases": [],
                }
            elif (
                candidate.name != existing["name"]
                and candidate.name not in existing["aliases"]
            ):
                existing["aliases"].append(candidate.name)
    return sorted(result.values(), key=lambda item: (item["name"], item["path"]))


def build(interfaces: Path, versions_path: Path, schema_path: Path) -> dict[str, Any]:
    if not interfaces.is_dir():
        raise CatalogBuildError("interfaces must be a directory")
    versions = _load_versions(versions_path)
    schema = _load_json(schema_path)
    if schema.get("$id") != "https://nebula.security/schemas/toolbox-catalog-v2.json":
        raise CatalogBuildError("unexpected Toolbox catalog schema")
    paths = sorted(interfaces.glob("*.yaml"))
    if not paths:
        raise CatalogBuildError("no Toolbox interface sources were found")
    tools = [_build_tool(path, versions) for path in paths]
    names = [tool["name"] for tool in tools]
    if len(names) != len(set(names)):
        raise CatalogBuildError("catalog contains duplicate tool names")
    payload = {
        "protocol": CATALOG_PROTOCOL,
        "interface_protocol": INTERFACE_PROTOCOL,
        "toolbox_version": versions["TOOLBOX_VERSION"],
        "tools": sorted(tools, key=lambda tool: tool["name"]),
        "inventory": _inventory(tools),
    }
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(component) for component in error.absolute_path)
        raise CatalogBuildError(
            "generated catalog violates its schema at "
            f"{location or '<root>'}: {error.message}"
        )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interfaces", type=Path, required=True)
    parser.add_argument("--versions", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    options = _parser().parse_args()
    payload = build(options.interfaces, options.versions, options.schema)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_CATALOG_BYTES:
        raise CatalogBuildError("generated catalog is too large")
    options.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=options.output.parent, delete=False) as handle:
        handle.write(encoded)
        temporary = Path(handle.name)
    temporary.chmod(0o444)
    temporary.replace(options.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
