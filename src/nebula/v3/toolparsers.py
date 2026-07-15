"""Bounded built-in parsers and the isolated parser-container wire contract."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import json
import os
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .sandbox import (
    SandboxExecutionKind,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    SandboxRunner,
    SandboxWorkspaceAccess,
)
from .tools import _is_digest_pinned_image


MAX_PARSER_INPUT_BYTES = 100_000_000


class ToolOutputParseError(ValueError):
    """Tool output is malformed or violates its declared parser contract."""


def _bounded_text(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_PARSER_INPUT_BYTES:
        raise ToolOutputParseError("tool output exceeds the parser input limit")
    return value


def parse_json(
    stdout: str, stderr: str = "", exit_code: int | None = None
) -> dict[str, Any]:
    del stderr, exit_code
    try:
        payload = json.loads(_bounded_text(stdout))
    except (json.JSONDecodeError, UnicodeError) as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.toolparsers.caught_failure_001",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="toolparsers",
        )
        raise ToolOutputParseError("tool output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ToolOutputParseError("JSON tool output must contain one object")
    return payload


def _jsonl_records(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(_bounded_text(stdout).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.toolparsers.caught_failure_002",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="toolparsers",
            )
            raise ToolOutputParseError(
                f"invalid JSONL record on line {number}"
            ) from exc
        if not isinstance(record, dict):
            raise ToolOutputParseError(
                f"JSONL record on line {number} is not an object"
            )
        records.append(record)
    return records


def parse_jsonl(
    stdout: str, stderr: str = "", exit_code: int | None = None
) -> dict[str, Any]:
    del stderr, exit_code
    return {"records": _jsonl_records(stdout)}


def parse_sarif(
    stdout: str, stderr: str = "", exit_code: int | None = None
) -> dict[str, Any]:
    payload = parse_json(stdout, stderr, exit_code)
    if not isinstance(payload.get("version"), str) or not isinstance(
        payload.get("runs"), list
    ):
        raise ToolOutputParseError("SARIF output requires version and runs")
    return payload


def parse_nmap_xml(
    stdout: str, stderr: str = "", exit_code: int | None = None
) -> dict[str, Any]:
    del stderr, exit_code
    source = _bounded_text(stdout)
    lowered = source.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ToolOutputParseError("Nmap XML cannot contain DTD or entity declarations")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.toolparsers.caught_failure_003",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="toolparsers",
        )
        raise ToolOutputParseError("tool output is not valid Nmap XML") from exc
    if root.tag != "nmaprun":
        raise ToolOutputParseError("Nmap XML root must be nmaprun")

    hosts: list[dict[str, Any]] = []
    for host in root.findall("host"):
        status = host.find("status")
        addresses = [
            {
                "address": item.attrib.get("addr", ""),
                "type": item.attrib.get("addrtype", ""),
            }
            for item in host.findall("address")
        ]
        hostnames = [
            item.attrib.get("name", "")
            for item in host.findall("./hostnames/hostname")
            if item.attrib.get("name")
        ]
        ports: list[dict[str, Any]] = []
        for port in host.findall("./ports/port"):
            state = port.find("state")
            service = port.find("service")
            try:
                port_number = int(port.attrib["portid"])
            except (KeyError, TypeError, ValueError) as exc:
                record_caught_exception(
                    "toolbox",
                    "toolbox.toolparsers.caught_failure_004",
                    "A handled toolbox operation raised an exception.",
                    exc,
                    stage="toolparsers",
                )
                raise ToolOutputParseError(
                    "Nmap XML contains an invalid port number"
                ) from exc
            if not 1 <= port_number <= 65_535:
                raise ToolOutputParseError(
                    "Nmap XML port number is outside the valid range"
                )
            ports.append(
                {
                    "protocol": port.attrib.get("protocol", ""),
                    "port": port_number,
                    "state": state.attrib.get("state", "") if state is not None else "",
                    "service": dict(service.attrib) if service is not None else {},
                }
            )
        hosts.append(
            {
                "status": status.attrib.get("state", "") if status is not None else "",
                "addresses": addresses,
                "hostnames": hostnames,
                "ports": ports,
            }
        )
    finished = root.find("./runstats/finished")
    return {
        "scanner": root.attrib.get("scanner", "nmap"),
        "arguments": root.attrib.get("args", ""),
        "hosts": hosts,
        "summary": dict(finished.attrib) if finished is not None else {},
    }


def parse_nuclei_jsonl(
    stdout: str, stderr: str = "", exit_code: int | None = None
) -> dict[str, Any]:
    del stderr, exit_code
    return {"findings": _jsonl_records(stdout)}


def parse_nikto(
    stdout: str, stderr: str = "", exit_code: int | None = None
) -> dict[str, Any]:
    del stderr, exit_code
    try:
        payload = json.loads(_bounded_text(stdout))
    except json.JSONDecodeError as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.toolparsers.caught_failure_005",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="toolparsers",
        )
        raise ToolOutputParseError("Nikto output is not valid JSON") from exc
    if isinstance(payload, list):
        if any(not isinstance(item, dict) for item in payload):
            raise ToolOutputParseError("Nikto findings must be objects")
        return {"vulnerabilities": payload}
    if not isinstance(payload, dict):
        raise ToolOutputParseError("Nikto output must be an object or object array")
    vulnerabilities = payload.get("vulnerabilities", [])
    if not isinstance(vulnerabilities, list) or any(
        not isinstance(item, dict) for item in vulnerabilities
    ):
        raise ToolOutputParseError("Nikto vulnerabilities must be an object array")
    return {"vulnerabilities": vulnerabilities}


BuiltinParser = Callable[[str, str, int | None], dict[str, Any]]

BUILTIN_PARSERS: dict[str, BuiltinParser] = {
    "json/v1": parse_json,
    "jsonl/v1": parse_jsonl,
    "sarif/v2.1": parse_sarif,
    "nmap.xml/v1": parse_nmap_xml,
    "nuclei.jsonl/v1": parse_nuclei_jsonl,
    "nikto.json/v1": parse_nikto,
}


def parse_tool_output(
    parser: str, stdout: str, stderr: str = "", exit_code: int | None = None
) -> dict[str, Any]:
    try:
        implementation = BUILTIN_PARSERS[parser]
    except KeyError as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.toolparsers.caught_failure_006",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="toolparsers",
        )
        raise ToolOutputParseError(f"unknown built-in parser: {parser}") from exc
    return implementation(stdout, stderr, exit_code)


class ParserContainerContract(BaseModel):
    """Wire contract for an untrusted parser executed outside Nebula Core."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["nebula.parser/v1"] = "nebula.parser/v1"
    image: str
    executable: str
    output_schema: dict[str, Any]
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    memory_mb: int = Field(default=128, ge=32, le=1024)
    input_path: str = "/workspace/tool-output"
    output_path: Literal["-"] = "-"

    @field_validator("image")
    @classmethod
    def image_is_immutable(cls, value: str) -> str:
        if not _is_digest_pinned_image(value):
            raise ValueError("parser image must be pinned by SHA-256 without a tag")
        return value

    @field_validator("executable", "input_path")
    @classmethod
    def paths_are_absolute(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("parser contract paths must be absolute container paths")
        if Path(value).name.lower() in {
            "sh",
            "bash",
            "dash",
            "zsh",
            "fish",
            "cmd",
            "powershell",
            "pwsh",
        }:
            raise ValueError("shell interpreters cannot be parser executables")
        return value

    @model_validator(mode="after")
    def output_is_one_strict_object(self) -> "ParserContainerContract":
        try:
            Draft202012Validator.check_schema(self.output_schema)
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.toolparsers.caught_failure_007",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="toolparsers",
            )
            raise ValueError("parser output_schema is not valid JSON Schema") from exc
        if self.output_schema.get("type") != "object":
            raise ValueError("parser output_schema must describe one object")
        if self.output_schema.get("additionalProperties") is not False:
            raise ValueError("parser output_schema must set additionalProperties=false")
        return self

    def argv(self) -> list[str]:
        return [
            self.executable,
            "--protocol",
            self.protocol,
            "--input",
            self.input_path,
            "--output",
            self.output_path,
        ]

    def validate_result(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ToolOutputParseError("parser container must return one JSON object")
        try:
            Draft202012Validator(self.output_schema).validate(payload)
        except ValidationError as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.toolparsers.caught_failure_008",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="toolparsers",
            )
            raise ToolOutputParseError(
                f"parser container output violates its schema: {exc.message}"
            ) from exc
        return payload


