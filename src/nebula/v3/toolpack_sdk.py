"""Safe scaffolding, validation, and deterministic archives for tool authors."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from .toolpacks import (
    ToolPackManifestV1,
    ToolPackValidationError,
    canonical_manifest_json,
    compile_manifest_yaml,
    manifest_digest,
)


MAX_PACK_ARCHIVE_BYTES = 100_000_000
MAX_PACK_ARCHIVE_MEMBERS = 10_000
_PLACEHOLDER = re.compile(r"\{\{sha256:[a-z0-9._-]+\}\}")


class ToolPackSDKError(RuntimeError):
    pass


class ToolPackArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: ToolPackManifestV1
    manifest_digest: str
    files: dict[str, bytes]


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
    return f"""api_version: tools.nebula.security/v1
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
    fixed_arguments: [--json]
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
    output_schema:
      type: object
      properties:
        result: {{type: string}}
      required: [result]
      additionalProperties: false
    policy:
      risk_class: local_read
      network_access: false
      filesystem_access: none
      requires_approval: false
    parser:
      built_in: json/v1
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
) -> ToolPackManifestV1:
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
        manifest = ToolPackManifestV1.model_validate(manifest_payload)
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
