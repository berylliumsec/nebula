"""Operator-enabled SSH environments: settings, discovery view, and agent tools.

``ssh_environments`` reads ``~/.ssh/config`` and talks to the system client.
This module joins that view with the operator's saved choices
(:class:`SshEnvironment`) and turns enabled hosts into ordinary broker tools,
one ``ssh.<host>.run_command`` per host, so remote commands share the ledger,
approval, and artifact path used by every other tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import Field

from . import ssh_environments as ssh
from .domain import (
    NebulaModel,
    RiskClass,
    SshEnvironment,
    SshEnvironmentApproval,
    SshEnvironmentProbe,
    utc_now,
)
from .storage import NebulaStore, NotFoundError

MAX_STREAM_BYTES = 1_000_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
MAX_COMMAND_TIMEOUT_SECONDS = 3_600


class SshEnvironmentResolved(NebulaModel):
    hostname: str
    user: str
    port: int
    identity_files: list[str] = Field(default_factory=list)
    options: dict[str, str] = Field(default_factory=dict)


class SshEnvironmentHost(NebulaModel):
    alias: str
    aliases: list[str] = Field(default_factory=list)
    comment: str = ""
    source: str | None = None
    line: int | None = None
    in_config: bool = True
    resolved: SshEnvironmentResolved | None = None
    resolve_error: str | None = None
    environment: SshEnvironment | None = None


class SshEnvironmentDiscovery(NebulaModel):
    config_path: str
    config_exists: bool
    ssh_available: bool
    files_read: list[str] = Field(default_factory=list)
    skipped_patterns: list[str] = Field(default_factory=list)
    skipped_match_blocks: int = 0
    errors: list[str] = Field(default_factory=list)
    hosts: list[SshEnvironmentHost] = Field(default_factory=list)
    read_at: Any = Field(default_factory=utc_now)


class SshEnvironmentSettings(NebulaModel):
    """Operator-editable fields; omitted fields keep their saved value."""

    display_name: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    notes: str | None = Field(default=None, max_length=4_000)
    working_directory: str | None = Field(default=None, max_length=1_024)
    command_approval: SshEnvironmentApproval | None = None
    expected_revision: int | None = Field(default=None, ge=1)


def environment_id(alias: str) -> str:
    return f"ssh:{alias}"


class SshEnvironmentService:
    def __init__(
        self,
        store: NebulaStore,
        *,
        config_path: Path | str | None = None,
        probe: Callable[[str], ssh.SshProbeResult] | None = None,
    ) -> None:
        self.store = store
        self.config_path = config_path
        self._probe = probe or (lambda alias: ssh.probe_host(alias, self.config_path))

    def get(self, alias: str) -> SshEnvironment | None:
        try:
            return self.store.get(SshEnvironment, environment_id(alias))
        except NotFoundError:
            return None

    def saved(self) -> list[SshEnvironment]:
        return self.store.list_entities(SshEnvironment, limit=1_000)

    def discover(self, *, resolve: bool = True) -> SshEnvironmentDiscovery:
        """List config hosts with saved settings; ``resolve`` adds ``ssh -G`` facts."""

        scan = ssh.discover_hosts(self.config_path)
        available = ssh.ssh_binary() is not None
        saved = {item.alias: item for item in self.saved()}
        hosts: list[SshEnvironmentHost] = []
        for entry in scan.hosts:
            resolved: SshEnvironmentResolved | None = None
            resolve_error: str | None = None
            if available and resolve:
                try:
                    facts = ssh.resolve_host(entry.alias, self.config_path)
                    resolved = SshEnvironmentResolved(
                        hostname=facts.hostname,
                        user=facts.user,
                        port=facts.port,
                        identity_files=list(facts.identity_files),
                        options=facts.options,
                    )
                except Exception as exc:  # diagnostic-expected: shown on the host row
                    resolve_error = str(exc)[:500] or type(exc).__name__
            hosts.append(
                SshEnvironmentHost(
                    alias=entry.alias,
                    aliases=list(entry.aliases),
                    comment=entry.comment,
                    source=entry.source,
                    line=entry.line,
                    resolved=resolved,
                    resolve_error=resolve_error,
                    environment=saved.pop(entry.alias, None),
                )
            )
        # Settings for hosts that disappeared from the config stay visible so
        # the operator can see why a host stopped working and forget it.
        for alias, environment in sorted(saved.items()):
            hosts.append(
                SshEnvironmentHost(
                    alias=alias, in_config=False, environment=environment
                )
            )
        return SshEnvironmentDiscovery(
            config_path=scan.path,
            config_exists=scan.exists,
            ssh_available=available,
            files_read=scan.files_read,
            skipped_patterns=scan.skipped_patterns,
            skipped_match_blocks=scan.skipped_match_blocks,
            errors=scan.errors,
            hosts=hosts,
        )

    def _require_configured(self, alias: str) -> None:
        if alias not in {
            host.alias for host in ssh.discover_hosts(self.config_path).hosts
        }:
            raise NotFoundError(f"Host {alias!r} is not defined in the SSH config")

    def save(self, alias: str, settings: SshEnvironmentSettings) -> SshEnvironment:
        changes = settings.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if "working_directory" in changes:
            changes["working_directory"] = (
                changes["working_directory"] or ""
            ).strip() or None
        existing = self.get(alias)
        if existing is None:
            self._require_configured(alias)
            return self.store.create(
                SshEnvironment(id=environment_id(alias), alias=alias, **changes)
            )
        return self.store.update(
            SshEnvironment,
            existing.id,
            changes,
            expected_revision=settings.expected_revision,
        )

    def forget(self, alias: str) -> None:
        existing = self.get(alias)
        if existing is None:
            raise NotFoundError(f"No saved settings for SSH host {alias!r}")
        self.store.delete(SshEnvironment, existing.id)

    async def probe(self, alias: str) -> SshEnvironment:
        existing = self.get(alias)
        if existing is None:
            self._require_configured(alias)
        result = await asyncio.to_thread(self._probe, alias)
        snapshot = SshEnvironmentProbe(
            status=result.status,
            latency_ms=result.latency_ms,
            system=result.system[:100],
            os_version=result.os_version[:200],
            arch=result.arch[:50],
            model=result.model[:200],
            tools=list(result.tools)[:64],
            passwordless_sudo=result.passwordless_sudo,
            detail=result.detail[:1_000],
        )
        if existing is None:
            return self.store.create(
                SshEnvironment(
                    id=environment_id(alias), alias=alias, last_probe=snapshot
                )
            )
        return self.store.update(
            SshEnvironment,
            existing.id,
            {"last_probe": snapshot},
        )

    def resolve_for_chat(self, ids: list[str] | None) -> tuple[SshEnvironment, ...]:
        return resolve_ssh_environments(self.store, ids)


def resolve_ssh_environments(
    store: NebulaStore, ids: list[str] | None
) -> tuple[SshEnvironment, ...]:
    """Enabled hosts for a turn: all of them by default, or an explicit subset."""

    if ids is None:
        return tuple(
            sorted(
                (
                    item
                    for item in store.list_entities(SshEnvironment, limit=1_000)
                    if item.enabled
                ),
                key=lambda item: item.alias,
            )
        )
    selected: list[SshEnvironment] = []
    for item_id in dict.fromkeys(ids):
        environment = store.get(SshEnvironment, item_id)
        if not environment.enabled:
            raise ValueError(f"SSH environment {environment.label!r} is not enabled")
        selected.append(environment)
    return tuple(selected)


def ssh_tool_name(alias: str) -> str:
    stem = re.sub(r"[^a-z0-9_-]+", "-", alias.casefold()).strip("-_")[:60] or "host"
    if stem != alias:
        # Distinct aliases that normalize alike must still get distinct tools.
        stem = f"{stem}-{hashlib.sha256(alias.encode()).hexdigest()[:6]}"
    return f"ssh.{stem}.run_command"


def _describe(environment: SshEnvironment) -> str:
    facts: list[str] = []
    probe = environment.last_probe
    if probe is not None and probe.status == "reachable":
        facts.append(
            " ".join(
                part for part in (probe.os_version or probe.system, probe.arch) if part
            )
        )
        if probe.model:
            facts.append(probe.model)
        if probe.tools:
            facts.append("has " + ", ".join(probe.tools))
        if probe.passwordless_sudo is False:
            facts.append("sudo needs a password, so do not use sudo")
    lines = [
        # The UI reads the host from this exact first line on approval cards.
        f"Run a shell command on the operator's machine {' '.join(environment.label.split())} "
        f"(SSH host {environment.alias}).",
        "The command runs in /bin/sh on that machine, not in the Nebula workspace; "
        "files there are separate from the workspace.",
    ]
    if facts:
        lines.append("Host: " + "; ".join(facts) + ".")
    if environment.working_directory:
        lines.append(f"Default directory: {environment.working_directory}.")
    if environment.notes.strip():
        lines.append("Operator notes: " + environment.notes.strip())
    lines.append(
        "Commands are non-interactive (no stdin, no password prompts). Returns exit "
        "code, stdout and stderr."
    )
    return "\n".join(lines)


async def run_remote_command(
    alias: str,
    command: str,
    *,
    cwd: str | None,
    timeout_seconds: int,
    config_path: Path | str | None = None,
) -> tuple[int | None, bytes, bytes, bool]:
    """Run one command over ssh. Returns ``(exit_code, stdout, stderr, timed_out)``."""

    argv = ssh.remote_command_argv(alias, command, cwd=cwd, path=config_path)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def drain(stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        chunks: list[bytes] = []
        size = 0
        while chunk := await stream.read(65_536):
            if size < MAX_STREAM_BYTES:
                chunks.append(chunk[: MAX_STREAM_BYTES - size])
            size += len(chunk)
        return b"".join(chunks)

    readers = asyncio.gather(drain(process.stdout), drain(process.stderr))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await process.wait()
    stdout, stderr = await readers
    return (None if timed_out else process.returncode), stdout, stderr, timed_out


def build_ssh_tool_plugins(
    environments: tuple[SshEnvironment, ...],
    *,
    config_path: Path | str | None = None,
) -> list[Any]:
    """One broker plugin per enabled host; ``ask`` hosts require approval."""

    from .tools import ToolExecutionResult, ToolPlugin, ToolSpec

    class SshCommandPlugin(ToolPlugin):
        def __init__(self, environment: SshEnvironment) -> None:
            self.environment = environment
            self.spec = ToolSpec(
                name=ssh_tool_name(environment.alias),
                description=_describe(environment)[:10_000],
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200_000,
                            "description": "POSIX shell command to run on the host.",
                        },
                        "cwd": {
                            "type": "string",
                            "maxLength": 1_024,
                            "description": "Remote directory; defaults to the host's configured directory or home.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_COMMAND_TIMEOUT_SECONDS,
                            "default": DEFAULT_COMMAND_TIMEOUT_SECONDS,
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.WORKSPACE_WRITE,
                timeout_seconds=MAX_COMMAND_TIMEOUT_SECONDS + 60,
                requires_approval=environment.command_approval
                != SshEnvironmentApproval.ALLOW,
            )

        async def execute(self, invocation: Any, runner: Any) -> Any:
            del runner
            arguments = invocation.arguments
            cwd = arguments.get("cwd") or self.environment.working_directory
            timeout = int(
                arguments.get("timeout_seconds") or DEFAULT_COMMAND_TIMEOUT_SECONDS
            )
            started = utc_now()
            failure = ""
            exit_code: int | None = None
            stdout = stderr = b""
            timed_out = False
            try:
                exit_code, stdout, stderr, timed_out = await run_remote_command(
                    self.environment.alias,
                    str(arguments["command"]),
                    cwd=cwd,
                    timeout_seconds=timeout,
                    config_path=config_path,
                )
            except (
                Exception
            ) as exc:  # diagnostic-expected: converted to a failed tool receipt
                failure = str(exc) or type(exc).__name__
            completed = utc_now()
            error_text = stderr.decode("utf-8", errors="replace")
            if exit_code == 255 and not failure:
                failure = (
                    f"SSH connection failed ({ssh.classify_ssh_failure(error_text)})."
                )
            if timed_out:
                failure = f"Command timed out after {timeout} seconds and was stopped."
            if failure:
                error_text = f"{error_text.rstrip()}\n{failure}".strip()
            return ToolExecutionResult(
                output={},
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=error_text,
                exit_code=exit_code if exit_code is not None else 1,
                stdout_truncated=len(stdout) >= MAX_STREAM_BYTES,
                stderr_truncated=len(stderr) >= MAX_STREAM_BYTES,
                execution={
                    "runtime": "ssh",
                    "ssh_environment_id": self.environment.id,
                    "ssh_alias": self.environment.alias,
                    "ssh_environment_name": self.environment.label,
                    "cwd": cwd,
                    "started_at": started.isoformat(),
                    "completed_at": completed.isoformat(),
                    "duration_seconds": max(0.0, (completed - started).total_seconds()),
                    "timed_out": timed_out,
                },
            )

    return [SshCommandPlugin(environment) for environment in environments]