class ParserContainerExecutor(Protocol):
    async def parse(
        self, contract: ParserContainerContract, raw_output: bytes
    ) -> dict[str, Any]: ...


class SandboxParserExecutor:
    """Run parser containers offline against one private, read-only input mount."""

    def __init__(self, *, runner: SandboxRunner, parser_root: Path) -> None:
        self.runner = runner
        self.parser_root = parser_root.expanduser()

    async def parse(
        self, contract: ParserContainerContract, raw_output: bytes
    ) -> dict[str, Any]:
        if len(raw_output) > MAX_PARSER_INPUT_BYTES:
            raise ToolOutputParseError("tool output exceeds the parser input limit")
        self.parser_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.parser_root, 0o700)
        with tempfile.TemporaryDirectory(
            prefix="nebula-parser-", dir=self.parser_root
        ) as temporary:
            workspace = Path(temporary)
            os.chmod(workspace, 0o700)
            input_path = workspace / "tool-output"
            descriptor = os.open(
                input_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw_output)
                stream.flush()
                os.fsync(stream.fileno())
            result = await self.runner.run(
                SandboxRequest(
                    image=contract.image,
                    command=contract.argv(),
                    workspace=workspace,
                    workspace_access=SandboxWorkspaceAccess.READ,
                    network=SandboxNetwork.NONE,
                    execution_kind=SandboxExecutionKind.PARSER,
                    limits=SandboxLimits(
                        cpu_count=1,
                        memory_mb=contract.memory_mb,
                        pids=64,
                        timeout_seconds=contract.timeout_seconds,
                        output_bytes=10_000_000,
                    ),
                )
            )
            if result.timed_out:
                raise ToolOutputParseError("parser container timed out")
            if result.exit_code != 0:
                raise ToolOutputParseError(
                    f"parser container exited with status {result.exit_code}"
                )
            try:
                payload = json.loads(_bounded_text(result.stdout))
            except json.JSONDecodeError as exc:
                record_caught_exception(
                    "toolbox",
                    "toolbox.toolparsers.caught_failure_009",
                    "A handled toolbox operation raised an exception.",
                    exc,
                    stage="toolparsers",
                )
                raise ToolOutputParseError(
                    "parser container stdout is not valid JSON"
                ) from exc
            return contract.validate_result(payload)
