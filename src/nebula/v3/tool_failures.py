"""One bounded, privacy-safe failure contract for assistant-visible tools."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from typing import Any
from uuid import uuid4

from jsonschema.exceptions import ValidationError

from .diagnostics import get_diagnostics, record_diagnostic
from .tools import InvalidToolArguments, PolicyDenied, ToolSpec

FAILURE_SCHEMA = "nebula.tool-failure/v1"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_SCHEMA_KEYS = (
    "type",
    "enum",
    "const",
    "format",
    "pattern",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "required",
    "additionalProperties",
)


def _diagnostic(
    spec: ToolSpec | None, error: BaseException, *, call_id: str | None
) -> tuple[str, bool]:
    """Keep the exact effective schema and original error outside model context."""

    reference = f"err_{uuid4().hex}"
    manager = get_diagnostics()
    captured = False
    if manager is not None:
        directory = manager.data_dir / "tool-failure-diagnostics"
        detail = {
            "schema": "nebula.tool-failure-diagnostic/v1",
            "reference": reference,
            "call_id": call_id,
            "tool": spec.name if spec else None,
            "tool_version": spec.version if spec else None,
            "original_failure": f"{type(error).__name__}: {error}",
            "effective_input_schema": spec.input_schema if spec else None,
        }
        temporary = directory / f".{reference}.tmp"
        try:
            if directory.is_symlink():
                raise OSError("tool diagnostic directory is a symbolic link")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
            target = directory / f"{reference}.json"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    detail, stream, ensure_ascii=False, sort_keys=True, default=str
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            captured = True
        except OSError:
            # The model still receives bounded recovery advice when the private
            # diagnostic sink is unavailable. The reference then names the log.
            with contextlib.suppress(OSError):
                temporary.unlink()
        record_diagnostic(
            "warning",
            "runtime",
            "runtime.tool_failure.recorded",
            "An assistant tool failed; the diagnostic reference records its status.",
            error_id=reference,
            outcome="failure",
            stage="tool_call",
        )
    return reference, captured


def unavailable_tool_failure(
    name: str, detail: str, *, call_id: str | None = None
) -> dict[str, Any]:
    """A call that was never offered has no effective input schema to repeat."""

    reference, captured = _diagnostic(
        None, InvalidToolArguments(detail), call_id=call_id
    )
    return {
        "schema": FAILURE_SCHEMA,
        "status": "failed",
        "tool": name[:128],
        "category": "unavailable_tool",
        "problem": "The requested tool was not offered for this step.",
        "side_effects": "none",
        "invalid_input": None,
        "effective_input_schema": None,
        "schema_truncated": False,
        "schema_reference": None,
        "next_action": "Choose a tool offered in this step, or finish without a tool.",
        "retry_safe": False,
        "diagnostic_reference": reference,
        "diagnostic_available": captured,
    }


def _invalid_field(error: BaseException, arguments: dict[str, Any]) -> str | None:
    cause = error if isinstance(error, ValidationError) else error.__cause__
    if isinstance(cause, ValidationError):
        if cause.absolute_path:
            return str(next(iter(cause.absolute_path)))
        if cause.validator == "required":
            match = re.search(r"'([^']+)' is a required property", cause.message)
            return match.group(1) if match else None
        if cause.validator == "additionalProperties":
            match = re.search(r"'([^']+)' was unexpected", cause.message)
            return match.group(1) if match else None
    if isinstance(error, InvalidToolArguments):
        match = re.search(r"(?:at|for) ([a-z][a-z0-9_]*)", str(error))
        if match and match.group(1) in arguments:
            return match.group(1)
    return None


def tool_failure(
    spec: ToolSpec,
    arguments: dict[str, Any],
    error: BaseException,
    *,
    phase: str,
    call_id: str | None = None,
) -> dict[str, Any]:
    """Classify without copying untrusted error text or argument values to a model."""

    field = _invalid_field(error, arguments)
    hash_as_artifact = (
        spec.name == "tool_output.read"
        and isinstance(arguments.get("artifact_id"), str)
        and bool(_SHA256.fullmatch(arguments["artifact_id"]))
    )
    if hash_as_artifact:
        field = "artifact_id"
    denied = isinstance(error, PolicyDenied) or isinstance(error, PermissionError)
    invalid = (
        isinstance(error, (InvalidToolArguments, ValidationError)) or hash_as_artifact
    )
    missing = type(error).__name__ in {"ToolOutputAccessError", "NotFoundError"}
    if missing and spec.name == "tool_output.read":
        field = "artifact_id"
    if missing and spec.name == "tool_output.search":
        field = "tool_call_id"
    timeout = isinstance(error, TimeoutError)
    cancelled = type(error).__name__ == "CancelledError"
    category = (
        "permission_denied"
        if denied
        else "invalid_arguments"
        if invalid
        else "unavailable_resource"
        if missing
        else "timeout"
        if timeout
        else "cancelled"
        if cancelled
        else "unavailable_dependency"
        if isinstance(error, (ConnectionError, OSError))
        else "execution_failed"
    )
    before_execution = phase == "before_execution"
    read_only_retrieval = spec.name in {
        "tool_output.read",
        "tool_output.search",
        "workspace.read",
        "workspace.search",
    }
    no_effects = before_execution or read_only_retrieval
    side_effects = "none" if no_effects else "unknown"
    if hash_as_artifact:
        problem = "artifact_id contains a SHA-256 digest, not an artifact receipt ID."
        action = "Use the artifact_id from an authorized tool result receipt; sha256 is a separate digest field."
    elif invalid:
        problem = f"Invalid input{f' in {field}' if field else ''}."
        action = (
            "Correct the indicated argument using the effective input schema, then issue a new call."
            if no_effects
            else "Check the operation state before correcting the input or issuing a new call."
        )
    elif missing:
        problem = "The requested resource is unavailable to this caller."
        action = (
            "Use an ID from a receipt available in this conversation, or select another authorized resource."
            if no_effects
            else "Check the operation state, then select an authorized resource."
        )
    elif denied:
        problem = "The call was denied by an access or approval rule."
        action = "Request access or choose an authorized action; do not repeat the denied call."
    elif timeout:
        problem = "The tool did not finish before its time limit."
        action = "Check the operation's recorded state before deciding whether to issue a new call."
    elif cancelled:
        problem = "The tool call was cancelled."
        action = "Check the operation's recorded state before deciding whether to issue a new call."
    else:
        problem = "The tool could not complete."
        action = "Check the diagnostic reference and operation state before choosing another action."
    schema_bytes = json.dumps(spec.input_schema, sort_keys=True, default=str).encode()
    schema_reference = (
        f"{spec.name}@{spec.version}:sha256:{hashlib.sha256(schema_bytes).hexdigest()}"
    )
    properties = spec.input_schema.get("properties", {})
    selected = properties.get(field) if field else None
    if not isinstance(selected, dict):
        selected = None
    safe_field = (
        {key: selected[key] for key in _SAFE_SCHEMA_KEYS if key in selected}
        if selected
        else None
    )
    if safe_field and len(json.dumps(safe_field, default=str)) > 1_000:
        safe_field = {
            key: selected[key] for key in ("type", "format") if key in selected
        }
        if isinstance(selected.get("enum"), list):
            safe_field["enum_count"] = len(selected["enum"])
    if field == "artifact_id" and safe_field is not None:
        safe_field["description"] = (
            "ID from the artifact receipt; sha256 is a digest, not this ID."
        )
    reference, captured = _diagnostic(spec, error, call_id=call_id)
    # The model was offered this schema for the call. Repeat it with the error
    # so a provider that does not retain function declarations can correct the
    # next request. Keep the whole envelope inside the 8 KiB result limit.
    inline_schema = len(schema_bytes) <= 4_000
    result = {
        "schema": FAILURE_SCHEMA,
        "status": "failed",
        "tool": spec.name,
        "category": category,
        "problem": problem,
        "side_effects": side_effects,
        "invalid_input": field,
        "effective_input_schema": spec.input_schema
        if inline_schema
        else (
            {
                "properties": {field: safe_field},
                "required": field in spec.input_schema.get("required", []),
            }
            if field and safe_field
            else {
                "required": [
                    str(name)[:80]
                    for name in spec.input_schema.get("required", [])[:32]
                ],
                "properties": [str(name)[:80] for name in sorted(properties)[:32]],
            }
        ),
        "schema_truncated": not inline_schema,
        "schema_reference": schema_reference,
        "next_action": action,
        "retry_safe": no_effects and not denied and not missing,
        "diagnostic_reference": reference,
        "diagnostic_available": captured,
    }
    return json.loads(json.dumps(result, ensure_ascii=False, default=str))
