"""Benchmark-only fair admission and an owned, kernel-limited compute executor.

This module is deliberately outside shipped Core. RPC is a private Unix socket
inside a mode-0700 disposable directory; callers can request only the inert CPU,
PDF and explicitly bounded memory probes below. No command/tool is executed.
"""

from __future__ import annotations

import asyncio
import base64
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
import json
import multiprocessing
import os
from pathlib import Path
import secrets
import struct
import time
from typing import Any

MAX_FRAME = 32 * 1024 * 1024


class BudgetFull(RuntimeError):
    pass


@dataclass(eq=False)
class Job:
    owner: str
    payload: dict
    size: int
    future: asyncio.Future
    context: Any
    on_start: Any
    admitted_at: float
    running: bool = False


class FairBudget:
    """A model: two global slots, one per owner, round-robin bounded queues."""

    def __init__(
        self, runner, *, concurrency=2, capacity=128, per_owner=4, byte_limit=32 << 20
    ):
        self.runner = runner
        self.concurrency = concurrency
        self.capacity = capacity
        self.per_owner = per_owner
        self.byte_limit = byte_limit
        self.queues: dict[str, deque[Job]] = {}
        self.ready: deque[str] = deque()
        self.active = Counter()
        self.jobs: set[Job] = set()
        self.bytes = 0
        self.wake = asyncio.Event()
        self.tasks = []
        self.closed = False
        self.counts = Counter()
        self.trace = []

    def start(self):
        if self.closed:
            raise RuntimeError("Model budget closed")
        if not self.tasks:
            self.tasks = [
                asyncio.create_task(self._dispatch(), name=f"budget-model-{i}")
                for i in range(self.concurrency)
            ]

    async def run(self, owner, payload, *, on_start=None):
        self.start()
        size = len(json.dumps(payload).encode())
        owned = sum(job.owner == owner for job in self.jobs)
        if (
            len(self.jobs) >= self.capacity
            or owned >= self.per_owner
            or size > 8 << 20
            or self.bytes + size > self.byte_limit
        ):
            self.counts["rejected"] += 1
            raise BudgetFull("Model background queue full; retry explicitly")
        job = Job(
            owner,
            payload,
            size,
            asyncio.get_running_loop().create_future(),
            copy_context(),
            on_start,
            time.monotonic(),
        )
        self.jobs.add(job)
        self.bytes += size
        self.queues.setdefault(owner, deque()).append(job)
        self.counts["accepted"] += 1
        self.counts["max_admitted"] = max(self.counts["max_admitted"], len(self.jobs))
        self.counts["max_bytes"] = max(self.counts["max_bytes"], self.bytes)
        self._ready(owner)
        try:
            return await job.future
        except asyncio.CancelledError:
            # diagnostic-expected: running jobs retain their resource reservation.
            self.counts["cancelled_running" if job.running else "cancelled_queued"] += 1
            if not job.running:
                if job in self.queues[owner]:
                    self.queues[owner].remove(job)
                self._release(job)
            raise

    def _ready(self, owner):
        if (
            self.queues.get(owner)
            and not self.active[owner]
            and owner not in self.ready
        ):
            self.ready.append(owner)
            self.wake.set()

    def _release(self, job):
        if job in self.jobs:
            self.jobs.remove(job)
            self.bytes -= job.size

    async def _dispatch(self):
        while True:
            while self.ready:
                owner = self.ready.popleft()
                if not self.queues.get(owner) or self.active[owner]:
                    continue
                job = self.queues[owner].popleft()
                break
            else:
                if self.closed:
                    return
                self.wake.clear()
                await self.wake.wait()
                continue
            job.running = True
            self.active[owner] += 1
            self.counts["max_active"] = max(
                self.counts["max_active"], sum(self.active.values())
            )
            self.counts["max_owner_active"] = max(
                self.counts["max_owner_active"], self.active[owner]
            )
            self.trace.append(
                {
                    "owner": owner,
                    "kind": job.payload["kind"],
                    "wait_s": time.monotonic() - job.admitted_at,
                }
            )
            try:
                if job.on_start is not None:
                    job.context.run(job.on_start)
                value = await self.runner(job.payload)
                self.counts["completed"] += 1
                if not job.future.done():
                    job.future.set_result(value)
                else:
                    self.counts["discarded"] += 1
            except Exception as exc:
                # diagnostic-expected: model job failures are returned to the
                # owning chat/render, which records authoritative durable state.
                self.counts["failed"] += 1
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                self.active[owner] -= 1
                self._release(job)
                self._ready(owner)

    async def shutdown(self):
        self.closed = True
        for owner, queue in self.queues.items():
            while queue:
                job = queue.popleft()
                job.future.cancel()
                self._release(job)
        self.ready.clear()
        self.wake.set()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def snapshot(self):
        return {
            **self.counts,
            "active": sum(self.active.values()),
            "active_by_owner": {
                owner: count for owner, count in self.active.items() if count
            },
            "admitted": len(self.jobs),
            "queued": sum(len(q) for q in self.queues.values()),
            "input_bytes": self.bytes,
            "capacity": self.capacity,
            "per_owner": self.per_owner,
            "concurrency": self.concurrency,
        }


