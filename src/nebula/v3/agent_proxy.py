"""Supervise one isolated mitmdump process behind the Core scope proxy."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml

from .browser_host_proxy import BrowserHostProxy
from .security_flow_bridge import SecurityFlowBridge
from .storage import NebulaStore


class AgentMitmProxy:
    def __init__(
        self,
        store: NebulaStore,
        session_id: str,
        identity_id: str,
        root: Path,
        authorize: Callable[[str], None],
        connect: Callable[
            [str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
        ]
        | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.identity_id = identity_id
        self.root = root / hashlib.sha256(identity_id.encode()).hexdigest()
        self.upstream = BrowserHostProxy(authorize, connect=connect)
        self.bridge = SecurityFlowBridge(store, session_id)
        self.password = secrets.token_urlsafe(32)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = ""
        self._port: int | None = None

    async def start(self) -> dict[str, str]:
        if self._process is not None and self._process.returncode is None:
            return self._config()
        await self.close()
        executable = Path(os.environ.get("NEBULA_MITMDUMP_EXECUTABLE", ""))
        addon = Path(os.environ.get("NEBULA_SECURITY_PROXY_ADDON", ""))
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError(
                "The isolated mitmdump sidecar is unavailable. Install the security-proxy runtime or repair the desktop package."
            )
        if not addon.is_absolute() or not addon.is_file():
            raise ValueError(
                "The security flow addon is unavailable. Repair the desktop package."
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        try:
            upstream = await self.upstream.start()
            await self.bridge.start()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                self._port = listener.getsockname()[1]
            config = {
                "mode": [f"upstream:{upstream['server']}"],
                "upstream_auth": f"nebula:{upstream['password']}",
                "proxyauth": f"nebula:{self.password}",
                "listen_host": "127.0.0.1",
                "listen_port": self._port,
                "connection_strategy": "lazy",
                # Bound Playwright API response buffering and proxy capture.
                "body_size_limit": "16m",
                "store_streamed_bodies": False,
                "flow_detail": 0,
                "http3": False,
                "onboarding": False,
                "rawtcp": False,
            }
            path = self.root / "config.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            os.chmod(path, 0o600)
            environment = {
                name: value
                for name, value in os.environ.items()
                if name
                in {
                    "HOME",
                    "USERPROFILE",
                    "PATH",
                    "LANG",
                    "LC_ALL",
                    "SYSTEMROOT",
                    "WINDIR",
                }
            }
            environment["NEBULA_SECURITY_FLOW_BRIDGE"] = self.bridge.address
            environment["NEBULA_SECURITY_FLOW_TOKEN"] = self.bridge.token
            self._process = await asyncio.create_subprocess_exec(
                str(executable),
                "--set",
                f"confdir={self.root}",
                "-s",
                str(addon),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            self._stderr_task = asyncio.create_task(self._read_stderr())
            for _ in range(100):
                if self._process.returncode is not None:
                    raise ValueError("The security proxy exited during startup")
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", self._port), 0.1
                    )
                    writer.close()
                    await writer.wait_closed()
                    return self._config()
                except (OSError, TimeoutError):
                    await asyncio.sleep(0.05)
            raise TimeoutError("The security proxy did not become ready")
        except BaseException:
            await self.close()
            raise

    def _config(self) -> dict[str, str]:
        if self._port is None:
            raise RuntimeError("security proxy has not started")
        return {
            "server": f"http://127.0.0.1:{self._port}",
            "username": "nebula",
            "password": self.password,
        }

    async def _read_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while chunk := await self._process.stderr.read(4096):
            self._stderr_tail = (self._stderr_tail + chunk.decode(errors="replace"))[
                -4000:
            ]

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        await self.bridge.close()
        await self.upstream.close()
        self._port = None
