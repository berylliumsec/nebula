#!/usr/bin/env python3
"""Toolbox adapter: native action streams plus bounded catalog JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PROTOCOL = "nebula.toolbox/v1"
CATALOG_PROTOCOL = "nebula.toolbox.catalog/v2"
INTERFACE_PROTOCOL = "nebula.toolbox.interface/v2"
CATALOG_PATH = Path("/opt/nebula/tool-catalog.json")
WORKSPACE = Path("/workspace")
MAX_OUTPUT_BYTES = 2_000_000
# Core captures at most 2,000,000 bytes from each sandbox stream.  Keep the
# complete JSON wire envelope below that boundary even when quoting arbitrary
# tool output expands it (for example, control characters become ``\u0000``).
MAX_ENVELOPE_BYTES = 1_900_000
RISK_LEVEL = {
    "local_read": 0,
    "workspace_write": 1,
    "passive": 2,
    "active_scan": 3,
    "credential_use": 4,
    "exploitation": 4,
    "persistence": 5,
    "destructive": 6,
}


def _catalog_payload() -> dict[str, Any]:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if (
        payload.get("protocol") != CATALOG_PROTOCOL
        or payload.get("interface_protocol") != INTERFACE_PROTOCOL
        or not isinstance(payload.get("tools"), list)
        or not isinstance(payload.get("inventory"), list)
    ):
        raise ValueError("the Toolbox interface catalog is invalid")
    return payload


def _load_catalog() -> dict[str, dict[str, Any]]:
    payload = _catalog_payload()
    result: dict[str, dict[str, Any]] = {}
    for item in payload["tools"]:
        if (
            not isinstance(item, dict)
            or item.get("protocol") != INTERFACE_PROTOCOL
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("version"), str)
            or not isinstance(item.get("commands"), list)
        ):
            raise ValueError("the Toolbox interface catalog contains an invalid entry")
        executable = Path(str(item.get("executable", "")))
        if not executable.is_absolute() or ".." in executable.parts:
            raise ValueError("Toolbox executables must be absolute")
        if item["name"] in result:
            raise ValueError("the Toolbox interface catalog contains a duplicate tool")
        result[item["name"]] = item
    return result


def _catalog_digest() -> str:
    return hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest()


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "name",
        "version",
        "package_version",
        "executable",
        "aliases",
        "category",
        "risk_class",
        "description",
        "homepage",
        "synopsis",
        "examples",
        "notes",
        "coverage",
    )
    result = {field: item[field] for field in fields if field in item}
    result["catalogued"] = True
    result["command_paths"] = [command["path"] for command in item["commands"]]
    return result


def _command(item: dict[str, Any], command_path: list[str]) -> dict[str, Any]:
    for command in item["commands"]:
        if command.get("path") == command_path:
            return command
    raise ValueError(
        f"{item['name']} has no catalogued command path: {' '.join(command_path)}"
    )


def _help_text(item: dict[str, Any], command: dict[str, Any]) -> str:
    heading = json.dumps(
        {
            "name": item["name"],
            "version": item["version"],
            "command_path": command["path"],
            "synopsis": command["synopsis"],
            "positionals": command["positionals"],
            "options": command["options"],
            "examples": item["examples"],
            "notes": item["notes"],
        },
        indent=2,
        sort_keys=True,
    )
    documents = "\n\n".join(
        f"$ {' '.join(document['argv'])}\n{document['text']}"
        for document in command.get("help_documents", [])
    )
    return f"{heading}\n\nExact-version help evidence:\n{documents}".rstrip()


def _cwd(value: str) -> Path:
    candidate = (
        (WORKSPACE / value).resolve()
        if not value.startswith("/")
        else Path(value).resolve()
    )
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise ValueError("working directory must remain inside /workspace")
    if not candidate.is_dir():
        raise ValueError("working directory does not exist")
    return candidate


def _decode_array(value: str, *, field: str) -> list[Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError(f"{field} must be a JSON array")
    return decoded


def _command_path(value: str | None) -> list[str]:
    if value is None:
        return []
    decoded = _decode_array(value, field="command-path-json")
    if any(
        not isinstance(item, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", item)
        for item in decoded
    ):
        raise ValueError("command-path-json contains an invalid component")
    return decoded


def _scalar(value: Any, descriptor: dict[str, Any], *, field: str) -> str:
    value_type = descriptor.get("type", "string")
    if value_type in {"integer", "port"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} must be an integer")
        minimum = 1 if value_type == "port" else descriptor.get("minimum")
        maximum = 65535 if value_type == "port" else descriptor.get("maximum")
        if minimum is not None and value < minimum:
            raise ValueError(f"{field} is below its minimum")
        if maximum is not None and value > maximum:
            raise ValueError(f"{field} exceeds its maximum")
    elif value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be numeric")
        if descriptor.get("minimum") is not None and value < descriptor["minimum"]:
            raise ValueError(f"{field} is below its minimum")
        if descriptor.get("maximum") is not None and value > descriptor["maximum"]:
            raise ValueError(f"{field} exceeds its maximum")
    elif value_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be boolean")
    elif not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if "enum" in descriptor and value not in descriptor["enum"]:
        raise ValueError(f"{field} is not an allowed value")
    if pattern := descriptor.get("pattern"):
        if re.fullmatch(pattern, str(value)) is None:
            raise ValueError(f"{field} does not match its required pattern")
    return str(value).lower() if isinstance(value, bool) else str(value)


def _render_option(option: dict[str, Any], value: Any) -> list[str]:
    flag = option["flags"][0]
    descriptor = option.get("value")
    if descriptor is None:
        if value not in (None, True):
            raise ValueError(f"{option['id']} is a switch and accepts no value")
        return [flag]
    if value is None:
        if descriptor.get("required", True):
            raise ValueError(f"{option['id']} requires a value")
        return [flag]
    values = value if isinstance(value, list) else [value]
    if len(values) > 1 and not option.get("repeatable"):
        raise ValueError(f"{option['id']} is not repeatable")
    rendered: list[str] = []
    for raw in values:
        item = _scalar(raw, descriptor, field=option["id"])
        style = descriptor.get("style", "separate")
        if style == "equals":
            rendered.append(f"{flag}={item}")
        elif style == "attached":
            rendered.append(f"{flag}{item}")
        else:
            rendered.extend([flag, item])
    return rendered


def _structured_arguments(
    item: dict[str, Any], invocation_json: str, target: str | None
) -> list[str]:
    invocation = json.loads(invocation_json)
    if not isinstance(invocation, dict) or set(invocation) != {
        "command_path",
        "options",
        "positionals",
    }:
        raise ValueError(
            "invocation-json must contain exactly command_path, options, and positionals"
        )
    command_path = invocation["command_path"]
    if not isinstance(command_path, list) or any(
        not isinstance(value, str) for value in command_path
    ):
        raise ValueError("command_path must be an array of strings")
    command = _command(item, command_path)
    options = invocation["options"]
    positionals = invocation["positionals"]
    if not isinstance(options, list) or not isinstance(positionals, list):
        raise ValueError("options and positionals must be arrays")
    option_index = {option["id"]: option for option in command["options"]}
    selected: dict[str, Any] = {}
    argv: list[str] = [*command_path]
    for index, supplied in enumerate(options):
        if not isinstance(supplied, dict) or set(supplied) != {"id", "value"}:
            raise ValueError(f"options[{index}] must contain exactly id and value")
        identifier = supplied["id"]
        if identifier not in option_index:
            raise ValueError(f"unknown option for {item['name']}: {identifier}")
        if identifier in selected and not option_index[identifier].get("repeatable"):
            raise ValueError(f"duplicate non-repeatable option: {identifier}")
        selected[identifier] = supplied["value"]
        argv.extend(_render_option(option_index[identifier], supplied["value"]))
    for identifier in selected:
        option = option_index[identifier]
        conflicts = set(option.get("conflicts_with", [])).intersection(selected)
        missing = set(option.get("requires", [])).difference(selected)
        if conflicts:
            raise ValueError(f"{identifier} conflicts with {sorted(conflicts)}")
        if missing:
            raise ValueError(f"{identifier} requires {sorted(missing)}")
    positional_index = {value["id"]: value for value in command["positionals"]}
    supplied_positionals: dict[str, Any] = {}
    for index, supplied in enumerate(positionals):
        if not isinstance(supplied, dict) or set(supplied) != {"id", "value"}:
            raise ValueError(f"positionals[{index}] must contain exactly id and value")
        identifier = supplied["id"]
        if identifier not in positional_index or identifier in supplied_positionals:
            raise ValueError(f"unknown or duplicate positional: {identifier}")
        supplied_positionals[identifier] = supplied["value"]
    for descriptor in command["positionals"]:
        identifier = descriptor["id"]
        if identifier not in supplied_positionals:
            if descriptor["required"]:
                raise ValueError(f"missing required positional: {identifier}")
            continue
        raw = supplied_positionals[identifier]
        values = raw if isinstance(raw, list) else [raw]
        if len(values) > 1 and not descriptor["repeatable"]:
            raise ValueError(f"{identifier} is not repeatable")
        argv.extend(_scalar(value, descriptor, field=identifier) for value in values)
    if target is None and any("{target}" in argument for argument in argv):
        raise ValueError("structured invocation uses {target} without a target")
    return [argument.replace("{target}", target or "") for argument in argv]


def _bounded(data: bytes) -> str:
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")


def _metadata(*, guidance: bool, script: str | None = None) -> dict[str, Any]:
    return {
        "catalog_digest": _catalog_digest(),
        "catalogued_guidance": guidance,
        "script_sha256": hashlib.sha256(script.encode()).hexdigest()
        if script is not None
        else None,
    }


def _envelope(
    operation: str,
    *,
    tool: str | None = None,
    command: list[str] | None = None,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    matches: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "operation": operation,
        "tool": tool,
        "command": command or [],
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "matches": matches or [],
        "metadata": metadata or _metadata(guidance=False),
    }


def _serialized_output(output: dict[str, Any]) -> str:
    """Return one complete JSON envelope within Core's stdout capture limit."""

    def encode(value: dict[str, Any]) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    serialized = encode(output)
    if len(serialized.encode("utf-8")) <= MAX_ENVELOPE_BYTES:
        return serialized

    # Execution output is observation data, so bounded prefixes are preferable
    # to a transport-truncated document that cannot be parsed at all. Search and
    # help matches can also be large; their human-readable content is already in
    # stdout and they are discarded only on this exceptional path.
    reduced = {**output, "matches": []}
    stdout = str(output.get("stdout", ""))
    stderr = str(output.get("stderr", ""))
    marker = "\n[nebula-toolbox: output truncated to preserve JSON envelope]"

    def with_prefix(limit: int) -> dict[str, Any]:
        return {
            **reduced,
            "stdout": stdout[:limit],
            "stderr": stderr[:limit] + marker,
        }

    low = 0
    high = max(len(stdout), len(stderr))
    best = with_prefix(0)
    while low <= high:
        middle = (low + high) // 2
        candidate = with_prefix(middle)
        if len(encode(candidate).encode("utf-8")) <= MAX_ENVELOPE_BYTES:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    serialized = encode(best)
    if len(serialized.encode("utf-8")) <= MAX_ENVELOPE_BYTES:
        return serialized

    # A future schema change could make non-output fields unexpectedly huge.
    # Still honor the wire contract with a small, schema-compatible envelope.
    return encode(
        _envelope(
            "error",
            exit_code=2,
            stderr="Toolbox result exceeded the JSON envelope limit",
            metadata=output.get("metadata"),
        )
    )


