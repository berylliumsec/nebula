"""Human-controlled PTY sessions, isolated from every model/tool capability."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import struct
import termios
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect


class PtyError(RuntimeError):
    pass


_SESSION_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class HumanPtyService:
    """Launch a real PTY only for an authenticated human WebSocket session."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    async def serve(
        self,
        websocket: WebSocket,
        *,
        session_id: str,
        columns: int,
        rows: int,
    ) -> None:
        if not _SESSION_ID.fullmatch(session_id):
            await websocket.close(code=4400, reason="invalid terminal session id")
            return
        workspace = (self.root / session_id).resolve()
        if self.root not in workspace.parents:
            await websocket.close(code=4400, reason="terminal workspace escaped root")
            return
        workspace.mkdir(mode=0o700, exist_ok=True)
        workspace.chmod(0o700)
        shell = Path("/bin/bash") if Path("/bin/bash").is_file() else Path("/bin/sh")
        master, slave = pty.openpty()
        _resize(master, columns, rows)
        environment = {
            "HOME": str(workspace),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TERM": "xterm-256color",
            "LANG": os.getenv("LANG", "C.UTF-8"),
        }
        process = await asyncio.create_subprocess_exec(
            str(shell),
            "--noprofile",
            "--norc",
            cwd=workspace,
            env=environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)

        async def output() -> None:
            while True:
                try:
                    data = await asyncio.to_thread(os.read, master, 65_536)
                except OSError:
                    return
                if not data:
                    return
                await websocket.send_text(data.decode("utf-8", errors="replace"))

        async def input_messages() -> None:
            while True:
                message = await websocket.receive_text()
                try:
                    frame = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if frame.get("type") == "input" and isinstance(frame.get("data"), str):
                    await asyncio.to_thread(
                        os.write, master, frame["data"].encode("utf-8")
                    )
                elif frame.get("type") == "resize":
                    new_columns = int(frame.get("columns", 0))
                    new_rows = int(frame.get("rows", 0))
                    if 1 <= new_columns <= 1000 and 1 <= new_rows <= 1000:
                        _resize(master, new_columns, new_rows)

        output_task = asyncio.create_task(output())
        input_task = asyncio.create_task(input_messages())
        process_task = asyncio.create_task(process.wait())
        try:
            done, pending = await asyncio.wait(
                {output_task, input_task, process_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if not task.cancelled():
                    task.exception()
            for task in pending:
                task.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            for task in (output_task, input_task, process_task):
                task.cancel()
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), timeout=2)
                except (ProcessLookupError, asyncio.TimeoutError):
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
            os.close(master)


def _resize(descriptor: int, columns: int, rows: int) -> None:
    if not 1 <= columns <= 1000 or not 1 <= rows <= 1000:
        raise PtyError("terminal dimensions must be between 1 and 1000")
    fcntl.ioctl(
        descriptor,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, columns, 0, 0),
    )


__all__ = ["HumanPtyService", "PtyError"]
