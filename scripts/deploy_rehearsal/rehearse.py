#!/usr/bin/env python3
"""Rehearse a Core disruption against long streaming chats on a scratch Core.

For every selected project this opens a fresh chat whose provider is the slow,
marker-logging stub, waits for the first streamed token, queues follow-ups
through the durable chat queue, then runs a disruption command (supervisor
restart, full restart, rolling handoff...) while the replies are still
streaming. Viewers reconnect with their saved cursor, the way the UI does.
Afterwards it reads authoritative SQL and the stub's request log and reports
per marker: user messages, assistant replies, provider requests, turn states,
queue item states and every stream error frame.

Env: REHEARSAL_ROOT (scratch root with core/ and stub/), REHEARSAL_PORT,
REHEARSAL_STUB_PORT, REHEARSAL_PSQL (psql command prefix), RH_DB (database).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import httpx

ROOT = Path(
    os.getenv("REHEARSAL_ROOT", str(Path.home() / "nebula-scratch/deploy-continuity"))
)
BASE = os.getenv(
    "RH_BASE", f"http://127.0.0.1:{os.getenv('REHEARSAL_PORT', '18440')}/api/v1"
)
DATA = Path(os.getenv("RH_DATA", str(ROOT / "core/data")))
DB = os.getenv("RH_DB", "nebula_deploy_continuity_core")
STUB_LOG = Path(os.getenv("RH_STUB_LOG", str(ROOT / "stub/requests.jsonl")))
SLOW = f"http://127.0.0.1:{os.getenv('REHEARSAL_STUB_PORT', '18441')}/v1"
PSQL = os.getenv(
    "REHEARSAL_PSQL", "docker exec nebula-staging-db psql -U nebula"
).split()


def log(*parts):
    print(f"[{time.strftime('%H:%M:%S')}]", *parts, flush=True)


async def _idempotency(request: httpx.Request) -> None:
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and "Idempotency-Key" not in request.headers
    ):
        request.headers["Idempotency-Key"] = str(uuid.uuid4())


def client(timeout: float = 30) -> httpx.AsyncClient:
    token = (DATA / "runtime" / "operator-token").read_text().strip()
    return httpx.AsyncClient(
        base_url=BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        event_hooks={"request": [_idempotency]},
    )


async def retrying(call, *, attempts=240, delay=0.5):
    last = None
    for _ in range(attempts):
        try:
            response = await call()
            if response.status_code in (429, 502, 503, 504):
                last = f"http {response.status_code}"
                await asyncio.sleep(delay)
                continue
            return response
        except (httpx.TransportError,) as error:
            last = f"{type(error).__name__}"
            await asyncio.sleep(delay)
    raise RuntimeError(f"gave up after retries: {last}")


async def ensure_provider(http) -> str:
    providers = (await http.get("/providers", params={"limit": 1000})).json()
    items = providers if isinstance(providers, list) else providers.get("items", [])
    for item in items:
        if item.get("endpoint") == SLOW:
            return item["id"]
    response = await http.post(
        "/providers",
        json={
            "name": "Slow rehearsal model",
            "provider_type": "openai_compatible",
            "endpoint": SLOW,
            "enabled": True,
            "is_local": True,
            "secret_ref": None,
            "model_allowlist": ["staging-model"],
            "capabilities": {"streaming": True},
            "privacy": {
                "local_only": True,
                "permits_sensitive_data": True,
                "auto_share_tool_results": False,
            },
            "metadata": {"default_model": "staging-model"},
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def request_body(provider, project, session, text):
    return {
        "backend": "provider",
        "provider_id": provider,
        "model": "staging-model",
        "engagement_id": project,
        "session_id": session,
        "mcp_server_ids": [],
        "messages": [{"role": "user", "content": text, "content_blocks": []}],
        "context_attachments": [],
        "include_knowledge": False,
        "allow_cloud_knowledge": False,
        "tools_enabled": False,
        "stream": True,
    }


class Chat:
    def __init__(self, run, index, project):
        self.run, self.index, self.project = run, index, project
        self.session = None
        self.turn = None
        self.cursor = 0
        self.epoch = None
        self.kinds: dict[str, int] = {}
        self.errors: list[dict] = []
        self.transport_failures: list[str] = []
        self.reconnects = 0
        self.first_token = asyncio.Event()
        self.finished = False
        self.queued: list[str] = []

    def marker(self, message):
        return f"[rh:{self.run}:p{self.index}:m{message}]"

    async def consume(self, response):
        event = None
        async for line in response.aiter_lines():
            if line.startswith("id:"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            kind = event or payload.get("type") or "?"
            self.kinds[kind] = self.kinds.get(kind, 0) + 1
            if isinstance(payload.get("sequence"), int):
                self.cursor = payload["sequence"]
                self.epoch = payload.get("epoch", self.epoch)
            self.turn = payload.get("turn_id") or self.turn
            self.session = payload.get("session_id") or self.session
            if kind == "error" or payload.get("type") == "error":
                self.errors.append(
                    {k: payload.get(k) for k in ("code", "detail", "type")}
                )
            if (
                payload.get("delta")
                or payload.get("content")
                or kind in ("delta", "content", "text")
            ):
                self.first_token.set()
            event = None

    async def run_first(self, http, provider):
        body = request_body(
            provider, self.project, None, self.marker(0) + " Long rehearsal turn."
        )
        try:
            async with http.stream(
                "POST",
                "/chat/completions",
                json=body,
                headers={"Accept": "text/event-stream"},
                timeout=600,
            ) as response:
                if response.status_code >= 400:
                    self.transport_failures.append(
                        f"start http {response.status_code}: {(await response.aread())[:300]!r}"
                    )
                    self.first_token.set()
                    return
                await self.consume(response)
            self.finished = True
        except (httpx.TransportError, httpx.StreamError) as error:
            self.transport_failures.append(
                f"stream {type(error).__name__}: {str(error)[:200]}"
            )
        self.first_token.set()
        while not self.finished:
            await self.reconnect(http)

    async def reconnect(self, http):
        if not self.turn:
            self.transport_failures.append("no turn id to reconnect")
            self.finished = True
            return
        self.reconnects += 1
        params = {"after": self.cursor}
        if self.epoch:
            params["epoch"] = self.epoch
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                async with http.stream(
                    "GET",
                    f"/chat/turns/{self.turn}/events",
                    params=params,
                    headers={"Accept": "text/event-stream"},
                    timeout=600,
                ) as response:
                    if response.status_code in (404, 409, 429, 502, 503, 504):
                        await asyncio.sleep(0.5)
                        continue
                    if response.status_code >= 400:
                        self.transport_failures.append(
                            f"reconnect http {response.status_code}: {(await response.aread())[:200]!r}"
                        )
                        await asyncio.sleep(0.5)
                        continue
                    await self.consume(response)
                    self.finished = True
                    return
            except (httpx.TransportError, httpx.StreamError) as error:
                self.transport_failures.append(f"reconnect {type(error).__name__}")
                await asyncio.sleep(0.5)
                return
        self.transport_failures.append("reconnect deadline")
        self.finished = True

    async def enqueue(self, http, provider, count):
        for message in range(1, count + 1):
            text = self.marker(message) + " Queued follow-up."
            for _ in range(240):
                try:
                    queue = (
                        await retrying(
                            lambda: http.get(f"/chat/sessions/{self.session}/queue")
                        )
                    ).json()
                    response = await retrying(
                        lambda: http.post(
                            f"/chat/sessions/{self.session}/queue",
                            json={
                                "expected_revision": queue.get("revision", 0),
                                "action": "enqueue",
                                "idempotency_key": f"{self.run}-{self.index}-{message}",
                                "request": request_body(
                                    provider, self.project, self.session, text
                                ),
                            },
                        )
                    )
                except RuntimeError as error:
                    self.transport_failures.append(f"enqueue {error}")
                    break
                if response.status_code == 409:
                    await asyncio.sleep(0.3)
                    continue
                if response.status_code >= 400:
                    self.transport_failures.append(
                        f"enqueue http {response.status_code}: {response.text[:300]}"
                    )
                break
            self.queued.append(text)


def psql(sql: str) -> list[list[str]]:
    for _ in range(30):
        result = subprocess.run(
            [*PSQL, "-d", DB, "-AtF", "\t", "-c", sql],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return [line.split("\t") for line in result.stdout.splitlines() if line]
        time.sleep(2)
    raise RuntimeError(result.stderr)


def verify(chats, run, stub_offset):
    stub = []
    with STUB_LOG.open() as stream:
        stream.seek(stub_offset)
        stub = [json.loads(line) for line in stream if line.strip()]
    requests, completed = {}, {}
    for entry in stub:
        for mark in entry.get("marks", []):
            requests[mark] = requests.get(mark, 0) + 1
        for mark in entry.get("done", []):
            completed[mark] = completed.get(mark, 0) + 1
    report = {"run": run, "chats": [], "violations": []}
    for chat in chats:
        if not chat.session:
            report["violations"].append(f"p{chat.index}: no session")
            continue
        messages = psql(
            "select payload->>'role', payload->>'content', (payload->>'sequence')::int from entities "
            f"where kind='chat_messages' and payload->>'session_id'='{chat.session}' order by 3"
        )
        turns = psql(
            "select id, payload->>'status', coalesce(payload->>'error',''), coalesce(payload->>'final_message_id','') from entities "
            f"where kind='chat_turns' and payload->>'session_id'='{chat.session}' order by payload->>'created_at'"
        )
        queue = psql(
            "select item->>'status', coalesce(item->>'error',''), coalesce(item->>'turn_id','') from entities, "
            "json_array_elements(payload->'items') item "
            f"where kind='chat_queues' and id='chat-queue-{chat.session}'"
        )
        users = [content for role, content, _ in messages if role == "user"]
        assistants = [content for role, content, _ in messages if role == "assistant"]
        markers = [chat.marker(i) for i in range(len(chat.queued) + 1)]
        per_marker = {}
        for mark in markers:
            seen = sum(mark in text for text in users)
            per_marker[mark] = {
                "user_messages": seen,
                "provider_requests": requests.get(mark, 0),
                "provider_completed": completed.get(mark, 0),
            }
            if seen != 1:
                report["violations"].append(f"{mark}: {seen} user messages")
            if requests.get(mark, 0) != 1:
                report["violations"].append(
                    f"{mark}: {requests.get(mark, 0)} provider requests"
                )
        states = [state for _, state, _, _ in turns]
        bad_turns = [
            (t, s, e[:160])
            for t, s, e, _ in turns
            if s != "complete" or e not in ("", "null")
        ]
        if bad_turns:
            report["violations"].append(
                f"p{chat.index}: turns not cleanly complete {bad_turns}"
            )
        if len(turns) != len(markers):
            report["violations"].append(
                f"p{chat.index}: {len(turns)} turns for {len(markers)} messages"
            )
        if len(assistants) != len(markers):
            report["violations"].append(
                f"p{chat.index}: {len(assistants)} assistant replies for {len(markers)} messages"
            )
        bad_queue = [row for row in queue if row[0] != "complete"]
        if bad_queue:
            report["violations"].append(
                f"p{chat.index}: queue items not complete {bad_queue}"
            )
        if chat.errors:
            report["violations"].append(
                f"p{chat.index}: stream error frames {chat.errors}"
            )
        report["chats"].append(
            {
                "project": chat.project,
                "session": chat.session,
                "first_turn": chat.turn,
                "turn_states": states,
                "queue_states": [row[0] for row in queue],
                "user_messages": len(users),
                "assistant_messages": len(assistants),
                "markers": per_marker,
                "stream_kinds": chat.kinds,
                "stream_error_frames": chat.errors,
                "viewer_transport_failures": chat.transport_failures,
                "viewer_reconnects": chat.reconnects,
            }
        )
    return report


async def wait_settled(chats, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = 0
        for chat in chats:
            if not chat.session:
                continue
            rows = psql(
                "select count(*) from entities where kind='chat_turns' and "
                f"payload->>'session_id'='{chat.session}' and payload->>'status' not in ('complete','failed','cancelled','interrupted')"
            )
            queue = psql(
                "select count(*) from entities, json_array_elements(payload->'items') item "
                f"where kind='chat_queues' and id='chat-queue-{chat.session}' and payload->>'paused' = 'false' and item->>'status' not in ('complete','failed','cancelled','needs_review')"
            )
            pending += int(rows[0][0]) + int(queue[0][0] if queue else 0)
        if pending == 0:
            return True
        await asyncio.sleep(2)
    return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--projects", type=int, default=3)
    parser.add_argument("--followups", type=int, default=2)
    parser.add_argument(
        "--disrupt", default="", help="shell command run once every chat is streaming"
    )
    parser.add_argument("--disrupt-delay", type=float, default=2.0)
    parser.add_argument("--settle-timeout", type=float, default=900)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--project-ids",
        default="",
        help="comma-separated projects instead of the first N",
    )
    parser.add_argument(
        "--queued-start",
        action="store_true",
        help="do not wait for a first token; turns may be held in admission",
    )
    parser.add_argument(
        "--background",
        default="",
        help="shell command started together with the disruption",
    )
    args = parser.parse_args()
    run = args.label + "-" + uuid.uuid4().hex[:6]
    stub_offset = STUB_LOG.stat().st_size if STUB_LOG.exists() else 0
    async with client(timeout=60) as http:
        provider = await ensure_provider(http)
        listing = (await http.get("/engagements", params={"limit": 100})).json()
        projects = [
            item["id"]
            for item in (listing if isinstance(listing, list) else listing["items"])
        ][: args.projects]
        if args.project_ids:
            projects = args.project_ids.split(",")
        chats = [Chat(run, index, project) for index, project in enumerate(projects)]
        log("run", run, "projects", len(chats))
        started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 2))
        tasks = [asyncio.create_task(chat.run_first(http, provider)) for chat in chats]
        if args.queued_start:
            for _ in range(240):
                for chat in chats:
                    if chat.session is None:
                        sessions = (
                            await http.get(
                                "/chat-sessions", params={"engagement_id": chat.project}
                            )
                        ).json()
                        items = (
                            sessions
                            if isinstance(sessions, list)
                            else sessions.get("items", [])
                        )
                        for item in items:
                            if (
                                chat.marker(0)[1:-1].split(":p")[0]
                                in (item.get("title") or "")
                                or item.get("created_at", "") > started_at
                            ):
                                chat.session = item["id"]
                                break
                if all(chat.session for chat in chats):
                    break
                await asyncio.sleep(0.5)
            log(
                "all chats accepted (admission may be held); sessions",
                [chat.session for chat in chats],
            )
        else:
            await asyncio.wait_for(
                asyncio.gather(*(chat.first_token.wait() for chat in chats)), 300
            )
            log("all chats streaming; sessions", [chat.session for chat in chats])
        await asyncio.gather(
            *(chat.enqueue(http, provider, args.followups) for chat in chats)
        )
        log("follow-ups queued")
        disruption = None
        if args.disrupt:
            await asyncio.sleep(args.disrupt_delay)
            log("disrupt:", args.disrupt)
            started = time.monotonic()
            if args.background:
                log("background:", args.background)
                await asyncio.create_subprocess_shell(args.background)
            process = await asyncio.create_subprocess_shell(args.disrupt)
            code = await process.wait()
            disruption = {
                "command": args.disrupt,
                "exit": code,
                "seconds": round(time.monotonic() - started, 1),
            }
            log("disruption finished", disruption)
        await asyncio.gather(*tasks)
        log("first-turn viewers finished")
        settled = await wait_settled(chats, args.settle_timeout)
        log("settled", settled)
    report = verify(chats, run, stub_offset)
    report["disruption"] = disruption
    report["settled"] = settled
    Path(args.out).write_text(json.dumps(report, indent=2))
    log("violations:", len(report["violations"]))
    for violation in report["violations"]:
        log("  ", violation)


if __name__ == "__main__":
    asyncio.run(main())
