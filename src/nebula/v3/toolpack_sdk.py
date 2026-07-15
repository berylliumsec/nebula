"""Safe scaffolding, validation, and deterministic archives for tool authors."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import RiskClass
from .toolpacks import (
    ToolPackManifest,
    ToolPackValidationError,
    canonical_manifest_json,
    compile_manifest_yaml,
    manifest_digest,
    parse_manifest_json,
)


MAX_PACK_ARCHIVE_BYTES = 100_000_000
MAX_PACK_ARCHIVE_MEMBERS = 10_000
_PLACEHOLDER = re.compile(r"\{\{sha256:[a-z0-9._-]+\}\}")


class ToolPackSDKError(RuntimeError):
    pass


class ToolPackArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: ToolPackManifest
    manifest_digest: str
    files: dict[str, bytes]


class CustomToolArgument(BaseModel):
    """One typed declarative argv binding used by CLI and UI generation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
    value_type: Literal[
        "string", "integer", "number", "boolean", "string_list", "integer_list"
    ]
    description: str = Field(default="", max_length=500)
    required: bool = True
    flag: str | None = Field(default=None, max_length=200)
    positional: bool = False
    smoke_value: Any = None

    @model_validator(mode="after")
    def binding_is_unambiguous(self) -> "CustomToolArgument":
        if self.positional == (self.flag is not None):
            raise ValueError("arguments require exactly one of flag or positional=true")
        if self.flag is not None and (
            not self.flag.startswith("-") or "\x00" in self.flag
        ):
            raise ValueError("argument flags must be fixed option tokens")
        return self


