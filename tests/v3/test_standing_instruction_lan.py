"""Authenticated real Core over a disposable non-loopback listener."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio
import json
import socket
import time
import httpx
import pytest
import uvicorn
from nebula.v3.api import create_app
from nebula.v3.host_load import HostLoadGate, HostLoadMonitor, HostLoadSnapshot
from tests.v3.test_standing_instruction_continuity import environment, RULE, EDITED_RULE


class AvailableFixtureHost(HostLoadMonitor):
    def cached(self):
        return self.sample()

    def sample(self):
        return HostLoadSnapshot(
            sampled_at=time.monotonic(),
            cpu_capacity=4,
            cpu_ratio=0.0,
            memory_available_ratio=1.0,
            busy=False,
            reason=None,
            measurements_available=True,
        )


async def test_authenticated_lan_chat_keeps_current_instructions_after_compaction_and_edit(
    tmp_path,
):
    store, project, profile, runtime, vendor, _, decision = await environment(tmp_path)
    token = "synthetic-instructions-fixture"
    app = create_app(
        store,
        auth_token=token,
        harness_runtime_service=runtime,
        execution_data_root=tmp_path,
        host_load_gate=HostLoadGate(AvailableFixtureHost()),
    )
    host = socket.gethostbyname(socket.gethostname())
    assert not host.startswith("127.")
    listener = socket.socket()
    listener.bind((host, 0))
    listener.listen()
    origin = f"http://{host}:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            base_url=origin, trust_env=False, timeout=20
        ) as client:
            unauthenticated = await client.post(
                "/api/v1/chat/completions",
                json={
                    "backend": "harness",
                    "engagement_id": project.id,
                    "harness_profile_id": profile.id,
                    "model": "test-model",
                    "include_knowledge": False,
                    "messages": [
                        {"role": "user", "content": "Unauthorized synthetic request"}
                    ],
                },
            )
            assert unauthenticated.status_code == 401
            headers = {"Authorization": f"Bearer {token}"}
            chat_id = None

            async def send(text):
                nonlocal chat_id
                response = await client.post(
                    "/api/v1/chat/completions",
                    headers=headers,
                    json={
                        "backend": "harness",
                        "engagement_id": project.id,
                        "harness_profile_id": profile.id,
                        "model": "test-model",
                        "session_id": chat_id,
                        "include_knowledge": False,
                        "stream": True,
                        "messages": [{"role": "user", "content": text}],
                    },
                )
                assert response.status_code == 200, response.text
                frames = [
                    json.loads(line[5:])
                    for line in response.text.splitlines()
                    if line.startswith("data:") and "[DONE]" not in line
                ]
                done = next(frame for frame in frames if frame["type"] == "done")
                chat_id = done["session_id"]
                return frames

            initial = await send("First authenticated synthetic request")
            first_native = next(iter(vendor.sessions))
            assert vendor.effective(first_native).count(RULE) == 1
            vendor.compact(first_native)
            await send("Continue after deliberately lossy native compaction")
            assert vendor.effective(first_native).count(RULE) == 1
            changed = await client.put(
                f"/api/v1/chat/sessions/{chat_id}/decisions/{decision.id}",
                headers=headers,
                json={
                    "expected_revision": decision.revision,
                    "text": EDITED_RULE,
                    "kind": "constraint",
                },
            )
            assert changed.status_code == 200, changed.text
            final = await send("Apply the saved instruction update")
            latest_native = list(vendor.sessions)[-1]
            assert latest_native != first_native
            assert RULE not in vendor.effective(latest_native)
            assert vendor.effective(latest_native).count(EDITED_RULE) == 1
            decisions = await client.get(
                f"/api/v1/chat/sessions/{chat_id}/decisions", headers=headers
            )
            assert decisions.status_code == 200
            assert decisions.json()[0]["text"] == EDITED_RULE
            receipt = {
                "synthetic_only": True,
                "origin": origin,
                "unauthenticated_status": 401,
                "initial_events": initial,
                "final_events": final,
                "requests": vendor.calls,
                "effective_contexts": vendor.sessions,
                "fixture_compaction": "lossy history; durable system head",
            }
            (tmp_path / "standing-instructions-lan-capture.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        listener.close()


pytestmark = pytest.mark.anyio
