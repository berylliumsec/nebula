"""Core's client for the protected local ``nebula.memory-runner/v1`` service.

The privileged runner lives in :mod:`nebula.v3.host_memory_runner` and runs as a
separate root service. Core never gains privileges: it asks the runner to create
a kernel-enforced memory group per worker job and to start each Host command in
that group, passing only the command's stdio pipes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pwd
import socket
from pathlib import Path
from typing import Any

from .diagnostics import create_diagnostic_task
from .domain import utc_now
from .memory_policy import (
    MemoryEnforcementEvidence,
    MemoryEnforcementUnavailable,
    MemoryLimits,
    MemoryRunnerCapabilities,
    MemoryTerminationReason,
)

HOST_RUNNER_PROFILE_ID = "host-memory-runner"
SOCKET_ENVIRONMENT = "NEBULA_HOST_MEMORY_RUNNER_SOCKET"
SOCKET_DIRECTORY = Path("/run/nebula-memory-runner")
REQUEST_TIMEOUT_SECONDS = 30
UNAVAILABLE_DETAIL = (
    "Host commands need the protected Nebula memory runner service. On Linux, "
    "run `sudo systemctl enable --now nebula-memory-runner@{user}` once, or choose "
    "a verified container execution profile; unrestricted host execution is "
    "unavailable."
)


def _core_user() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        # diagnostic-expected: a host without a passwd entry has no default runner.
        return "<user>"


def default_socket() -> Path:
    """Each Core OS user has its own runner instance and socket."""
    return SOCKET_DIRECTORY / f"{_core_user()}.sock"


def unavailable_detail() -> str:
    return UNAVAILABLE_DETAIL.format(user=_core_user())


def host_group_id(job_id: str) -> str:
    """The runner names each job group by this immutable 64-hex identity."""
    return hashlib.sha256(job_id.encode()).hexdigest()


class HostRunnerError(MemoryEnforcementUnavailable):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


class HostMemoryRunner:
    """Async client for one runner socket. Safe to construct without a runner."""

    profile = None
    memory_profile_id = HOST_RUNNER_PROFILE_ID

    def __init__(self, socket_path: Path | str | None = None):
        self.socket_path = Path(socket_path or default_socket())

    @classmethod
    def from_environment(cls) -> "HostMemoryRunner":
        return cls(os.environ.get(SOCKET_ENVIRONMENT) or default_socket())

    # -- identity used by launch ownership and recovery -------------------
    def memory_fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "kind": "nebula.memory-runner/v1:host",
                    "socket": str(self.socket_path),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    async def memory_identity(self) -> str:
        authority = (await self.info()).get("authority")
        if not isinstance(authority, str) or not 8 <= len(authority) <= 300:
            raise MemoryEnforcementUnavailable(
                "the host memory runner did not report a stable authority"
            )
        return authority

    # -- transport ----------------------------------------------------------
    async def _request(self, operation: str, **fields: Any) -> dict[str, Any]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except (OSError, asyncio.TimeoutError) as error:
            raise HostRunnerError("unavailable", unavailable_detail()) from error
        try:
            writer.write(
                json.dumps({"op": operation, **fields}, separators=(",", ":")).encode()
                + b"\n"
            )
            await writer.drain()
            line = await asyncio.wait_for(
                reader.readline(), timeout=REQUEST_TIMEOUT_SECONDS
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                # diagnostic-expected: the runner closes each request connection.
                pass
        return _result(line)

    async def info(self) -> dict[str, Any]:
        document = await self._request("info")
        if document.get("protocol") != "nebula.memory-runner/v1":
            raise HostRunnerError("unsupported", "the host runner protocol differs")
        return document

    async def available(self) -> tuple[bool, str]:
        try:
            await self.info()
        except MemoryEnforcementUnavailable as error:
            # diagnostic-expected: a missing runner keeps Host work held.
            return False, str(error)
        return True, "Protected host memory runner is available."

    async def capabilities(self) -> MemoryRunnerCapabilities:
        try:
            document = await self.info()
        except MemoryEnforcementUnavailable as error:
            # diagnostic-expected: capabilities report unsupported instead of failing.
            return MemoryRunnerCapabilities(
                environment="local",
                profile_id=HOST_RUNNER_PROFILE_ID,
                detail=str(error),
            )
        capacity = document.get("capacity_bytes")
        enforced = (
            document.get("kernel_enforced") is True
            and document.get("protected_process_tree") is True
        )
        return MemoryRunnerCapabilities(
            environment="local",
            profile_id=HOST_RUNNER_PROFILE_ID,
            kernel_enforced=enforced,
            protected_process_tree=enforced,
            explicit_swap=document.get("explicit_swap") is True,
            pressure_threshold=document.get("pressure_threshold") is True,
            live_update=document.get("live_update") is True,
            capacity_bytes=capacity
            if type(capacity) is int and capacity >= 16 * 1024 * 1024
            else None,
            detail="Host commands run as Core's OS user inside a root-owned cgroup-v2 group with verified memory, swap and pressure limits, no capabilities, no_new_privs and hidden daemon sockets."
            if enforced
            else unavailable_detail(),
        )

    # -- group lifecycle ------------------------------------------------------
    async def create(self, job_id: str, limits: MemoryLimits) -> dict[str, Any]:
        return await self._request(
            "create",
            job_id=job_id,
            max_bytes=limits.max_bytes,
            swap_bytes=limits.swap_bytes,
            high_bytes=limits.high_bytes,
        )

    async def observe(self, job_id: str) -> dict[str, Any] | None:
        report = await self._request("observe", job_id=job_id)
        return None if report.get("absent") else report

    async def update(self, job_id: str, limits: MemoryLimits) -> dict[str, Any]:
        return await self._request(
            "update",
            job_id=job_id,
            max_bytes=limits.max_bytes,
            swap_bytes=limits.swap_bytes,
            high_bytes=limits.high_bytes,
        )

    async def remove(self, job_id: str) -> dict[str, Any]:
        report = await self._request("remove", job_id=job_id)
        if report.get("group_empty") is not True:
            raise HostRunnerError(
                "unverified", "the runner did not verify an empty group"
            )
        return report

    async def spawn(
        self, job_id: str, argv: list[str], cwd: str, environment: dict[str, str]
    ) -> "HostRunnerProcess":
        loop = asyncio.get_running_loop()
        pipes = [os.pipe() for _ in range(3)]
        child = [pipes[0][0], pipes[1][1], pipes[2][1]]
        parent = [pipes[0][1], pipes[1][0], pipes[2][0]]
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        message = (
            json.dumps(
                {
                    "op": "spawn",
                    "job_id": job_id,
                    "argv": argv,
                    "cwd": cwd,
                    "env": environment,
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )

        def send() -> None:
            connection.settimeout(REQUEST_TIMEOUT_SECONDS)
            connection.connect(str(self.socket_path))
            socket.send_fds(connection, [message], child)
            connection.setblocking(False)

        try:
            try:
                await asyncio.to_thread(send)
            except OSError as error:
                raise HostRunnerError("unavailable", unavailable_detail()) from error
            finally:
                for descriptor in child:
                    os.close(descriptor)
            reader, writer = await asyncio.open_unix_connection(sock=connection)
            first = await asyncio.wait_for(
                reader.readline(), timeout=REQUEST_TIMEOUT_SECONDS
            )
            event = _event(first)
            if event.get("event") != "started" or type(event.get("pid")) is not int:
                raise HostRunnerError("launch_failed", "the runner did not start it")
            stdout = asyncio.StreamReader(limit=2**20)
            stderr = asyncio.StreamReader(limit=2**20)
            out_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(stdout),
                os.fdopen(parent[1], "rb", buffering=0),
            )
            err_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(stderr),
                os.fdopen(parent[2], "rb", buffering=0),
            )
            stdin_transport, stdin_protocol = await loop.connect_write_pipe(
                lambda: asyncio.streams.FlowControlMixin(),
                os.fdopen(parent[0], "wb", buffering=0),
            )
            stdin = asyncio.StreamWriter(stdin_transport, stdin_protocol, None, loop)
        except BaseException:
            connection.close()
            for descriptor in parent:
                try:
                    os.close(descriptor)
                except OSError:
                    # diagnostic-expected: a descriptor already owned by a transport.
                    pass
            raise
        return HostRunnerProcess(
            pid=event["pid"],
            control_reader=reader,
            control_writer=writer,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            transports=(out_transport, err_transport),
        )


class HostRunnerProcess:
    """A process the runner started inside a job group."""

    def __init__(
        self,
        *,
        pid: int,
        control_reader: asyncio.StreamReader,
        control_writer: asyncio.StreamWriter,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
        stderr: asyncio.StreamReader,
        transports: tuple[asyncio.ReadTransport, asyncio.ReadTransport],
    ):
        self.pid = pid
        self.control_reader, self.control_writer = control_reader, control_writer
        self.stdin, self.stdout, self.stderr = stdin, stdout, stderr
        self.transports = transports
        self.returncode: int | None = None
        self._exit: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._watcher = create_diagnostic_task(
            self._watch(),
            feature="runtime",
            event_code="runtime.host_runner_exit_watch",
            failure_message="Host command exit supervision stopped unexpectedly.",
            name=f"host-runner-{pid}",
        )

    async def _watch(self) -> None:
        try:
            while True:
                line = await self.control_reader.readline()
                if not line:
                    break
                event = _event(line)
                if (
                    event.get("event") == "exit"
                    and type(event.get("returncode")) is int
                ):
                    code = event["returncode"]
                    self.returncode = 128 - code if code < 0 else code
                    break
        except (OSError, ValueError, MemoryEnforcementUnavailable):
            # diagnostic-expected: a lost runner connection reports exit 255 below.
            pass
        if self.returncode is None:
            # The runner went away. The process group stays charged to the job
            # group until the session's verified cleanup removes it.
            self.returncode = 255
        if not self._exit.done():
            self._exit.set_result(self.returncode)

    async def wait(self) -> int:
        return await asyncio.shield(self._exit)

    async def signal(self, name: str) -> None:
        if self.control_writer.is_closing():
            return
        try:
            self.control_writer.write(
                json.dumps({"op": "signal", "signal": name}).encode() + b"\n"
            )
            await self.control_writer.drain()
        except (OSError, ConnectionError):
            # diagnostic-expected: the runner already closed this process's channel.
            pass

    async def close(self) -> None:
        self._watcher.cancel()
        await asyncio.gather(self._watcher, return_exceptions=True)
        if not self._exit.done():
            self._exit.set_result(255)
        for transport in self.transports:
            transport.close()
        self.stdin.close()
        self.control_writer.close()


def _event(line: bytes) -> dict[str, Any]:
    if not line or len(line) > 1_000_000:
        raise HostRunnerError("unavailable", "the host memory runner disconnected")
    document = json.loads(line)
    if not isinstance(document, dict):
        raise HostRunnerError("invalid", "the host memory runner sent invalid data")
    if document.get("ok") is False:
        raise HostRunnerError(
            str(document.get("error") or "failed"),
            str(document.get("detail") or "the host memory runner refused the request"),
        )
    return document


def _result(line: bytes) -> dict[str, Any]:
    document = _event(line)
    result = document.get("result")
    if document.get("ok") is not True or not isinstance(result, dict):
        raise HostRunnerError("invalid", "the host memory runner sent no result")
    return result


def host_limits(report: dict[str, Any]) -> MemoryLimits:
    return MemoryLimits(
        max_bytes=report["max_bytes"],
        swap_bytes=report["swap_bytes"],
        high_bytes=report.get("high_bytes"),
    )


def host_evidence(
    report: dict[str, Any],
    job_id: str,
    *,
    termination_reason: MemoryTerminationReason,
    detail: str,
    fallback_limits: MemoryLimits | None = None,
    peak_bytes: int | None = None,
) -> MemoryEnforcementEvidence:
    """Translate a runner report into Core's enforcement evidence."""
    if "max_bytes" in report:
        applied = host_limits(report)
    elif fallback_limits is not None:
        applied = fallback_limits
    else:
        raise HostRunnerError("unverified", "the runner report has no limits")
    oom = report.get("oom_kill_count")
    oom = oom if type(oom) is int and oom >= 0 else None
    empty = report.get("group_empty") is True
    reported_peak = report.get("peak_bytes")
    return MemoryEnforcementEvidence(
        job_id=job_id,
        group_id=host_group_id(job_id),
        applied=applied,
        protected_process_tree=True,
        current_bytes=0 if empty else report.get("current_bytes"),
        peak_bytes=reported_peak if type(reported_peak) is int else peak_bytes,
        observed_at=utc_now().isoformat(),
        oom_kill_count=oom,
        oom_victim_confirmed=bool(oom),
        group_empty=empty,
        termination_reason=termination_reason,
        detail=detail,
    )