def attached_chat_owner(store, harness_session_id):
    """Resolve current durable ownership, including legitimate harness rollovers."""
    from nebula.v3.domain import ChatSession

    chats = [
        chat
        for chat in store.find_entities(
            ChatSession,
            {"backend": "harness", "harness_session_id": harness_session_id},
        )
        if not chat.metadata.get("temporary_assistant")
        and not chat.metadata.get("side_chat")
    ]
    if len(chats) > 1:
        raise ValueError("Synthetic harness has ambiguous chat ownership")
    return chats[0].id if chats else None


async def read_frame(reader):
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    if length > MAX_FRAME:
        raise ValueError("Model RPC frame exceeds limit")
    return json.loads(await reader.readexactly(length))


async def write_frame(writer, payload):
    encoded = json.dumps(payload).encode()
    if len(encoded) > MAX_FRAME:
        raise ValueError("Model RPC reply exceeds limit")
    writer.write(struct.pack("!I", len(encoded)) + encoded)
    await writer.drain()


class ComputeClient:
    def __init__(self, path, token):
        self.path, self.token = str(path), token

    async def request(self, op, payload=None):
        async with asyncio.timeout(300):
            reader, writer = await asyncio.open_unix_connection(self.path)
            try:
                await write_frame(
                    writer, {"token": self.token, "op": op, "payload": payload}
                )
                reply = await read_frame(reader)
            finally:
                writer.close()
                await writer.wait_closed()
        if "error" in reply:
            raise RuntimeError("Model compute failed: " + reply["error"])
        return reply["value"]

    async def run(self, payload):
        return await self.request("run", payload)


def compute_job(payload):
    started = time.process_time()
    kind = payload["kind"]
    if kind == "cpu":
        seconds = float(payload["seconds"])
        if not 0 < seconds <= 2:
            raise ValueError("CPU probe bound")
        value = 1
        while time.process_time() - started < seconds:
            for _ in range(5000):
                value = (value * 1664525 + 1013904223) & 0xFFFFFFFF
        result = {"checksum": value}
    elif kind == "pdf":
        from nebula.v3.reporting import render_pdf_snapshot

        pdf, warnings = render_pdf_snapshot(
            payload["snapshot"],
            payload["font_root"],
            tuple(tuple(pair) for pair in payload["font_hashes"]),
        )
        result = {"pdf": base64.b64encode(pdf).decode("ascii"), "warnings": warnings}
    elif kind == "memory":
        amount = int(payload["mib"])
        if not 1 <= amount <= 1024:
            raise ValueError("Memory probe bound")
        data = bytearray(amount * 1024 * 1024)
        time.sleep(0.2)
        result = {"allocated": len(data)}
    else:
        raise ValueError("Unknown inert compute operation")
    return {**result, "pid": os.getpid(), "cpu_s": time.process_time() - started}