class CustomToolDefinition(BaseModel):
    """Configuration-only contract for a parser-free custom OCI tool."""

    model_config = ConfigDict(extra="forbid")

    pack_name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    publisher: str = Field(default="local", pattern=r"^[a-z0-9][a-z0-9.-]{0,127}$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    description: str = Field(min_length=1, max_length=2_000)
    image: str = Field(pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$")
    platform: Literal["linux/amd64", "linux/arm64"] = "linux/amd64"
    executable: str = Field(pattern=r"^/[^\x00]+$")
    fixed_arguments: list[str] = Field(default_factory=list, max_length=64)
    arguments: list[CustomToolArgument] = Field(default_factory=list, max_length=64)
    risk_class: RiskClass = RiskClass.LOCAL_READ
    network_access: bool = False
    target_argument: str | None = None
    port_argument: str | None = None
    filesystem_access: Literal["none", "read", "workspace_write"] = "none"
    requires_approval: bool = False
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    output_flag: str | None = Field(default=None, max_length=200)
    output_filename: str = Field(default="result", pattern=r"^[A-Za-z0-9._-]{1,200}$")
    capture_paths: list[str] = Field(default_factory=list, max_length=32)
    expected_exit_code: int = Field(default=0, ge=0, le=255)

    @field_validator("fixed_arguments")
    @classmethod
    def fixed_arguments_are_literal(cls, values: list[str]) -> list[str]:
        if any("\x00" in value for value in values):
            raise ValueError("fixed arguments cannot contain NUL bytes")
        return values

    @model_validator(mode="after")
    def policy_and_arguments_are_coherent(self) -> "CustomToolDefinition":
        names = [item.name for item in self.arguments]
        if len(names) != len(set(names)):
            raise ValueError("custom tool argument names must be unique")
        if self.network_access and not self.target_argument:
            raise ValueError("network tools require target_argument")
        for mapped in (self.target_argument, self.port_argument):
            if mapped is not None and mapped not in names:
                raise ValueError(f"mapped argument {mapped!r} is not declared")
        if self.target_argument is not None:
            target = next(
                item for item in self.arguments if item.name == self.target_argument
            )
            if not target.required or target.value_type != "string":
                raise ValueError("target_argument must be a required string")
        if self.port_argument is not None:
            port = next(
                item for item in self.arguments if item.name == self.port_argument
            )
            if port.value_type not in {"integer", "integer_list"}:
                raise ValueError("port_argument must be an integer or integer list")
        if self.output_flag is not None and not self.output_flag.startswith("-"):
            raise ValueError("output_flag must be a fixed option token")
        if (
            self.risk_class
            in {
                RiskClass.CREDENTIAL_USE,
                RiskClass.EXPLOITATION,
                RiskClass.PERSISTENCE,
                RiskClass.DESTRUCTIVE,
            }
            and not self.requires_approval
        ):
            raise ValueError("invasive custom tools require approval")
        return self

    def permission_preview(self) -> dict[str, Any]:
        return {
            "network": self.network_access,
            "workspace": self.filesystem_access,
            "risk_class": self.risk_class.value,
            "requires_approval": self.requires_approval,
            "capture_paths": list(self.capture_paths),
            "rootless_oci_only": True,
            "parser": "not_configured",
        }


def _argument_schema(argument: CustomToolArgument) -> dict[str, Any]:
    schema: dict[str, Any]
    if argument.value_type == "string":
        schema = {"type": "string", "maxLength": 8_192}
    elif argument.value_type == "integer":
        schema = {"type": "integer"}
    elif argument.value_type == "number":
        schema = {"type": "number"}
    elif argument.value_type == "boolean":
        schema = {"type": "boolean"}
    elif argument.value_type == "string_list":
        schema = {
            "type": "array",
            "items": {"type": "string", "maxLength": 8_192},
            "maxItems": 256,
        }
    else:
        schema = {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 65_535},
            "maxItems": 256,
        }
    if argument.description:
        schema["description"] = argument.description
    return schema


def _smoke_value(argument: CustomToolArgument) -> Any:
    if argument.smoke_value is not None:
        return argument.smoke_value
    return {
        "string": "127.0.0.1" if "target" in argument.name else "smoke-test",
        "integer": 80 if "port" in argument.name else 1,
        "number": 1.0,
        "boolean": False,
        "string_list": ["smoke-test"],
        "integer_list": [80],
    }[argument.value_type]


def custom_tool_manifest(definition: CustomToolDefinition) -> ToolPackManifest:
    """Build and validate the exact v2 manifest shared by every authoring surface."""

    properties = {
        argument.name: _argument_schema(argument) for argument in definition.arguments
    }
    if definition.port_argument is not None:
        port_schema = properties[definition.port_argument]
        if port_schema.get("type") == "integer":
            port_schema.update(minimum=1, maximum=65_535)
    required = [argument.name for argument in definition.arguments if argument.required]
    bindings = []
    for argument in definition.arguments:
        if argument.positional:
            kind = "positional"
        elif argument.value_type == "boolean":
            kind = "boolean_flag"
        elif argument.value_type in {"string_list", "integer_list"}:
            kind = "repeat"
        else:
            kind = "value"
        bindings.append(
            {
                "argument": argument.name,
                "kind": kind,
                **({"flag": argument.flag} if argument.flag is not None else {}),
            }
        )
    fixed_arguments = list(definition.fixed_arguments)
    if definition.output_flag is not None:
        fixed_arguments.extend(
            [definition.output_flag, f"{{output_dir}}/{definition.output_filename}"]
        )
    smoke_arguments = {
        argument.name: _smoke_value(argument)
        for argument in definition.arguments
        if argument.required or argument.smoke_value is not None
    }
    payload = {
        "api_version": "tools.nebula.security/v2",
        "kind": "ToolPack",
        "metadata": {
            "publisher": definition.publisher,
            "name": definition.pack_name,
            "version": "0.1.0",
            "minimum_nebula_version": "3.0.0a1",
            "description": definition.description,
            "licenses": ["LicenseRef-Proprietary"],
        },
        "images": [
            {
                "name": "tool",
                "platform": definition.platform,
                "image": definition.image,
                "sbom": "sbom/tool.cdx.json",
                "provenance": "provenance/tool.intoto.jsonl",
            }
        ],
        "permissions": {
            "network": definition.network_access,
            "workspace": definition.filesystem_access,
            "credentials": [],
        },
        "tools": [
            {
                "name": definition.tool_name,
                "description": definition.description,
                "image": "tool",
                "executable": definition.executable,
                "fixed_arguments": fixed_arguments,
                "argument_bindings": bindings,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                "policy": {
                    "risk_class": definition.risk_class.value,
                    "target_argument": definition.target_argument,
                    "port_argument": definition.port_argument,
                    "network_access": definition.network_access,
                    "filesystem_access": definition.filesystem_access,
                    "requires_approval": definition.requires_approval,
                },
                "capture_paths": definition.capture_paths,
                "timeout_seconds": definition.timeout_seconds,
                "smoke_tests": [
                    {
                        "arguments": smoke_arguments,
                        "expected_exit_code": definition.expected_exit_code,
                        "timeout_seconds": min(definition.timeout_seconds, 300),
                    }
                ],
            }
        ],
    }
    return compile_manifest_yaml(yaml.safe_dump(payload, sort_keys=False))


def generate_custom_tool_project(
    directory: Path, definition: CustomToolDefinition
) -> Path:
    """Generate a complete unsigned local source bundle without host execution."""

    root = directory.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ToolPackSDKError("custom-tool destination must be empty")
    root.mkdir(parents=True, exist_ok=True)
    for relative in ("schemas", "tests", "sbom", "provenance"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    manifest = custom_tool_manifest(definition)
    source = yaml.safe_dump(
        manifest.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=False,
    )
    files = {
        "nebula-tool-pack.yaml": source,
        "schemas/input.json": json.dumps(
            manifest.tools[0].input_schema, indent=2, sort_keys=True
        )
        + "\n",
        "tests/smoke.json": json.dumps(
            manifest.tools[0].smoke_tests[0].model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        "tests/policy-cases.yaml": "cases: []\n",
        "sbom/tool.cdx.json": json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "version": 1,
                "metadata": {
                    "component": {"type": "container", "name": definition.image}
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        "provenance/tool.intoto.jsonl": json.dumps(
            {
                "_type": "https://in-toto.io/Statement/v1",
                "subject": [{"name": definition.image}],
                "predicateType": "https://slsa.dev/provenance/v1",
                "predicate": {"buildDefinition": {}, "runDetails": {}},
            },
            sort_keys=True,
        )
        + "\n",
        "README.md": (
            f"# {definition.pack_name}\n\nGenerated parser-free custom tool "
            f"`{definition.tool_name}`. Raw stdout/stderr and files under "
            "`NEBULA_OUTPUT_DIR` become immutable artifacts.\n"
        ),
    }
    for relative, content in files.items():
        (root / relative).write_text(content, encoding="utf-8", newline="\n")
    validate_tool_pack_directory(root, allow_digest_placeholders=False)
    return root


def init_tool_pack(directory: Path, *, name: str, publisher: str) -> Path:
    """Create a conservative source pack that is not installable before build."""

    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", name):
        raise ToolPackSDKError("tool-pack name must be a canonical identifier")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", publisher):
        raise ToolPackSDKError("publisher must be a canonical identifier")
    root = directory.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ToolPackSDKError("tool-pack destination must be empty")
    root.mkdir(parents=True, exist_ok=True)
    for relative in ("schemas", "tests/parser-fixtures", "tests"):
        (root / relative).mkdir(parents=True, exist_ok=True)

    files = {
        "nebula-tool-pack.yaml": _manifest_template(name, publisher),
        "Containerfile": _containerfile_template(),
        "schemas/input.json": _schema_template("message"),
        "schemas/output.json": _schema_template("result"),
        "tests/policy-cases.yaml": "cases: []\n",
        "tests/parser-fixtures/output.json": '{"result":"ok"}\n',
        "README.md": _readme_template(name),
    }
    for relative, content in files.items():
        path = root / relative
        path.write_text(content, encoding="utf-8", newline="\n")
    return root


def _manifest_template(name: str, publisher: str) -> str:
    return f"""api_version: tools.nebula.security/v2
kind: ToolPack
metadata:
  publisher: {publisher}
  name: {name}
  version: 0.1.0
  minimum_nebula_version: 3.0.0a1
  description: A locally developed Nebula tool pack.
  licenses: [BSD-3-Clause]
images:
  - name: tool
    platform: linux/amd64
    image: example.invalid/{publisher}/{name}@{{{{sha256:tool-amd64}}}}
    sbom: sbom/tool-amd64.cdx.json
    provenance: provenance/tool-amd64.intoto.jsonl
  - name: tool
    platform: linux/arm64
    image: example.invalid/{publisher}/{name}@{{{{sha256:tool-arm64}}}}
    sbom: sbom/tool-arm64.cdx.json
    provenance: provenance/tool-arm64.intoto.jsonl
permissions:
  network: false
  workspace: none
  credentials: []
tools:
  - name: {name}.run
    description: Run a bounded example tool.
    image: tool
    executable: /usr/local/bin/example-tool
    fixed_arguments: []
    argument_bindings:
      - argument: message
        kind: value
        flag: --message
    input_schema:
      type: object
      properties:
        message: {{type: string, maxLength: 1000}}
      required: [message]
      additionalProperties: false
    policy:
      risk_class: local_read
      network_access: false
      filesystem_access: none
      requires_approval: false
    smoke_tests:
      - arguments: {{message: smoke-test}}
        expected_exit_code: 0
        timeout_seconds: 30
"""


def _containerfile_template() -> str:
    return """FROM alpine:3.22
RUN addgroup -S nebula && adduser -S -G nebula -u 10001 nebula
COPY --chown=nebula:nebula example-tool /usr/local/bin/example-tool
USER 10001:10001
"""


def _schema_template(property_name: str) -> str:
    return (
        json.dumps(
            {
                "type": "object",
                "properties": {property_name: {"type": "string"}},
                "required": [property_name],
                "additionalProperties": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _readme_template(name: str) -> str:
    return f"""# {name}

Build both Linux platforms, replace every `{{{{sha256:...}}}}` placeholder with
the pushed OCI manifest digest, then run `nebula tools validate` and
`nebula tools pack`. Nebula never executes the Containerfile on the host.
"""


def validate_tool_pack_directory(
    directory: Path, *, allow_digest_placeholders: bool = True
) -> ToolPackManifest:
    root = directory.expanduser().resolve()
    manifest_path = root / "nebula-tool-pack.yaml"
    try:
        source = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.toolpack_sdk.caught_failure_001",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="toolpack_sdk",
        )
        raise ToolPackSDKError("cannot read nebula-tool-pack.yaml") from exc
    if allow_digest_placeholders:
        source = _PLACEHOLDER.sub("sha256:" + "0" * 64, source)
    elif _PLACEHOLDER.search(source):
        raise ToolPackSDKError("pack contains unresolved image digest placeholders")
    try:
        manifest = compile_manifest_yaml(source)
    except ToolPackValidationError as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.toolpack_sdk.caught_failure_002",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="toolpack_sdk",
        )
        raise ToolPackSDKError("source manifest is invalid") from exc
    _validate_source_tree(root)
    return manifest


def _validate_source_tree(root: Path) -> None:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ToolPackSDKError(f"tool packs cannot contain symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.is_absolute() or ".." in relative.parts:
            raise ToolPackSDKError("tool-pack path escapes its source directory")
        total += path.stat().st_size
        if total > MAX_PACK_ARCHIVE_BYTES:
            raise ToolPackSDKError("tool-pack source exceeds the archive limit")


def pack_tool_pack(directory: Path, destination: Path) -> Path:
    root = directory.expanduser().resolve()
    manifest_source = (root / "nebula-tool-pack.yaml").read_text(encoding="utf-8")
    if _PLACEHOLDER.search(manifest_source):
        raise ToolPackSDKError("pack contains unresolved image digest placeholders")
    manifest = validate_tool_pack_directory(root, allow_digest_placeholders=False)
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ToolPackSDKError("refusing to overwrite an existing pack archive")
    files = sorted(path for path in root.rglob("*") if path.is_file())
    try:
        with zipfile.ZipFile(
            destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            _write_deterministic(
                archive, "manifest.json", canonical_manifest_json(manifest) + b"\n"
            )
            for path in files:
                relative = path.relative_to(root).as_posix()
                _write_deterministic(archive, f"source/{relative}", path.read_bytes())
    except Exception as caught_error:
        record_caught_exception(
            "toolbox",
            "toolbox.toolpack_sdk.caught_failure_003",
            "A handled toolbox operation raised an exception.",
            caught_error,
            stage="toolpack_sdk",
        )
        destination.unlink(missing_ok=True)
        raise
    return destination


def _write_deterministic(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    archive.writestr(info, payload)


def read_tool_pack(path: Path) -> ToolPackArchive:
    try:
        if path.stat().st_size > MAX_PACK_ARCHIVE_BYTES:
            raise ToolPackSDKError("tool-pack archive exceeds the file-size limit")
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_PACK_ARCHIVE_MEMBERS:
                raise ToolPackSDKError("tool-pack archive has too many members")
            if sum(member.file_size for member in members) > MAX_PACK_ARCHIVE_BYTES:
                raise ToolPackSDKError("tool-pack archive exceeds the expanded limit")
            files: dict[str, bytes] = {}
            for member in members:
                pure = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if (
                    pure.is_absolute()
                    or ".." in pure.parts
                    or "\\" in member.filename
                    or stat.S_ISLNK(mode)
                    or member.is_dir()
                ):
                    raise ToolPackSDKError(
                        f"unsafe tool-pack archive member: {member.filename}"
                    )
                if member.filename in files:
                    raise ToolPackSDKError(
                        f"duplicate tool-pack archive member: {member.filename}"
                    )
                files[member.filename] = archive.read(member)
    except (OSError, zipfile.BadZipFile) as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.toolpack_sdk.caught_failure_004",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="toolpack_sdk",
        )
        raise ToolPackSDKError("tool-pack archive is unreadable") from exc
    try:
        manifest_payload = json.loads(files.pop("manifest.json"))
        manifest = parse_manifest_json(json.dumps(manifest_payload))
        source_manifest = compile_manifest_yaml(files["source/nebula-tool-pack.yaml"])
        if canonical_manifest_json(source_manifest) != canonical_manifest_json(
            manifest
        ):
            raise ToolPackSDKError(
                "tool-pack source and canonical manifests do not match"
            )
    except ToolPackSDKError as caught_error:
        record_caught_exception(
            "toolbox",
            "toolbox.toolpack_sdk.caught_failure_005",
            "A handled toolbox operation raised an exception.",
            caught_error,
            stage="toolpack_sdk",
        )
        raise
    except Exception as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.toolpack_sdk.caught_failure_006",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="toolpack_sdk",
        )
        raise ToolPackSDKError("tool-pack archive has no valid manifest") from exc
    return ToolPackArchive(
        manifest=manifest,
        manifest_digest=manifest_digest(manifest),
        files=files,
    )
