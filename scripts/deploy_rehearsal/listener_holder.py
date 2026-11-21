#!/usr/bin/env python3
"""Emulate a systemd socket unit for the scratch Core.

Holds 127.0.0.1:<port> open for the whole rehearsal and starts supervisor
generations with the socket-activation protocol (fd 3, LISTEN_FDS=1,
LISTEN_PID=<child>). Unless REHEARSAL_HOST_SOCKET=0, it also holds the private
host-services Unix socket (core/holder/host-services.sock, mode 0600) and
passes it as fd 4 with --host-services-fd 4, the way a second socket unit
does in production. Commands arrive one per line on a FIFO:
  start <PYTHONPATH>   start a generation running that code
  stop                 SIGTERM the current generation and wait for it to exit
  swap <PYTHONPATH>    stop, then start (what host activation does)
Every command's outcome is appended to holder.log as JSON.
"""

import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

CORE = (
    Path(
        os.getenv(
            "REHEARSAL_ROOT", str(Path.home() / "nebula-scratch/deploy-continuity")
        )
    )
    / "core"
)
PORT = int(os.getenv("REHEARSAL_PORT", "18440"))
FIFO = CORE / "holder.fifo"
LOG = CORE / "holder.log"
PY = os.getenv("REHEARSAL_PYTHON", str(Path.home() / "nebula-staging/venv/bin/python"))
UI = os.getenv("REHEARSAL_UI_DIR", str(Path.home() / "nebula/ui/dist"))
HOST_SOCKET = os.getenv("REHEARSAL_HOST_SOCKET", "1") != "0"


def note(**values):
    with LOG.open("a") as out:
        out.write(json.dumps({"t": time.time(), **values}) + "\n")


def environment(pythonpath):
    env = dict(os.environ)
    for line in (CORE / "core.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    env["PYTHONPATH"] = pythonpath
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def main():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", PORT))
    listener.listen(2048)
    # Pass copies above 4, so moving them to 3 and 4 never overwrites one.
    descriptor = fcntl.fcntl(listener.fileno(), fcntl.F_DUPFD, 10)
    services = None
    if HOST_SOCKET:
        directory = CORE / "holder"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "host-services.sock"
        path.unlink(missing_ok=True)
        services = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        services.bind(str(path))
        os.chmod(path, 0o600)
        services.listen(2048)
    services_descriptor = (
        fcntl.fcntl(services.fileno(), fcntl.F_DUPFD, 10) if services else None
    )
    if not FIFO.exists():
        os.mkfifo(FIFO, 0o600)
    note(
        event="holding",
        port=PORT,
        pid=os.getpid(),
        host_services=services.getsockname() if services else None,
    )
    child = None

    def start(pythonpath):
        passed = (descriptor,) + ((services_descriptor,) if services else ())
        move = f"exec 3<&{descriptor} {descriptor}<&-; "
        extra = ""
        if services is not None:
            move += f"exec 4<&{services_descriptor} {services_descriptor}<&-; "
            extra = " --host-services-fd 4"
        command = (
            move + 'LISTEN_PID=$$ LISTEN_FDS=1 exec "$0" -m nebula.v3.cli serve '
            f"--host 127.0.0.1 --port {PORT} --data-dir {CORE / 'data'} "
            f"--static-dir {UI}" + extra
        )
        output = open(CORE / "logs/core.out.log", "ab")
        process = subprocess.Popen(
            ["/bin/bash", "-c", command, PY],
            pass_fds=passed,
            env=environment(pythonpath),
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=CORE,
        )
        note(event="started", pid=process.pid, pythonpath=pythonpath)
        return process

    def stop(process):
        if process is None or process.poll() is not None:
            return
        began = time.monotonic()
        process.send_signal(signal.SIGTERM)
        try:
            code = process.wait(120)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
        note(
            event="stopped",
            pid=process.pid,
            exit=code,
            seconds=round(time.monotonic() - began, 2),
        )

    while True:
        with FIFO.open() as commands:
            for line in commands:
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "start":
                    child = start(parts[1])
                elif parts[0] == "stop":
                    stop(child)
                    child = None
                elif parts[0] == "swap":
                    stop(child)
                    child = start(parts[1])
                elif parts[0] == "exit":
                    stop(child)
                    note(event="exit")
                    return
                else:
                    note(event="unknown", line=line.strip())


if __name__ == "__main__":
    sys.exit(main())
