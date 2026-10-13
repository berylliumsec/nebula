"""Private, persistent Jupyter worker. Only framed JSON is written to stdout."""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Any

from jupyter_client import AsyncKernelManager


class Worker:
    def __init__(self) -> None:
        self.kernel = AsyncKernelManager(kernel_name="python3")
        self.client: Any = None
        self.active: asyncio.Task[None] | None = None
        self.output_lock = asyncio.Lock()

    async def send(self, frame: dict[str, Any]) -> None:
        async with self.output_lock:
            sys.stdout.write(
                json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            sys.stdout.flush()

    async def start(self) -> None:
        await self.kernel.start_kernel()
        self.client = self.kernel.client()
        self.client.start_channels()
        await self.client.wait_for_ready(timeout=30)
        await self.send({"type": "ready", "python": sys.version.split()[0]})

    async def run(self, run_id: str, code: str) -> None:
        assert self.client is not None
        status = "succeeded"
        try:
            message_id = self.client.execute(
                code, allow_stdin=False, store_history=True
            )
            await self.send({"type": "ack", "run_id": run_id})
            idle = False
            while not idle:
                message = await asyncio.wait_for(
                    self.client.get_iopub_msg(), timeout=900
                )
                if message.get("parent_header", {}).get("msg_id") != message_id:
                    continue
                kind = message.get("msg_type")
                content = message.get("content", {})
                if kind == "status" and content.get("execution_state") == "idle":
                    idle = True
                elif kind in {
                    "stream",
                    "execute_result",
                    "display_data",
                    "error",
                    "clear_output",
                }:
                    event = {"kind": kind, "content": content}
                    encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
                    if len(encoded) > 2_000_000:
                        event = {
                            "kind": "stream",
                            "content": {
                                "name": "stderr",
                                "text": "[Notebook output exceeded 2 MB and was truncated]",
                            },
                        }
                    await self.send({"type": "event", "run_id": run_id, "event": event})
                    if kind == "error":
                        status = "failed"
            reply = await asyncio.wait_for(self.client.get_shell_msg(), timeout=30)
            if reply.get("parent_header", {}).get("msg_id") == message_id:
                if reply.get("content", {}).get("status") != "ok":
                    status = "failed"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception as exc:
            status = "failed"
            await self.send(
                {
                    "type": "event",
                    "run_id": run_id,
                    "event": {
                        "kind": "error",
                        "content": {
                            "ename": type(exc).__name__,
                            "evalue": str(exc),
                            "traceback": traceback.format_exception(exc),
                        },
                    },
                }
            )
        finally:
            await self.send({"type": "result", "run_id": run_id, "status": status})
            self.active = None

    async def handle(self, frame: dict[str, Any]) -> None:
        operation = frame.get("op")
        request_id = frame.get("id")
        if operation == "run":
            if self.active is not None:
                await self.send(
                    {"type": "error", "id": request_id, "detail": "kernel is busy"}
                )
                return
            run_id = str(frame["run_id"])
            self.active = asyncio.create_task(self.run(run_id, str(frame["code"])))
        elif operation == "interrupt":
            await self.kernel.interrupt_kernel()
            await self.send({"type": "ack", "id": request_id})
        elif operation == "restart":
            if self.active is not None:
                await self.send(
                    {"type": "error", "id": request_id, "detail": "kernel is busy"}
                )
                return
            self.client.stop_channels()
            await self.kernel.restart_kernel(now=True)
            self.client = self.kernel.client()
            self.client.start_channels()
            await self.client.wait_for_ready(timeout=30)
            await self.send({"type": "ack", "id": request_id})
        elif operation == "shutdown":
            await self.send({"type": "ack", "id": request_id})
            raise SystemExit(0)
        else:
            await self.send(
                {"type": "error", "id": request_id, "detail": "unknown operation"}
            )


async def main() -> None:
    worker = Worker()
    try:
        await worker.start()
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                await worker.handle(json.loads(line))
            except SystemExit:
                break
            except Exception as exc:
                await worker.send({"type": "error", "detail": str(exc)})
    finally:
        if worker.active is not None:
            worker.active.cancel()
            await asyncio.gather(worker.active, return_exceptions=True)
        if worker.client is not None:
            worker.client.stop_channels()
        await worker.kernel.shutdown_kernel(now=True)


if __name__ == "__main__":
    asyncio.run(main())