def _execute(
    operation: str,
    command: list[str],
    *,
    tool: str | None,
    cwd: Path,
    timeout: int,
    target: str | None,
    ports: str | None,
    guidance: bool,
    script: str | None = None,
) -> int:
    """Relay native child streams and status; Core owns artifact capture."""
    environment = {
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/opt/nebula/venv/bin:/opt/nebula/nmap/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
        "NEBULA_TARGET": target or "",
        "NEBULA_PORTS": ports or "[]",
        "NEBULA_OUTPUT_DIR": os.getenv("NEBULA_OUTPUT_DIR", "/tmp"),
        "NUCLEI_TEMPLATES": "/opt/nebula/nuclei-templates",
        "SEMGREP_ENABLE_VERSION_CHECK": "0",
        "SEMGREP_SEND_METRICS": "off",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return int(completed.returncode)
    except (
        subprocess.TimeoutExpired
    ):  # diagnostic-expected: exit 124 is the wrapper timeout contract
        # Child output was already relayed. Core records this status and the
        # partial streams even though the adapter-level deadline fired.
        print(
            f"nebula-toolbox: {tool or operation} timed out after {timeout}s",
            file=sys.stderr,
        )
        return 124


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nebula-toolbox")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    help_parser = subparsers.add_parser("help")
    help_parser.add_argument("--tool", required=True)
    help_parser.add_argument("--command-path-json")
    command = subparsers.add_parser("exec")
    command.add_argument("--cwd", default="/workspace")
    command.add_argument("--timeout", type=int, default=300)
    command.add_argument("--target")
    command.add_argument("--ports-json")
    command.add_argument("--tool", required=True)
    command.add_argument("--invocation-json", required=True)
    command.add_argument("--max-risk", choices=sorted(RISK_LEVEL), required=True)
    shell = subparsers.add_parser("shell")
    shell.add_argument("--cwd", default="/workspace")
    shell.add_argument("--timeout", type=int, default=300)
    shell.add_argument("--target")
    shell.add_argument("--ports-json")
    shell.add_argument("--script", required=True)
    code = subparsers.add_parser("code")
    code.add_argument("--language", choices=("bash", "sh", "python"), required=True)
    return parser


def _execute_code(language: str) -> int:
    """Run reviewed UTF-8 source with fixed argv and no interactive stdin."""

    source = sys.stdin.buffer.read(200_001)
    if not source:
        raise ValueError("code source cannot be empty")
    if len(source) > 200_000:
        raise ValueError("code source exceeds 200000 bytes")
    source.decode("utf-8", errors="strict")
    suffix = ".py" if language == "python" else ".sh"
    descriptor, name = tempfile.mkstemp(prefix="nebula-code-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o400)
        commands = {
            "bash": [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-eu",
                "-o",
                "pipefail",
                str(path),
            ],
            "sh": [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "--posix",
                "-eu",
                "-o",
                "pipefail",
                str(path),
            ],
            "python": [
                "/opt/nebula/venv/bin/python",
                "-E",
                "-s",
                "-u",
                str(path),
            ],
        }
        environment = {
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/opt/nebula/venv/bin:/opt/nebula/nmap/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
            "NEBULA_TARGET": os.getenv("NEBULA_TARGET", ""),
            "NEBULA_PORTS": os.getenv("NEBULA_PORTS", "[]"),
            "NEBULA_OUTPUT_DIR": os.getenv("NEBULA_OUTPUT_DIR", "/tmp"),
            "NUCLEI_TEMPLATES": "/opt/nebula/nuclei-templates",
            "SEMGREP_ENABLE_VERSION_CHECK": "0",
            "SEMGREP_SEND_METRICS": "off",
        }
        completed = subprocess.run(
            commands[language],
            cwd="/workspace",
            env=environment,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return int(completed.returncode)
    finally:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    native_action = False
    try:
        options = _parser().parse_args(argv)
        if options.operation == "code":
            return _execute_code(options.language)
        index = _load_catalog()
        if options.operation == "search":
            query = options.query.casefold()
            matches = [
                _summary(item)
                for item in index.values()
                if query
                in " ".join(
                    str(item.get(field, ""))
                    for field in ("name", "category", "description", "version")
                ).casefold()
            ]
            if not matches:
                matches = [
                    {**item, "catalogued": False}
                    for item in _catalog_payload()["inventory"]
                    if query
                    in f"{item.get('name', '')} {item.get('path', '')}".casefold()
                ]
            output = _envelope(
                "search",
                matches=matches,
                metadata=_metadata(
                    guidance=bool(matches and matches[0].get("catalogued"))
                ),
            )
        elif options.operation == "help":
            item = index.get(options.tool)
            if item is None:
                raise ValueError(f"unknown catalogued Toolbox tool: {options.tool}")
            selected_command = _command(item, _command_path(options.command_path_json))
            descriptor = {**_summary(item), "selected_command": selected_command}
            output = _envelope(
                "help",
                tool=options.tool,
                stdout=_help_text(item, selected_command),
                matches=[descriptor],
                metadata=_metadata(guidance=True),
            )
        else:
            native_action = True
            if options.timeout < 1 or options.timeout > 3600:
                raise ValueError("timeout must be between 1 and 3600 seconds")
            cwd = _cwd(options.cwd)
            if options.operation == "exec":
                item = index.get(options.tool)
                if item is None:
                    raise ValueError(f"unknown catalogued Toolbox tool: {options.tool}")
                declared_risk = item.get("risk_class")
                if declared_risk not in RISK_LEVEL:
                    raise ValueError(
                        f"Toolbox tool has an invalid risk class: {options.tool}"
                    )
                if RISK_LEVEL[declared_risk] > RISK_LEVEL[options.max_risk]:
                    raise ValueError(
                        f"{options.tool} requires {declared_risk}, above this capability's {options.max_risk} limit"
                    )
                argv = [
                    item["executable"],
                    *_structured_arguments(
                        item, options.invocation_json, options.target
                    ),
                ]
                tool = options.tool
                guidance = True
                script = None
            else:
                if "\x00" in options.script:
                    raise ValueError("script cannot contain NUL bytes")
                argv = [
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-eu",
                    "-o",
                    "pipefail",
                    "-c",
                    options.script,
                ]
                tool = "shell"
                guidance = False
                script = options.script
            return _execute(
                options.operation,
                argv,
                tool=tool,
                cwd=cwd,
                timeout=options.timeout,
                target=options.target,
                ports=options.ports_json,
                guidance=guidance,
                script=script,
            )
    except Exception as exc:
        if native_action:
            print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
            return 2
        # diagnostic-expected: this isolated adapter returns a bounded failure
        # envelope; Core records it without protocol payloads or tool output.
        try:
            metadata = _metadata(guidance=False)
        except Exception:
            # diagnostic-expected: safe empty metadata is the fail-closed fallback.
            metadata = {
                "catalog_digest": None,
                "catalogued_guidance": False,
                "script_sha256": None,
            }
        output = _envelope(
            "error",
            exit_code=2,
            stderr=f"{exc.__class__.__name__}: {exc}",
            metadata=metadata,
        )
    sys.stdout.write(_serialized_output(output) + "\n")
    return int(output["exit_code"]) if 0 <= int(output["exit_code"]) <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
