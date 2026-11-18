"""Protected local memory runner (``nebula.memory-runner/v1``) for Host commands.

This module is the privileged half of Host-mode memory enforcement. It runs as a
separate root service that owns a delegated cgroup-v2 subtree. Core connects
over a Unix socket and asks it to create one memory group per worker job and to
start processes inside that group as Core's OS user.

It deliberately imports only the standard library and must be started with an
isolated interpreter (``python3 -I host_memory_runner.py``) so that nothing on
Core's import path executes as root.

Containment guarantees for every spawned process:

* It is moved into the job group before it executes anything but a fixed,
  root-owned stage script, so every descendant is charged to that group.
* The group directories are root-owned. Migrating out requires write access to
  a common ancestor's ``cgroup.procs``, which an unprivileged process lacks.
* The workload runs with no capabilities, an empty bounding set, cleared
  supplementary groups and ``no_new_privs`` (setuid helpers such as ``sudo``,
  ``at`` or ``crontab`` cannot escalate).
* A private mount namespace hides the runner socket, the user's systemd/D-Bus
  runtime directory and container daemon sockets, so the workload cannot ask a
  daemon outside its group to run work on its behalf. Clients whose process is
  inside a job group are refused regardless.
* Cleanup uses ``cgroup.kill`` and only reports an empty group after the kernel
  reports ``populated 0`` and the directory has been removed.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

PROTOCOL = "nebula.memory-runner/v1"
CGROUP_MOUNT = Path("/sys/fs/cgroup")
DEFAULT_SOCKET = Path("/run/nebula-memory-runner/runner.sock")
DEFAULT_STATE = Path("/var/lib/nebula-memory-runner")
MAX_REQUEST_BYTES = 256 * 1024
MAX_JOB_BYTES = 1 << 50
JOB_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT}
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
# Directories and sockets that would let a workload delegate work to a daemon
# outside its memory group. Missing paths are skipped.
HIDDEN_DIRECTORIES = ("/run/user/{uid}", "/run/containerd", "/run/podman")
HIDDEN_SOCKETS = ("/run/docker.sock", "/var/run/docker.sock")

# Stage 1 runs as root inside a private mount namespace. It blocks until the
# runner has moved it into the job group, hides daemon control paths, then
# irrevocably drops privileges before stage 2 can run.
STAGE_SCRIPT = r"""#!/bin/sh
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
gate_path=$1; uid=$2; gid=$3; socket_dir=$4; shift 4
exec 3<"$gate_path"
IFS= read -r gate <&3
[ "$gate" = enter ] || exit 125
/usr/bin/mount --make-rprivate /
for target in "/run/user/$uid" "$socket_dir" /run/containerd /run/podman; do
  if [ -d "$target" ] && [ ! -L "$target" ]; then
    /usr/bin/mount -t tmpfs -o size=4k,mode=0755,nosuid,nodev,noexec nebula-hidden "$target"
  fi
done
for target in /run/docker.sock /var/run/docker.sock; do
  if [ -S "$target" ] && [ ! -L "$target" ]; then
    /usr/bin/mount --bind /dev/null "$target"
  fi
done
exec /usr/bin/setpriv --reuid="$uid" --regid="$gid" --clear-groups \
  --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \
  -- /bin/sh -c '
