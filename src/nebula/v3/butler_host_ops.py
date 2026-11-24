"""Butler supervisor host operations: audited shell, SSH, signals and lab scripts.

Every tool here re-reads the operator policy on each call (the kill switch),
screens the command against the code-enforced guardrails in
``butler_supervisor_policy``, runs it with a timeout, and appends one durable
audit record whether the command ran, failed or was refused. The record holds
the host, the redacted command, the exit code, the duration and SHA-256
digests of stdout and stderr. An optional ``event_sink`` also records a Butler
event for the project.

SECURITY NOTE: commands run as the Core service user. Where that user has
passwordless sudo, ``NEBULA_BUTLER_SUPERVISOR_ALLOW_SUDO=1`` is equivalent to
unrestricted root. It stays off by default; the runner control tool covers the
operator's configured sudoers control scripts without it.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal as signal_module
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .butler_supervisor_policy import (
    MAX_OUTPUT_BYTES,
    GuardrailViolation,
    LabHost,
    SupervisorPolicy,
    digest,
    load_policy,
    redact,
    screen_command,
    write_audit,
)
from .domain import RiskClass
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


EventSink = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1_800
_SIGNALS = {"STOP", "CONT", "TERM", "INT", "HUP", "KILL", "USR1", "USR2"}
_GATE_RESULT = re.compile(r"\bRESULT\s+(PASS|FAIL)\b", re.IGNORECASE)

_HOST = {"type": "string", "minLength": 1, "maxLength": 64}
_TIMEOUT = {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT}
_REASON = {"type": "string", "minLength": 3, "maxLength": 500}
_OUT = {"type": "object", "additionalProperties": True}

_SHELL_SCHEMA = {
    "type": "object",
    "properties": {
        "host": _HOST,
        "command": {"type": "string", "minLength": 1, "maxLength": 8_000},
        "cwd": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "timeout_seconds": _TIMEOUT,
        "reason": _REASON,
    },
    "required": ["host", "command", "reason"],
    "additionalProperties": False,
}
_SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "host": _HOST,
        "pid": {"type": "integer", "minimum": 2, "maximum": 4_194_304},
        "signal": {"type": "string", "enum": sorted(_SIGNALS)},
        "expected_command_substring": {
            "type": "string",
            "minLength": 3,
            "maxLength": 200,
        },
        "reason": _REASON,
    },
    "required": ["host", "pid", "signal", "expected_command_substring", "reason"],
    "additionalProperties": False,
}
_RUNNER_SCHEMA = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "enum": ["runner", "ingress"]},
        "verb": {"type": "string", "enum": ["start", "stop", "restart"]},
        "reason": _REASON,
    },
    "required": ["service", "verb", "reason"],
    "additionalProperties": False,
}
_PUSH_SCHEMA = {
    "type": "object",
    "properties": {
        "step": {"type": "string", "enum": ["prepare", "publish"]},
        "timeout_seconds": _TIMEOUT,
        "reason": _REASON,
    },
    "required": ["step", "reason"],
    "additionalProperties": False,
}
_GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "args": {
            "type": "array",
            "items": {"type": "string", "maxLength": 1_000},
            "maxItems": 32,
        },
        "timeout_seconds": _TIMEOUT,
        "reason": _REASON,
    },
    "required": ["path", "reason"],
    "additionalProperties": False,
}


def _truncate(policy: SupervisorPolicy, data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8", "replace")
    truncated = len(data) > MAX_OUTPUT_BYTES
    if truncated:
        text = (
            text[: MAX_OUTPUT_BYTES // 2]
            + "\n…[truncated]…\n"
            + text[-MAX_OUTPUT_BYTES // 2 :]
        )
    return redact(policy, text), truncated


def _ssh_argv(host: LabHost, command: str) -> list[str]:
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
    ]
    if host.identity_file:
        argv += ["-i", host.identity_file, "-o", "IdentitiesOnly=yes"]
    if host.port:
        argv += ["-p", str(host.port)]
    target = f"{host.user}@{host.address}" if host.user else str(host.address)
    return [*argv, target, "--", command]


class ButlerHostOps:
    """Audited supervisor host tools for Butler."""

    def __init__(
        self,
        *,
        project_id: str,
        actor: str,
        policy_loader: Callable[[], SupervisorPolicy] = load_policy,
        event_sink: EventSink | None = None,
    ) -> None:
        self.project_id = project_id
        self.actor = actor
        self._policy_loader = policy_loader
        self._event_sink = event_sink

    def _policy(self) -> SupervisorPolicy:
        policy = self._policy_loader()
        policy.require_enabled()
        return policy

    async def _record(
        self, policy: SupervisorPolicy, record: dict[str, Any]
    ) -> dict[str, Any]:
        entry = write_audit(
            policy,
            {"project_id": self.project_id, "actor": self.actor, **record},
        )
        if self._event_sink is not None:
            try:
                await self._event_sink(entry)
            except Exception:  # diagnostic-expected: the durable log already holds it.
                pass
        return entry

    async def _refuse(
        self,
        policy: SupervisorPolicy,
        tool: str,
        error: GuardrailViolation,
        **fields: Any,
    ) -> None:
        await self._record(
            policy,
            {
                "tool": tool,
                "outcome": "refused",
                "rule": error.rule,
                "detail": error.detail,
                **fields,
            },
        )
        raise error

    async def _exec(
        self,
        policy: SupervisorPolicy,
        *,
        tool: str,
        host: LabHost,
        argv: list[str],
        display: str,
        timeout: int,
        cwd: str | None,
        reason: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        env = {
            key: value
            for key, value in os.environ.items()
            # Core's own credentials never reach a supervisor command.
            if not key.startswith(
                ("NEBULA_V3_API", "NEBULA_V3_DATABASE", "ANTHROPIC_", "OPENAI_")
            )
            and "TOKEN" not in key
            and "SECRET" not in key
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            await self._record(
                policy,
                {
                    "tool": tool,
                    "host": host.name,
                    "command": display,
                    "reason": reason,
                    "outcome": "spawn_failed",
                    "detail": str(exc),
                },
            )
            raise
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal_module.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = await process.communicate()
        duration = round(time.monotonic() - started, 3)
        out_text, out_cut = _truncate(policy, stdout)
        err_text, err_cut = _truncate(policy, stderr)
        entry = await self._record(
            policy,
            {
                "tool": tool,
                "host": host.name,
                "command": display,
                "cwd": cwd,
                "reason": reason,
                "outcome": "timeout" if timed_out else "completed",
                "exit_code": process.returncode,
                "duration_seconds": duration,
                "stdout_sha256": digest(stdout),
                "stderr_sha256": digest(stderr),
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
            },
        )
        return {
            "host": host.name,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "duration_seconds": duration,
            "stdout": out_text,
            "stderr": err_text,
            "stdout_truncated": out_cut,
            "stderr_truncated": err_cut,
            "audit": {
                "at": entry["at"],
                "stdout_sha256": entry["stdout_sha256"],
                "stderr_sha256": entry["stderr_sha256"],
            },
        }

    async def _run_on(
        self,
        policy: SupervisorPolicy,
        *,
        tool: str,
        host: LabHost,
        command: str,
        timeout: int,
        cwd: str | None,
        reason: str,
    ) -> dict[str, Any]:
        try:
            screen_command(policy, command, host=host, cwd=cwd)
        except GuardrailViolation as error:
            await self._refuse(policy, tool, error, host=host.name, command=command)
        if host.local:
            argv = ["bash", "-o", "pipefail", "-c", command]
            run_cwd = cwd
        else:
            remote = f"cd {_quote(cwd)} && {command}" if cwd else command
            argv = _ssh_argv(host, remote)
            run_cwd = None
        return await self._exec(
            policy,
            tool=tool,
            host=host,
            argv=argv,
            display=command,
            timeout=timeout,
            cwd=run_cwd,
            reason=reason,
        )

    # ---- tools -----------------------------------------------------------

    async def shell(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy()
        try:
            host = policy.host(arguments["host"])
        except GuardrailViolation as error:
            await self._refuse(
                policy,
                "shell",
                error,
                host=arguments["host"],
                command=arguments["command"],
            )
        return await self._run_on(
            policy,
            tool="shell",
            host=host,
            command=arguments["command"],
            timeout=int(arguments.get("timeout_seconds") or DEFAULT_TIMEOUT),
            cwd=arguments.get("cwd"),
            reason=arguments["reason"],
        )

    async def signal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy()
        host = policy.host(arguments["host"])
        pid = int(arguments["pid"])
        name = arguments["signal"]
        if name not in _SIGNALS:
            raise GuardrailViolation("invalid_signal", name)
        if host.local and pid in {os.getpid(), os.getppid()}:
            await self._refuse(
                policy,
                "signal",
                GuardrailViolation("protected_process", "Core may not signal itself"),
                host=host.name,
                command=f"kill -s {name} {pid}",
            )
        expected = arguments["expected_command_substring"]
        if not re.fullmatch(r"[A-Za-z0-9 ._/:=@+-]{3,200}", expected):
            raise GuardrailViolation("invalid_argument", "expected_command_substring")
        # Re-check the target's identity at signal time so a reused PID is never hit.
        command = (
            f"cmd=$(ps -o args= -p {pid}) || exit 3; "
            # expected is validated to [A-Za-z0-9 ._/:=@+-], so single quotes are safe.
            f"case \"$cmd\" in *'{expected}'*) kill -s {name} {pid};; "
            '*) echo "pid identity mismatch: $cmd" >&2; exit 4;; esac'
        )
        return await self._run_on(
            policy,
            tool="signal",
            host=host,
            command=command,
            timeout=30,
            cwd=None,
            reason=arguments["reason"],
        )

    async def runner_control(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy()
        local = next((h for h in policy.hosts.values() if h.local), None)
        if local is None:
            raise GuardrailViolation(
                "host_not_allowlisted", "no local lab host configured"
            )
        script = (
            policy.runner_control
            if arguments["service"] == "runner"
            else policy.ingress_control
        )
        if not script:
            raise GuardrailViolation(
                "not_configured",
                "set NEBULA_BUTLER_SUPERVISOR_RUNNER_CONTROL or _INGRESS_CONTROL",
            )
        verb = arguments["verb"]
        if verb not in {"start", "stop", "restart"}:
            raise GuardrailViolation("invalid_argument", verb)
        if arguments["service"] == "ingress" and verb == "restart":
            raise GuardrailViolation(
                "invalid_argument", "the ingress control script supports start and stop"
            )
        # The operator's sudoers entry names this exact script and verb.
        argv = ["sudo", "-n", script, verb]
        return await self._exec(
            policy,
            tool="runner_control",
            host=local,
            argv=argv,
            display=" ".join(argv),
            timeout=300,
            cwd=None,
            reason=arguments["reason"],
        )

    async def repo_publish(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy()
        local = next((h for h in policy.hosts.values() if h.local), None)
        script = (
            policy.prepare_script
            if arguments["step"] == "prepare"
            else policy.publish_script
        )
        if local is None or not script:
            raise GuardrailViolation(
                "not_configured",
                "set NEBULA_BUTLER_SUPERVISOR_PUBLISH_SCRIPT and _PREPARE_SCRIPT",
            )
        argv = ["bash", script]
        return await self._exec(
            policy,
            tool=f"repo_{arguments['step']}",
            host=local,
            argv=argv,
            display=" ".join(argv),
            timeout=int(arguments.get("timeout_seconds") or 1_200),
            cwd=None,
            reason=arguments["reason"],
        )

    async def run_gate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy()
        local = next((h for h in policy.hosts.values() if h.local), None)
        if local is None:
            raise GuardrailViolation(
                "host_not_allowlisted", "no local lab host configured"
            )
        path = Path(os.path.realpath(arguments["path"]))
        root = next(
            (
                Path(os.path.realpath(item))
                for item in policy.gate_roots
                if path.is_relative_to(os.path.realpath(item))
            ),
            None,
        )
        try:
            if root is None:
                raise GuardrailViolation(
                    "gate_not_allowlisted",
                    "gate scripts must live under NEBULA_BUTLER_SUPERVISOR_GATE_ROOTS",
                )
            if path.suffix not in {".py", ".sh"} or not path.is_file():
                raise GuardrailViolation(
                    "gate_invalid", "gate must be a .py or .sh file"
                )
            _require_committed(path)
        except GuardrailViolation as error:
            await self._refuse(policy, "run_gate", error, command=str(path))
        args = [str(item) for item in arguments.get("args") or []]
        if any(
            item.startswith("-") and "=" not in item and len(item) > 40 for item in args
        ):
            raise GuardrailViolation("invalid_argument", "gate argument")
        for item in args:
            screen_command(policy, item, host=local)
        interpreter = ["python3", "-I"] if path.suffix == ".py" else ["bash"]
        argv = [*interpreter, str(path), *args]
        result = await self._exec(
            policy,
            tool="run_gate",
            host=local,
            argv=argv,
            display=" ".join(argv),
            timeout=int(arguments.get("timeout_seconds") or 600),
            cwd=str(path.parent),
            reason=arguments["reason"],
        )
        match = None
        for match in _GATE_RESULT.finditer(result["stdout"]):
            pass
        stated = match.group(1).upper() if match else None
        result["gate"] = {
            "path": str(path),
            "commit": _head_for(path),
            "stated_result": stated,
            "passed": result["exit_code"] == 0
            and not result["timed_out"]
            and stated != "FAIL",
        }
        return result

    def actions(self) -> list[AnalysisTool]:
        write = RiskClass.WORKSPACE_WRITE
        once = IdempotencyBehavior.NON_IDEMPOTENT
        return [
            AnalysisTool(
                ToolSpec(
                    name="nebula.supervisor.shell",
                    description=(
                        "Run one shell command on an allowlisted lab host (the Core "
                        "host or a lab host over SSH) and return its exit code and "
                        "output. Guardrails refuse non-lab network targets, radio "
                        "tools off rf_lab hosts, writes to the pull-only production "
                        "mirror, git worktrees, operator-only actions (outside "
                        "uploads and reports, accounts, platform integrity, "
                        "firmware, power) and, unless the operator "
                        "enabled it, sudo. Every call is audited. State the reason."
                    ),
                    input_schema=_SHELL_SCHEMA,
                    output_schema=_OUT,
                    risk_class=write,
                    idempotency=once,
                ),
                self.shell,
            ),
            AnalysisTool(
                ToolSpec(
                    name="nebula.supervisor.signal",
                    description=(
                        "Send one signal (STOP, CONT, TERM, INT, HUP, KILL, USR1, "
                        "USR2) to one process on a lab host. The process's command "
                        "line must contain expected_command_substring at signal time, "
                        "so a reused PID is never hit. Audited."
                    ),
                    input_schema=_SIGNAL_SCHEMA,
                    output_schema=_OUT,
                    risk_class=write,
                    idempotency=once,
                ),
                self.signal,
            ),
            AnalysisTool(
                ToolSpec(
                    name="nebula.supervisor.runner_control",
                    description=(
                        "Start, stop or restart the runner service, or start or "
                        "stop the ingress service, through the operator-configured "
                        "sudoers control scripts on the Core host. Audited."
                    ),
                    input_schema=_RUNNER_SCHEMA,
                    output_schema=_OUT,
                    risk_class=write,
                    idempotency=once,
                ),
                self.runner_control,
            ),
            AnalysisTool(
                ToolSpec(
                    name="nebula.supervisor.repo_publish",
                    description=(
                        "Run the operator's configured repository publish flow: "
                        "'prepare' runs the prepare script (for example a large-file "
                        "filter); 'publish' runs the publish script (for example a "
                        "secrets scan, merge and push). Only those two scripts run. "
                        "Audited."
                    ),
                    input_schema=_PUSH_SCHEMA,
                    output_schema=_OUT,
                    risk_class=write,
                    idempotency=once,
                ),
                self.repo_publish,
            ),
            AnalysisTool(
                ToolSpec(
                    name="nebula.supervisor.run_gate",
                    description=(
                        "Run one committed deterministic gate script (.py or .sh) "
                        "from an allowlisted gate root, with argument list only. The "
                        "script must be tracked by git with no uncommitted change. "
                        "Returns exit code, output and gate.passed (exit 0 and no "
                        "'RESULT FAIL' line). Act on gate.passed, not on prose."
                    ),
                    input_schema=_GATE_SCHEMA,
                    output_schema=_OUT,
                    risk_class=write,
                    idempotency=once,
                ),
                self.run_gate,
            ),
        ]


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path.parent), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _require_committed(path: Path) -> None:
    tracked = _git(path, "ls-files", "--error-unmatch", "--", path.name)
    if tracked.returncode != 0:
        raise GuardrailViolation("gate_uncommitted", f"{path} is not tracked by git")
    clean = _git(path, "diff", "--quiet", "HEAD", "--", path.name)
    if clean.returncode != 0:
        raise GuardrailViolation(
            "gate_uncommitted", f"{path} has uncommitted changes; commit it first"
        )


def _head_for(path: Path) -> str | None:
    result = _git(path, "log", "-1", "--format=%H", "--", path.name)
    return result.stdout.strip() or None
