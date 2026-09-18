"""Provider-native lifecycle hooks discovered only from the shared .agents tree."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator

from .domain import NativeHookExecution, NebulaModel, utc_now
from .storage import NebulaStore


HOOK_MANIFEST = "hook.json"
HOOK_EVENTS = {
    "chat.turn.started",
    "chat.turn.completed",
    "chat.turn.failed",
    "chat.turn.cancelled",
}
MAX_HOOK_OUTPUT_BYTES = 64 * 1024


class NativeHookError(RuntimeError):
    """A safe, operator-actionable native-hook failure."""


class NativeHookManifest(NebulaModel):
    version: Literal[1]
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1_000)
    events: list[str] = Field(min_length=1, max_length=16)
    command: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(default=10, ge=1, le=300)
    side_effects: Literal["none", "workspace", "external"] = "none"
    failure_policy: Literal["continue", "block"] = "continue"

    @field_validator("events")
    @classmethod
    def supported_events(cls, value: list[str]) -> list[str]:
        distinct = list(dict.fromkeys(value))
        unknown = sorted(set(distinct) - HOOK_EVENTS)
        if unknown:
            raise ValueError(f"unsupported hook events: {', '.join(unknown)}")
        return distinct

    @field_validator("command")
    @classmethod
    def bounded_command(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item or len(item) > 2_000 for item in value):
            raise ValueError("hook command entries must be non-empty bounded strings")
        executable = Path(value[0])
        if executable.is_absolute() or ".." in executable.parts:
            raise ValueError("hook executable must be relative to its hook directory")
        return value


class NativeHookDescriptor(NebulaModel):
    id: str
    source: Literal["project"] = "project"
    path: str
    manifest: NativeHookManifest
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NativeHookSnapshot(NativeHookDescriptor):
    selected_at: str


def _read_bounded(path: Path, *, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise NativeHookError(f"hook file is not a regular file: {path.name}")
    size = path.stat().st_size
    if size > maximum:
        raise NativeHookError(f"hook file exceeds {maximum} bytes: {path.name}")
    return path.read_bytes()


def discover_native_hooks(workspace: Path) -> list[NativeHookDescriptor]:
    root = workspace.resolve() / ".agents" / "hooks"
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise NativeHookError(".agents/hooks must be a regular directory")
    descriptors: list[NativeHookDescriptor] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if directory.is_symlink() or not directory.is_dir():
            continue
        manifest_path = directory / HOOK_MANIFEST
        if not manifest_path.exists():
            continue
        raw = _read_bounded(manifest_path, maximum=64 * 1024)
        try:
            manifest = NativeHookManifest.model_validate_json(raw)
        except ValueError as exc:
            raise NativeHookError(f"invalid hook manifest {directory.name}: {exc}") from exc
        executable_source = directory / manifest.command[0]
        if executable_source.is_symlink():
            raise NativeHookError(
                f"hook executable cannot be a symlink: {directory.name}"
            )
        executable = executable_source.resolve()
        try:
            executable.relative_to(directory.resolve())
        except ValueError as exc:
            raise NativeHookError(
                f"hook executable escapes its directory: {directory.name}"
            ) from exc
        executable_bytes = _read_bounded(executable, maximum=2 * 1024 * 1024)
        if not os.access(executable, os.X_OK):
            raise NativeHookError(f"hook executable is not executable: {directory.name}")
        descriptors.append(
            NativeHookDescriptor(
                id=directory.name,
                path=str(directory.resolve()),
                manifest=manifest,
                manifest_sha256=hashlib.sha256(raw).hexdigest(),
                executable_sha256=hashlib.sha256(executable_bytes).hexdigest(),
            )
        )
    return descriptors


def snapshot_native_hook(
    hook_id: str, catalog: list[NativeHookDescriptor]
) -> NativeHookSnapshot:
    matches = [item for item in catalog if item.id == hook_id]
    if len(matches) != 1:
        raise NativeHookError(f"native hook {hook_id!r} is not uniquely available")
    return NativeHookSnapshot(
        **matches[0].model_dump(mode="python"), selected_at=utc_now().isoformat()
    )


class NativeHookRunner:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    async def run(
        self,
        snapshot: NativeHookSnapshot,
        *,
        engagement_id: str,
        chat_session_id: str,
        chat_turn_id: str,
        event_name: str,
        payload: dict[str, Any],
    ) -> NativeHookExecution:
        if event_name not in snapshot.manifest.events:
            raise NativeHookError(f"hook {snapshot.id!r} does not subscribe to {event_name}")
        directory = Path(snapshot.path)
        manifest_path = directory / HOOK_MANIFEST
        executable = (directory / snapshot.manifest.command[0]).resolve()
        manifest_bytes = _read_bounded(manifest_path, maximum=64 * 1024)
        executable_bytes = _read_bounded(executable, maximum=2 * 1024 * 1024)
        if hashlib.sha256(manifest_bytes).hexdigest() != snapshot.manifest_sha256:
            raise NativeHookError(f"hook {snapshot.id!r} manifest changed after selection")
        if hashlib.sha256(executable_bytes).hexdigest() != snapshot.executable_sha256:
            raise NativeHookError(f"hook {snapshot.id!r} executable changed after selection")
        started = utc_now()
        execution = self.store.create(
            NativeHookExecution(
                id=str(uuid4()),
                engagement_id=engagement_id,
                chat_session_id=chat_session_id,
                chat_turn_id=chat_turn_id,
                hook_id=snapshot.id,
                hook_snapshot=snapshot.model_dump(mode="json"),
                event_name=event_name,
                side_effects=snapshot.manifest.side_effects,
                started_at=started,
            )
        )
        envelope = json.dumps(
            {
                "schema": "nebula.native-hook-event/v1",
                "event": event_name,
                "event_version": 1,
                "engagement_id": engagement_id,
                "chat_session_id": chat_session_id,
                "chat_turn_id": chat_turn_id,
                "payload": payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "NEBULA_HOOK_EVENT": event_name,
            "NEBULA_HOOK_EVENT_VERSION": "1",
        }
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [str(executable), *snapshot.manifest.command[1:]],
                cwd=str(directory),
                env=environment,
                input=envelope,
                capture_output=True,
                timeout=snapshot.manifest.timeout_seconds,
                check=False,
            )
            stdout, stderr = completed.stdout, completed.stderr
            status = "complete" if completed.returncode == 0 else "failed"
            error = None if completed.returncode == 0 else "hook exited unsuccessfully"
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
            status, error, exit_code = "timed_out", "hook timed out", None
        except OSError as exc:
            stdout, stderr = b"", b""
            status, error, exit_code = "failed", f"hook could not start: {exc}", None
        return self.store.update(
            NativeHookExecution,
            execution.id,
            {
                "status": status,
                "completed_at": utc_now(),
                "exit_code": exit_code,
                "stdout": stdout[:MAX_HOOK_OUTPUT_BYTES].decode("utf-8", "replace"),
                "stderr": stderr[:MAX_HOOK_OUTPUT_BYTES].decode("utf-8", "replace"),
                "error": error,
            },
            expected_revision=execution.revision,
        )


__all__ = [
    "HOOK_EVENTS",
    "NativeHookDescriptor",
    "NativeHookError",
    "NativeHookExecution",
    "NativeHookManifest",
    "NativeHookRunner",
    "NativeHookSnapshot",
    "discover_native_hooks",
    "snapshot_native_hook",
]