IFS= read -r gate <&3
[ "$gate" = run ] || exit 125
directory=$1; shift
exec 3<&- 4>&-
cd -- "$directory"
exec /usr/bin/env -i "$@"
' nebula-stage "$@"
"""
# The runner inspects the blocked stage through /proc while it waits at each
# gate. The gate is a root-owned FIFO opened by stage 1 as root and inherited by
# stage 2 on descriptor 3, so the workload never needs access to its path.


class RunnerError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _read(path: Path) -> str:
    with path.open("r", encoding="ascii") as handle:
        return handle.read().strip()


def _write(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY)
    try:
        os.write(descriptor, value.encode("ascii"))
    finally:
        os.close(descriptor)


def _limit(value: str) -> int | None:
    return None if value == "max" else int(value)


def _keyed(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in _read(path).splitlines():
        key, _, value = line.partition(" ")
        if value.isdecimal():
            result[key] = int(value)
    return result


def own_cgroup() -> Path:
    for line in _read(Path("/proc/self/cgroup")).splitlines():
        hierarchy, _, path = line.partition("::")
        if hierarchy == "0":
            return CGROUP_MOUNT / path.lstrip("/")
    raise RunnerError("unsupported", "cgroup v2 unified hierarchy is required")


def process_cgroup(pid: int) -> Path | None:
    try:
        content = _read(Path(f"/proc/{pid}/cgroup"))
    except OSError:
        # diagnostic-expected: an exited process has no cgroup to report.
        return None
    for line in content.splitlines():
        if line.startswith("0::"):
            return CGROUP_MOUNT / line[3:].lstrip("/")
    return None


def _status(pid: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _read(Path(f"/proc/{pid}/status")).splitlines():
        key, _, value = line.partition(":")
        result[key] = value.strip()
    return result


def group_name(job_id: str) -> str:
    # A 64-hex name matches Core's immutable launch-group identity format.
    return hashlib.sha256(job_id.encode()).hexdigest()


class Runner:
    def __init__(
        self,
        *,
        root: Path,
        client_uid: int,
        client_gid: int,
        socket_path: Path,
        state: Path,
    ):
        self.root, self.jobs = root, root / "jobs"
        self.client_uid, self.client_gid = client_uid, client_gid
        self.socket_path, self.state = socket_path, state
        self.lock = threading.Lock()
        self.processes: dict[int, subprocess.Popen] = {}
        self.authority = ""
        self.stage_path = state / "stage.sh"
        self.swap_supported = False

    # -- setup -----------------------------------------------------------
    def prepare(self) -> None:
        if os.geteuid() != 0:
            raise RunnerError("unsupported", "the memory runner must run as root")
        if _read(Path("/proc/filesystems")).find("cgroup2") < 0:
            raise RunnerError("unsupported", "cgroup v2 is unavailable")
        controllers = _read(self.root / "cgroup.controllers").split()
        if "memory" not in controllers or "pids" not in controllers:
            raise RunnerError(
                "unsupported",
                "the runner cgroup was not delegated the memory and pids controllers",
            )
        # A cgroup with processes cannot enable controllers for children.
        service = self.root / "runner"
        service.mkdir(exist_ok=True)
        _write(service / "cgroup.procs", str(os.getpid()))
        _write(self.root / "cgroup.subtree_control", "+memory +pids")
        self.jobs.mkdir(exist_ok=True)
        os.chmod(self.jobs, 0o755)
        _write(self.jobs / "cgroup.subtree_control", "+memory +pids")
        probe = self.jobs / "probe-swap"
        probe.mkdir(exist_ok=True)
        self.swap_supported = (probe / "memory.swap.max").exists()
        probe.rmdir()
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        instance_path = self.state / "instance"
        if not instance_path.exists():
            instance_path.write_text(uuid.uuid4().hex, encoding="ascii")
        instance = _read(instance_path)
        # Stable across runner restarts and reboots on this host. Group absence
        # under this root is kernel proof of cleanup: a populated cgroup cannot
        # be removed, and cgroupfs does not survive a reboot.
        self.authority = hashlib.sha256(f"{instance}\0{self.root}".encode()).hexdigest()
        temporary = self.state / ".stage.tmp"
        temporary.write_text(STAGE_SCRIPT, encoding="ascii")
        os.chmod(temporary, 0o755)
        os.replace(temporary, self.stage_path)
        for tool in ("/usr/bin/setpriv", "/usr/bin/unshare", "/usr/bin/mount"):
            if not os.access(tool, os.X_OK):
                raise RunnerError("unsupported", f"{tool} is required")

    # -- operations ------------------------------------------------------
    def info(self) -> dict[str, Any]:
        meminfo = _read(Path("/proc/meminfo"))
        total = next(
            int(line.split()[1]) * 1024
            for line in meminfo.splitlines()
            if line.startswith("MemTotal:")
        )
        return {
            "protocol": PROTOCOL,
            "authority": "host-runner:" + self.authority,
            "kernel_enforced": True,
            "protected_process_tree": True,
            "explicit_swap": self.swap_supported,
            "pressure_threshold": True,
            "live_update": True,
            "capacity_bytes": total,
        }

    def _group(self, job_id: Any) -> Path:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise RunnerError("invalid", "job identity is invalid")
        return self.jobs / group_name(job_id)

    @staticmethod
    def _limits(request: dict[str, Any]) -> tuple[int, int, int | None]:
        maximum, swap, high = (
            request.get("max_bytes"),
            request.get("swap_bytes"),
            request.get("high_bytes"),
        )
        if type(maximum) is not int or not 16 * 1024 * 1024 <= maximum <= MAX_JOB_BYTES:
            raise RunnerError("invalid", "max_bytes is out of range")
        if type(swap) is not int or not 0 <= swap <= MAX_JOB_BYTES:
            raise RunnerError("invalid", "swap_bytes is out of range")
        if high is not None and (type(high) is not int or not 0 < high <= maximum):
            raise RunnerError("invalid", "high_bytes is out of range")
        return maximum, swap, high

    def _apply(self, group: Path, maximum: int, swap: int, high: int | None) -> None:
        if swap and not self.swap_supported:
            raise RunnerError("unsupported", "swap accounting is unavailable")
        _write(group / "memory.high", "max" if high is None else str(high))
        _write(group / "memory.max", str(maximum))
        if self.swap_supported:
            _write(group / "memory.swap.max", str(swap))

    def _observe(self, group: Path, job_id: str) -> dict[str, Any]:
        swap = int(_read(group / "memory.swap.max")) if self.swap_supported else 0
        events = _keyed(group / "memory.events")
        peak_path = group / "memory.peak"
        return {
            "job_id": job_id,
            "group_id": group.name,
            "max_bytes": int(_read(group / "memory.max")),
            "swap_bytes": swap,
            "high_bytes": _limit(_read(group / "memory.high")),
            "current_bytes": int(_read(group / "memory.current")),
            "peak_bytes": int(_read(peak_path)) if peak_path.exists() else None,
            "oom_kill_count": events.get("oom_kill", 0),
            "populated": _keyed(group / "cgroup.events").get("populated", 0) == 1,
        }

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        group = self._group(request.get("job_id"))
        maximum, swap, high = self._limits(request)
        with self.lock:
            try:
                group.mkdir(mode=0o755)
            except FileExistsError as error:
                raise RunnerError(
                    "conflict", "this job already owns a memory group"
                ) from error
            try:
                self._apply(group, maximum, swap, high)
                observed = self._observe(group, request["job_id"])
            except BaseException:
                group.rmdir()
                raise
        if (observed["max_bytes"], observed["swap_bytes"], observed["high_bytes"]) != (
            maximum,
            swap,
            high,
        ):
            self._remove(group, request["job_id"])
            raise RunnerError("unverified", "kernel readback differs from the request")
        return observed

    def observe(self, request: dict[str, Any]) -> dict[str, Any]:
        group = self._group(request.get("job_id"))
        if not group.is_dir():
            return {"absent": True, "group_id": group.name}
        return self._observe(group, request["job_id"])

    def update(self, request: dict[str, Any]) -> dict[str, Any]:
        group = self._group(request.get("job_id"))
        maximum, swap, high = self._limits(request)
        if not group.is_dir():
            raise RunnerError("absent", "the job group no longer exists")
        try:
            self._apply(group, maximum, swap, high)
        except OSError as error:
            raise RunnerError(
                "rejected", f"kernel rejected the limit: {error}"
            ) from error
        return self._observe(group, request["job_id"])

    def remove(self, request: dict[str, Any]) -> dict[str, Any]:
        group = self._group(request.get("job_id"))
        return self._remove(group, request["job_id"])

    def _remove(self, group: Path, job_id: str) -> dict[str, Any]:
        if not group.is_dir():
            return {"absent": True, "group_id": group.name, "group_empty": True}
        final = self._observe(group, job_id)
        _write(group / "cgroup.kill", "1")
        deadline = time.monotonic() + 15
        while _keyed(group / "cgroup.events").get("populated", 0):
            if time.monotonic() > deadline:
                raise RunnerError(
                    "unverified", "group processes did not exit after cgroup.kill"
                )
            time.sleep(0.02)
        final["oom_kill_count"] = _keyed(group / "memory.events").get("oom_kill", 0)
        peak = group / "memory.peak"
        if peak.exists():
            final["peak_bytes"] = int(_read(peak))
        for _ in range(250):
            try:
                group.rmdir()
                break
            except OSError as error:
                if error.errno != errno.EBUSY:
                    raise
                time.sleep(0.02)
        if group.exists():
            raise RunnerError("unverified", "the emptied group could not be removed")
        final.update(populated=False, group_empty=True, current_bytes=0)
        return final

    def spawn(self, request: dict[str, Any], fds: list[int], connection) -> None:
        group = self._group(request.get("job_id"))
        argv, cwd, environment = (
            request.get("argv"),
            request.get("cwd"),
            request.get("env") or {},
        )
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 4096
            or not all(isinstance(item, str) and "\0" not in item for item in argv)
        ):
            raise RunnerError("invalid", "argv is invalid")
        if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd:
            raise RunnerError("invalid", "cwd must be absolute")
        if not isinstance(environment, dict) or not all(
            isinstance(key, str)
            and ENV_KEY.fullmatch(key)
            and isinstance(value, str)
            and "\0" not in value
            for key, value in environment.items()
        ):
            raise RunnerError("invalid", "environment is invalid")
        if len(fds) != 3:
            raise RunnerError("invalid", "stdin, stdout and stderr are required")
        if "=" in argv[0]:
            raise RunnerError("invalid", "the program path cannot contain '='")
        if not group.is_dir():
            raise RunnerError("absent", "the job group does not exist")
        gates = self.state / "gates"
        gates.mkdir(mode=0o700, exist_ok=True)
        gate_path = gates / uuid.uuid4().hex
        os.mkfifo(gate_path, 0o600)
        # O_RDWR never blocks on a FIFO and keeps a writer open for the stage.
        gate_write = os.open(gate_path, os.O_RDWR)
        try:
            process = subprocess.Popen(
                [
                    "/usr/bin/unshare",
                    "--mount",
                    "--propagation",
                    "private",
                    "--",
                    "/bin/sh",
                    str(self.stage_path),
                    str(gate_path),
                    str(self.client_uid),
                    str(self.client_gid),
                    str(self.socket_path.parent),
                    cwd,
                    *(f"{key}={value}" for key, value in environment.items()),
                    *argv,
                ],
                stdin=fds[0],
                stdout=fds[1],
                stderr=fds[2],
                close_fds=True,
                cwd="/",
                env={"PATH": SAFE_PATH, "LANG": "C"},
                start_new_session=True,
            )
        except BaseException:
            os.close(gate_write)
            gate_path.unlink()
            raise
        finally:
            for descriptor in fds:
                os.close(descriptor)
        try:
            self._admit(process, group, gate_write)
        except BaseException:
            self._kill(process)
            raise
        finally:
            os.close(gate_write)
            gate_path.unlink(missing_ok=True)
        with self.lock:
            self.processes[process.pid] = process
        _send(connection, {"event": "started", "pid": process.pid})
        commands = threading.Thread(
            target=self._commands, args=(connection, group, process.pid), daemon=True
        )
        commands.start()
        code = process.wait()
        with self.lock:
            self.processes.pop(process.pid, None)
        try:
            _send(connection, {"event": "exit", "returncode": code})
        except OSError:
            # diagnostic-expected: a vanished client cannot receive the exit event.
            # The client went away; the job group still owns any descendants.
            return
        # Keep accepting signals for the process group (background children)
        # until the client closes the channel.
        commands.join()

    def _admit(self, process: subprocess.Popen, group: Path, gate: int) -> None:
        # Wait until unshare has exec'd the stage script, which blocks at gate 1.
        deadline = time.monotonic() + 10
        while True:
            try:
                command = Path(f"/proc/{process.pid}/cmdline").read_bytes()
            except OSError as error:
                raise RunnerError("launch_failed", "stage process exited") from error
            if command.split(b"\0")[:2] == [b"/bin/sh", str(self.stage_path).encode()]:
                break
            if process.poll() is not None or time.monotonic() > deadline:
                raise RunnerError("launch_failed", "stage process did not start")
            time.sleep(0.005)
        _write(group / "cgroup.procs", str(process.pid))
        if process_cgroup(process.pid) != group:
            raise RunnerError("unverified", "process did not enter the job group")
        os.write(gate, b"enter\n")
        # Wait for the unprivileged stage to block at gate 2, then verify it.
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                raise RunnerError("launch_failed", "stage exited before release")
            try:
                status = _status(process.pid)
                command = Path(f"/proc/{process.pid}/cmdline").read_bytes()
            except OSError as error:
                raise RunnerError(
                    "launch_failed", "stage exited before release"
                ) from error
            if command.startswith(b"/bin/sh\0-c\0") and status.get("Uid", "").split()[
                :1
            ] == [str(self.client_uid)]:
                break
            if time.monotonic() > deadline:
                raise RunnerError("launch_failed", "privilege drop did not complete")
            time.sleep(0.005)
        uids = {int(value) for value in status["Uid"].split()}
        gids = {int(value) for value in status["Gid"].split()}
        groups = set(status.get("Groups", "").split())
        if (
            uids != {self.client_uid}
            or gids != {self.client_gid}
            or groups - {str(self.client_gid)}
            or int(status["CapEff"], 16)
            or int(status["CapPrm"], 16)
            or int(status["CapBnd"], 16)
            or status.get("NoNewPrivs") != "1"
            or process_cgroup(process.pid) != group
        ):
            raise RunnerError("unverified", "workload privileges could not be verified")
        os.write(gate, b"run\n")

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # diagnostic-expected: the stage already exited before the kill.
            pass
        process.wait()

    @staticmethod
    def _signal_group(group: Path, process_group: int, value: int) -> None:
        """Signal one process group, but only its members inside this job."""
        try:
            members = _read(group / "cgroup.procs").split()
        except OSError:
            # diagnostic-expected: the job group vanished; nothing remains to signal.
            return
        for member in members:
            try:
                fields = Path(f"/proc/{member}/stat").read_text().rsplit(")", 1)[1]
                if int(fields.split()[2]) == process_group:
                    os.kill(int(member), value)
            except (OSError, ValueError, IndexError):
                # diagnostic-expected: a member exited while its process group was read.
                continue

    def _commands(self, connection, group: Path, process_group: int) -> None:
        reader = connection.makefile("rb")
        try:
            for line in reader:
                if len(line) > 4096:
                    return
                try:
                    command = json.loads(line)
                except ValueError:
                    # diagnostic-expected: malformed control input ends the signal channel.
                    return
                if command.get("op") != "signal":
                    continue
                value = SIGNALS.get(command.get("signal"))
                if value is not None:
                    self._signal_group(group, process_group, value)
        except OSError:
            # diagnostic-expected: the client closed the signal channel.
            return

    # -- server ----------------------------------------------------------
    def authorize(self, connection) -> None:
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, _gid = struct.unpack("3i", credentials)
        if uid not in {self.client_uid, 0}:
            raise RunnerError("forbidden", "this client may not use the memory runner")
        location = process_cgroup(pid)
        if location is None or location == self.jobs or self.jobs in location.parents:
            raise RunnerError(
                "forbidden", "managed workloads cannot control the runner"
            )

    def handle(self, connection) -> None:
        try:
            self.authorize(connection)
            message, fds = _receive(connection)
            operation = message.get("op")
            if operation == "spawn":
                self.spawn(message, fds, connection)
                return
            for descriptor in fds:
                os.close(descriptor)
            handler = {
                "info": lambda _request: self.info(),
                "create": self.create,
                "observe": self.observe,
                "update": self.update,
                "remove": self.remove,
            }.get(str(operation))
            if handler is None:
                raise RunnerError("invalid", "unknown operation")
            _send(connection, {"ok": True, "result": handler(message)})
        except RunnerError as error:
            # diagnostic-expected: refusals are reported to the authenticated client.
            _reply_error(connection, error.code, error.detail)
        except Exception as error:  # noqa: BLE001 - report, never crash the daemon
            # diagnostic-expected: unexpected failures are reported to the client and the daemon keeps serving.
            _reply_error(connection, "failed", f"{type(error).__name__}: {error}")
        finally:
            try:
                connection.close()
            except OSError:
                # diagnostic-expected: the client already closed its connection.
                pass

    def serve(self) -> None:
        # One runner instance per Core user can share this directory; each
        # socket is restricted to its own user's group.
        directory = self.socket_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o755)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            # diagnostic-expected: no stale socket exists to replace.
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chown(self.socket_path, 0, self.client_gid)
        os.chmod(self.socket_path, 0o660)
        server.listen(64)
        print(
            json.dumps(
                {"event": "ready", "socket": str(self.socket_path), **self.info()}
            ),
            flush=True,
        )
        while True:
            connection, _ = server.accept()
            threading.Thread(
                target=self.handle, args=(connection,), daemon=True
            ).start()


def _receive(connection) -> tuple[dict[str, Any], list[int]]:
    buffer = b""
    fds: list[int] = []
    while not buffer.endswith(b"\n"):
        data, received, _flags, _address = socket.recv_fds(connection, 65536, 8)
        fds.extend(received)
        if not data:
            break
        buffer += data
        if len(buffer) > MAX_REQUEST_BYTES:
            raise RunnerError("invalid", "request is too large")
    try:
        message = json.loads(buffer)
    except ValueError as error:
        for descriptor in fds:
            os.close(descriptor)
        raise RunnerError("invalid", "request is not JSON") from error
    if not isinstance(message, dict):
        raise RunnerError("invalid", "request must be an object")
    return message, fds


def _send(connection, document: dict[str, Any]) -> None:
    connection.sendall(json.dumps(document, separators=(",", ":")).encode() + b"\n")


def _reply_error(connection, code: str, detail: str) -> None:
    try:
        _send(connection, {"ok": False, "error": code, "detail": detail[:2000]})
    except OSError:
        # diagnostic-expected: the client already closed its connection.
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nebula protected memory runner")
    parser.add_argument("--client-user", required=True, help="Core's OS user")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--cgroup-root",
        type=Path,
        default=None,
        help="Delegated cgroup directory (default: this process's own cgroup)",
    )
    arguments = parser.parse_args(argv)
    import pwd

    account = pwd.getpwnam(arguments.client_user)
    root = arguments.cgroup_root or own_cgroup()
    if root.parent == CGROUP_MOUNT.parent or not str(root).startswith(
        str(CGROUP_MOUNT) + "/"
    ):
        print("refusing to manage the cgroup root", file=sys.stderr)
        return 2
    if not stat.S_ISDIR(os.stat(root).st_mode):
        print("cgroup root is not a directory", file=sys.stderr)
        return 2
    runner = Runner(
        root=root,
        client_uid=account.pw_uid,
        client_gid=account.pw_gid,
        socket_path=arguments.socket,
        state=arguments.state_dir,
    )
    try:
        runner.prepare()
    except RunnerError as error:
        # diagnostic-expected: an unsupported host is reported on stdout for the service log.
        print(json.dumps({"event": "unsupported", "detail": error.detail}), flush=True)
        return 3
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    runner.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
