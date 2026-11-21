#!/usr/bin/env python3
"""Call the private host services as a retained worker does, through swaps.

Finds a ready execution worker of the scratch Core, reads its private IPC
credential, and calls the read-only `host.services/v1` capability through the
worker's own endpoint (<socket root>/host-services.sock) from several loops
until the stop file appears. By default it uses a plain RPC client that never
resends, so every call made while no supervisor generation serves is either
held by the kernel (holder-owned socket) or fails; `--resend` uses the
workers' HostServiceClient fallback instead. Writes a JSON summary.

Env: REHEARSAL_ROOT, NEBULA_V3_DATABASE_URL (from core.env), PYTHONPATH with
the Core source under test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from sqlalchemy import select

from nebula.v3.database import Database, RuntimeProcessRow
from nebula.v3.runtime_host_services import (
    HOST_SOCKET_NAME,
    PEER_HEADER,
    HostServiceClient,
)
from nebula.v3.runtime_ipc import RuntimeCredentials, RuntimeRpcClient
from nebula.v3.runtime_processes import ORIGIN_MARKER

ROOT = Path(
    os.getenv("REHEARSAL_ROOT", str(Path.home() / "nebula-scratch/deploy-continuity"))
)
RUNTIME = ROOT / "core/data/runtime"


async def worker():
    database = Database(os.environ["NEBULA_V3_DATABASE_URL"], bootstrap=False)
    try:
        async with database.session() as session:
            rows = list(
                await session.scalars(
                    select(RuntimeProcessRow).where(
                        RuntimeProcessRow.role == "execution",
                        RuntimeProcessRow.state == "ready",
                    )
                )
            )
    finally:
        await database.dispose()
    if not rows:
        raise SystemExit("No ready execution worker; start a chat first")
    row = rows[0]
    secret = RuntimeCredentials(RUNTIME / "credentials").read(row.credential_reference)
    return row.runtime_id, secret


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--loops", type=int, default=4)
    parser.add_argument("--resend", action="store_true")
    args = parser.parse_args()
    runtime_id, secret = await worker()
    sockets = Path(json.loads((RUNTIME / ORIGIN_MARKER).read_text())["socket_root"])
    endpoint = "unix://" + str(sockets / HOST_SOCKET_NAME)
    calls = []

    async def loop(index):
        kind = HostServiceClient if args.resend else RuntimeRpcClient
        client = kind(endpoint, secret)
        client.client.headers[PEER_HEADER.decode()] = runtime_id
        try:
            while not Path(args.stop_file).exists():
                began = time.time()
                try:
                    result = await client.call("host.services/v1", {})
                    outcome = "ok" if result.get("capacity") is True else "unexpected"
                except Exception as error:  # noqa: BLE001 - every failure is counted
                    outcome = f"{type(error).__name__}: {error}"[:160]
                calls.append(
                    {
                        "loop": index,
                        "start": round(began, 3),
                        "seconds": round(time.time() - began, 3),
                        "outcome": outcome,
                    }
                )
                await asyncio.sleep(0.05)
        finally:
            await client.close()

    await asyncio.gather(*(loop(index) for index in range(args.loops)))
    outcomes = {}
    for call in calls:
        outcomes[call["outcome"]] = outcomes.get(call["outcome"], 0) + 1
    slowest = sorted(calls, key=lambda call: call["seconds"])[-5:]
    summary = {
        "runtime_id": runtime_id,
        "endpoint_is_link": (sockets / HOST_SOCKET_NAME).is_symlink(),
        "client": "HostServiceClient" if args.resend else "RuntimeRpcClient",
        "calls": len(calls),
        "outcomes": outcomes,
        "failures": sum(n for name, n in outcomes.items() if name != "ok"),
        "slowest": slowest,
    }
    Path(args.out).write_text(json.dumps({"summary": summary, "calls": calls}))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