def cgroup_metrics():
    root = Path("/sys/fs/cgroup")

    def counters(name):
        return {
            key: int(value)
            for key, value in (
                line.split() for line in (root / name).read_text().splitlines()
            )
        }

    return {
        "cpu_max": (root / "cpu.max").read_text().strip(),
        "cpu": counters("cpu.stat"),
        "memory_max": (root / "memory.max").read_text().strip(),
        "memory_current": int((root / "memory.current").read_text()),
        "memory_peak": int((root / "memory.peak").read_text()),
        "memory_events": counters("memory.events"),
        "pids_max": (root / "pids.max").read_text().strip(),
        "pids_current": int((root / "pids.current").read_text()),
    }


async def compute_server(directory, workers):
    """Runs only inside the driver's owned --network=none Docker container."""
    token = os.environ.pop("NEBULA_RESOURCE_MODEL_TOKEN")
    path = Path(directory) / "compute.sock"
    pool = ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    )
    active = 0
    maximum = 0

    async def handle(reader, writer):
        nonlocal pool, active, maximum
        try:
            request = await read_frame(reader)
            if not secrets.compare_digest(request.get("token", ""), token):
                raise ValueError("Synthetic RPC authentication failed")
            op = request["op"]
            if op == "run":
                active += 1
                maximum = max(maximum, active)
                try:
                    value = await asyncio.get_running_loop().run_in_executor(
                        pool, compute_job, request["payload"]
                    )
                finally:
                    active -= 1
            elif op == "metrics":
                value = {
                    **cgroup_metrics(),
                    "active": active,
                    "max_active": maximum,
                    "workers": workers,
                }
            elif op == "reset":
                if active:
                    raise ValueError("Cannot reset a busy model executor")
                pool.shutdown(wait=True, cancel_futures=True)
                pool = ProcessPoolExecutor(
                    max_workers=workers, mp_context=multiprocessing.get_context("spawn")
                )
                value = {"reset": True}
            else:
                raise ValueError("Unknown model RPC operation")
            reply = {"value": value}
        except Exception as exc:
            # diagnostic-expected: the driver records explicit fault outcomes;
            # never leak input or exception text through this benchmark RPC.
            reply = {"error": type(exc).__name__}
        try:
            await write_frame(writer, reply)
        except (BrokenPipeError, ConnectionResetError):
            # diagnostic-expected: a cancelled caller may close its owned socket.
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=path)
    try:
        async with server:
            await server.serve_forever()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


class PdfGateway:
    """Only the model fixture injects this into the existing report service."""

    def __init__(self, client, budget, root):
        self.client, self.budget, self.root = client, budget, str(root)
        self.rendering = asyncio.Semaphore(2)
        self.admitted = 0

    async def start(self):
        if self.budget:
            self.budget.start()

    async def shutdown(self):
        pass  # The fixture lifespan owns the shared budget and compute container.

    async def run(
        self, function, snapshot, font_root, hashes, *, input_bytes=0, on_start=None
    ):
        if function.__name__ != "render_pdf_snapshot":
            raise ValueError("Model supports only pure PDF generation")
        payload = {
            "kind": "pdf",
            "snapshot": snapshot,
            "font_root": font_root.replace(self.root, "/repo", 1),
            "font_hashes": hashes,
        }
        if self.budget:
            result = await self.budget.run(
                "pdf:" + snapshot["report"]["id"], payload, on_start=on_start
            )
        else:
            if self.admitted >= 8 or input_bytes > 8 << 20:
                raise BudgetFull("Two-render model queue full")
            self.admitted += 1
            try:
                async with self.rendering:
                    if on_start:
                        on_start()
                    result = await self.client.run(payload)
            finally:
                self.admitted -= 1
        return base64.b64decode(result["pdf"], validate=True), result["warnings"]
