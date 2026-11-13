"""Durable HTTP acceptance and delivery independent of any public API process.

Only authenticated adapters call ``accept``. SQL contains keyed encrypted-content
references; credentials never enter an accepted intent. A claimed mutation is
never automatically retried, including after a partial HTTP response.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
import json
import re
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import select

from .async_utils import finish_cleanup
from .authentication import runtime_principal
from .database import (
    EntityRow,
    RuntimeChatBindingRow,
    RuntimeCommandRow,
    RuntimeDeliveryRow,
)
from .domain import NebulaModel, PairedDeviceSession, utc_now
from .execution_scope import execution_context
from .memory_actor import memory_actor
from .runtime_coordination import (
    ClaimedCommand,
    CommandReceipt,
    CoordinationConflict,
    ProjectLease,
    RuntimeCoordination,
)
from .runtime_ipc import RuntimeIpcError
from .runtime_vault import RuntimeVault
from .runtime_chat_acceptance import (
    CHAT_ACCEPT_COMMAND,
    CHAT_RESUME_COMMAND,
    CHAT_COMMANDS,
    HTTP_INVOKE_COMMAND,
    ChatBinding,
)
from .storage import NebulaStore, NotFoundError

MAX_REQUEST_BYTES = 8 * 1024**2
# Only global Library ingestion carries a base64-encoded 20 MiB document in
# JSON. Keep a bounded allowance for the filename, media type and JSON syntax.
MAX_LIBRARY_INGEST_REQUEST_BYTES = 4 * ((20 * 1024**2 + 2) // 3) + 128 * 1024
MAX_FRAME_BYTES = 256 * 1024
_INTENT_HEADERS = {
    "content-type",
    "accept",
    "if-match",
    "if-none-match",
    "if-entry-identity",
    "x-nebula-sensitive-data-acknowledged",
    "x-content-sha256",
    "x-parent-identity",
}


def _json(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def http_intent_body_limit(method: str, path: str) -> int:
    if method == "POST" and path == "/api/v1/library/items/ingest":
        return MAX_LIBRARY_INGEST_REQUEST_BYTES
    return MAX_REQUEST_BYTES


class HttpIntent(NebulaModel):
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=4096)
    query: str = Field(default="", max_length=8192)
    headers: list[tuple[str, str]] = Field(default_factory=list, max_length=8)
    body_base64: str = Field(
        default="", max_length=(MAX_LIBRARY_INGEST_REQUEST_BYTES + 2) // 3 * 4
    )

    @model_validator(mode="after")
    def valid_intent(self):
        if (
            not self.path.startswith("/api/v1/")
            or "?" in self.path
            or "#" in self.path
            or "\x00" in self.path
            or "\r" in self.query
            or "\n" in self.query
        ):
            raise ValueError("durable commands require a bounded API path")
        maximum = http_intent_body_limit(self.method, self.path)
        if len(self.body_base64) > (maximum + 2) // 3 * 4:
            raise ValueError("durable HTTP request exceeds its bound")
        try:
            self.path.encode("ascii")
            self.query.encode("ascii")
            raw = base64.b64decode(self.body_base64, validate=True)
        except (UnicodeError, ValueError, binascii.Error):
            raise ValueError("invalid durable HTTP request encoding") from None
        if len(raw) > maximum:
            raise ValueError("durable HTTP request exceeds its bound")
        names = []
        for name, value in self.headers:
            if (
                name not in _INTENT_HEADERS
                or len(value) > 4096
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("durable HTTP request contains an unsupported header")
            try:
                value.encode("latin-1")
            except UnicodeError:
                raise ValueError("invalid durable HTTP header encoding") from None
            names.append(name)
        if len(set(names)) != len(names):
            raise ValueError("durable HTTP headers must be unambiguous")
        return self

    @classmethod
    def capture(cls, method: str, path: str, query: bytes, headers, body: bytes):
        if len(body) > http_intent_body_limit(method, path):
            raise ValueError("durable HTTP request exceeds its bound")
        return cls.model_validate(
            {
                "method": method,
                "path": path,
                "query": query.decode("ascii"),
                "headers": sorted(
                    (key.decode("ascii").lower(), value.decode("latin-1"))
                    for key, value in headers
                    if key.decode("ascii").lower() in _INTENT_HEADERS
                ),
                "body_base64": base64.b64encode(body).decode("ascii"),
            }
        )

    def body(self) -> bytes:
        return base64.b64decode(self.body_base64, validate=True)


@dataclass(frozen=True)
class DeliveryFrame:
    sequence: int
    kind: str
    content: bytes


@dataclass(frozen=True)
class DeliveryPage:
    command_id: str
    project_id: str
    state: str
    frames: tuple[DeliveryFrame, ...]


class RuntimeDelivery:
    def __init__(self, coordination: RuntimeCoordination, vault: RuntimeVault):
        self.coordination, self.vault = coordination, vault

    @staticmethod
    def _intent_purpose(actor: str) -> str:
        if actor != "operator" and not (
            actor.startswith("device:") and len(actor) <= 200
        ):
            raise ValueError("durable commands require an authenticated Core principal")
        return "intent:" + actor

    @staticmethod
    def _frame_purpose(command_id: str, sequence: int) -> str:
        return f"delivery:{command_id}:{sequence}"

    @staticmethod
    def is_chat_start_intent(intent: HttpIntent) -> bool:
        """Recognize a real chat submission or explicit resume from its route."""
        if intent.method != "POST":
            return False

        try:
            body = json.loads(intent.body()) if intent.body() else {}
        # diagnostic-expected: malformed bodies cannot establish a chat-start intent.
        except (ValueError, UnicodeError, RecursionError):
            body = None

        if intent.path == "/api/v1/chat/completions":
            return (
                isinstance(body, dict)
                and isinstance(body.get("messages"), list)
                and bool(body["messages"])
                and body.get("backend", "provider") in {"provider", "harness"}
            )
        if intent.path == "/api/v1/chat/goal-conversations":
            # Goal conversations create a durable ChatSession before the first
            # turn, so this request itself is the start of an open chat.
            return (
                isinstance(body, dict)
                and isinstance(body.get("engagement_id"), str)
                and bool(body["engagement_id"])
                and isinstance(body.get("objective"), str)
                and bool(body["objective"])
            )
        if intent.path in {
            "/api/v1/chat/control-assistant/session",
            "/api/v1/chat/temporary-sessions",
        }:
            # These routes create (or reserve) an open project chat session.
            return (
                isinstance(body, dict)
                and isinstance(body.get("engagement_id"), str)
                and bool(body["engagement_id"])
            )
        if re.fullmatch(r"/api/v1/chat/sessions/[^/]{1,200}/fork", intent.path):
            # Forking an archived conversation starts a new exact-project chat.
            return True
        if re.fullmatch(r"/api/v1/runs/[^/]{1,200}/discuss", intent.path):
            # Attaching a completed run creates or returns its project chat.
            return True
        if re.fullmatch(
            r"/api/v1/executions/[^/]{1,200}/chat-attachments", intent.path
        ):
            # An operator explicitly asks to start an execution discussion chat.
            return True
        if re.fullmatch(r"/api/v1/chat/turns/[^/]{1,200}/resume", intent.path):
            return intent.body() in (b"", b"{}")
        return False

    async def accept(
        self,
        intent: HttpIntent,
        *,
        project_id: str,
        actor_id: str,
        idempotency_key: str,
        lane: Literal["work", "control"] = "work",
        expected_revision: int | None = None,
        chat_acceptance: bool = False,
        chat_resume: bool = False,
        chat_start: bool = False,
    ) -> CommandReceipt:
        if chat_acceptance and chat_resume:
            raise ValueError("A request has one chat transport")
        if chat_start and not self.is_chat_start_intent(intent):
            raise ValueError(
                "Chat admission requires a valid chat start or resume intent"
            )
        if chat_resume and (
            intent.method != "POST"
            or not re.fullmatch(r"/api/v1/chat/turns/[^/]{1,200}/resume", intent.path)
            or intent.body() not in (b"", b"{}")
        ):
            raise ValueError("Chat resume requires an empty explicit resume intent")
        if chat_acceptance:
            try:
                body = json.loads(intent.body())
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError(
                    "Chat acceptance requires a streaming chat intent"
                ) from None
            if (
                intent.method != "POST"
                or intent.path != "/api/v1/chat/completions"
                or not isinstance(body, dict)
                or body.get("stream") is not True
            ):
                raise ValueError("Chat acceptance requires a streaming chat intent")
        purpose = self._intent_purpose(actor_id)
        # Project and revision also participate in the immutable identity.
        raw = _json(intent.model_dump(mode="json"))
        reference = await asyncio.to_thread(self.vault.put, raw, purpose=purpose)
        return await self.coordination.submit(
            project_id=project_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            name=CHAT_ACCEPT_COMMAND
            if chat_acceptance
            else (CHAT_RESUME_COMMAND if chat_resume else HTTP_INVOKE_COMMAND),
            payload={"format": 1, "request_reference": reference},
            lane=lane,
            expected_revision=expected_revision,
            chat_start=chat_start,
        )

    async def request(self, command: ClaimedCommand) -> tuple[HttpIntent, str]:
        async with self.coordination.database.session() as session:
            row = await session.get(RuntimeCommandRow, command.receipt.command_id)
            if (
                row is None
                or row.name not in CHAT_COMMANDS | {HTTP_INVOKE_COMMAND}
                or row.name != command.name
                or row.payload != command.payload
            ):
                raise CoordinationConflict(
                    "durable HTTP invocation does not match its receipt"
                )
            actor = row.actor_id
        if (
            set(command.payload) != {"format", "request_reference"}
            or command.payload["format"] != 1
        ):
            raise RuntimeIpcError("unsupported durable HTTP request format")
        raw = await asyncio.to_thread(
            self.vault.get,
            command.payload["request_reference"],
            purpose=self._intent_purpose(actor),
        )
        return HttpIntent.model_validate_json(raw), actor

    async def append(
        self,
        lease: ProjectLease,
        command_id: str,
        kind: Literal["head", "body"],
        content: bytes,
    ) -> int:
        if kind not in {"head", "body"} or len(content) > MAX_FRAME_BYTES:
            raise RuntimeIpcError("runtime response frame exceeds its bound")
        # Sequence allocation, owner validation and publication share one short
        # fenced transaction. There is exactly one response writer per command.
        async with self.coordination.fenced(lease) as session:
            row = await session.get(RuntimeCommandRow, command_id)
            if row is None or (
                row.project_id,
                row.runtime_id,
                row.fencing_token,
                row.state,
            ) != (lease.project_id, lease.runtime_id, lease.fencing_token, "running"):
                raise CoordinationConflict(
                    "response writer no longer owns this command"
                )
            return await self._append_frame(session, lease, command_id, kind, content)

    async def _append_frame(self, session, lease, command_id, kind, content):
        previous = await session.scalar(
            select(RuntimeDeliveryRow)
            .where(RuntimeDeliveryRow.command_id == command_id)
            .order_by(RuntimeDeliveryRow.sequence.desc())
            .limit(1)
        )
        if (previous is None and kind != "head") or (
            previous is not None and (kind == "head" or previous.kind == "end")
        ):
            raise CoordinationConflict("runtime response frames are out of order")
        sequence = previous.sequence + 1 if previous else 1
        reference = await asyncio.to_thread(
            self.vault.put, content, purpose=self._frame_purpose(command_id, sequence)
        )
        session.add(
            RuntimeDeliveryRow(
                id=str(uuid4()),
                project_id=lease.project_id,
                command_id=command_id,
                sequence=sequence,
                kind=kind,
                content_reference=reference,
            )
        )
        return sequence

    async def complete(self, lease: ProjectLease, command_id: str, *, status_code: int):
        async def terminal(session):
            await self._append_frame(session, lease, command_id, "end", b"")

        # A final frame is visible only in the transaction that commits the
        # command outcome. A cancelled controller cannot publish false success.
        await self.coordination.finish_command(
            lease,
            command_id,
            state="succeeded" if status_code < 400 else "failed",
            result={"http_status": status_code},
            apply=terminal,
        )

    async def lookup(self, actor_id: str, idempotency_key: str) -> str:
        """Find a lost acceptance without submitting or revealing another actor's work."""
        self._intent_purpose(actor_id)
        if re.fullmatch(r"[!-~]{1,200}", idempotency_key) is None:
            raise ValueError(
                "Idempotency-Key must contain 1 to 200 visible ASCII characters"
            )
        async with self.coordination.database.session() as session:
            command_id = await session.scalar(
                select(RuntimeCommandRow.id).where(
                    RuntimeCommandRow.actor_id == actor_id,
                    RuntimeCommandRow.idempotency_key == idempotency_key,
                )
            )
        if command_id is None:
            raise NotFoundError("accepted request is unavailable")
        return command_id

    async def page(
        self, actor_id: str, command_id: str, *, after=0, limit=64
    ) -> DeliveryPage:
        self._intent_purpose(actor_id)
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 128
        ):
            raise ValueError("runtime response cursor is outside its bound")
        async with self.coordination.database.session() as session:
            command = await session.get(RuntimeCommandRow, command_id)
            if command is None or command.actor_id != actor_id:
                raise NotFoundError("accepted request is unavailable")
            rows = list(
                await session.scalars(
                    select(RuntimeDeliveryRow)
                    .where(
                        RuntimeDeliveryRow.command_id == command_id,
                        RuntimeDeliveryRow.sequence > after,
                    )
                    .order_by(RuntimeDeliveryRow.sequence)
                    .limit(limit)
                )
            )
            project_id, state = command.project_id, command.state
        frames = []
        for row in rows:
            content = await asyncio.to_thread(
                self.vault.get,
                row.content_reference,
                purpose=self._frame_purpose(command_id, row.sequence),
            )
            frames.append(DeliveryFrame(row.sequence, row.kind, content))
        return DeliveryPage(command_id, project_id, state, tuple(frames))

    async def chat_binding(self, actor_id: str, command_id: str) -> ChatBinding | None:
        self._intent_purpose(actor_id)
        async with self.coordination.database.session() as session:
            command = await session.get(RuntimeCommandRow, command_id)
            if command is None or command.actor_id != actor_id:
                raise NotFoundError("accepted request is unavailable")
            binding = await session.get(RuntimeChatBindingRow, command_id)
            if binding is None:
                return None
            if command.name not in CHAT_COMMANDS:
                raise CoordinationConflict(
                    "The chat binding does not belong to a typed chat receipt"
                )
            turn = await session.get(EntityRow, binding.turn_id)
            if (
                turn is None
                or turn.kind != "chat_turns"
                or (
                    binding.project_id,
                    turn.engagement_id,
                    turn.payload.get("session_id"),
                )
                != (command.project_id, command.project_id, binding.session_id)
            ):
                raise CoordinationConflict(
                    "The accepted chat binding needs reconciliation"
                )
            return ChatBinding(
                command_id,
                binding.project_id,
                binding.turn_id,
                binding.session_id,
                binding.stream_after,
                command.name == CHAT_ACCEPT_COMMAND or binding.stream_after is not None,
            )

    async def valid_actor(self, actor: str) -> bool:
        self._intent_purpose(actor)
        if actor == "operator":
            return True
        store = NebulaStore(self.coordination.database)
        try:
            device = await store.get(PairedDeviceSession, actor.removeprefix("device:"))
        except NotFoundError:
            # diagnostic-expected: a removed device cannot view or submit a durable command.
            return False
        now = utc_now()
        return (
            device.revoked_at is None
            and now < device.idle_expires_at
            and now < device.absolute_expires_at
        )


class DurableHttpRunner:
    """Execution-owned ASGI delivery; viewer cancellation has no task to cancel."""

    def __init__(self, delivery: RuntimeDelivery, app, *, operator_token: str):
        self.delivery, self.app, self.operator_token = delivery, app, operator_token

    async def run_one(
        self, lease: ProjectLease, *, lane: Literal["work", "control"]
    ) -> bool:
        command = await self.delivery.coordination.claim_next(lease, lane=lane)
        if command is None:
            return False
        try:
            with execution_context(lease):
                await self._invoke(lease, command)
        except BaseException:
            # A controller failure can follow a committed external effect.
            # Keep the receipt, mark uncertainty and never replay automatically.
            async def uncertain():
                await self.delivery.coordination.finish_command(
                    lease,
                    command.receipt.command_id,
                    state="uncertain",
                    result={
                        "detail": "Execution outcome needs explicit reconciliation"
                    },
                )

            await finish_cleanup(uncertain())
            raise
        return True

    async def _invoke(self, lease: ProjectLease, command: ClaimedCommand):
        intent, actor = await self.delivery.request(command)
        command_id = command.receipt.command_id
        if not await self.delivery.valid_actor(actor):
            await self.delivery.append(
                lease,
                command_id,
                "head",
                _json(
                    {
                        "status": 401,
                        "headers": [["content-type", "application/json"]],
                    }
                ),
            )
            await self.delivery.append(
                lease,
                command_id,
                "body",
                _json(
                    {
                        "detail": "The accepting device session expired or was revoked",
                    }
                ),
            )
            await self.delivery.complete(lease, command_id, status_code=401)
            return
        body = intent.body()
        headers = [
            (key.encode(), value.encode("latin-1")) for key, value in intent.headers
        ]
        headers.extend(
            [
                (b"host", b"nebula-runtime"),
                (b"authorization", ("Bearer " + self.operator_token).encode()),
                (b"content-length", str(len(body)).encode()),
            ]
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": intent.method,
            "scheme": "http",
            "path": intent.path,
            "raw_path": intent.path.encode(),
            "query_string": intent.query.encode(),
            "headers": headers,
            "root_path": "",
            "server": ("nebula-runtime", 80),
            "client": None,
            "state": {
                "runtime_command_id": command_id,
                "runtime_chat_acceptance": command.name == CHAT_ACCEPT_COMMAND,
                "runtime_chat_resume": command.name == CHAT_RESUME_COMMAND,
            },
        }
        consumed, started, ended, status_code = False, False, False, 0

        async def receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # A public viewer is not this execution-owned request. Only worker
            # shutdown or loss of ownership can cancel this delivery task.
            await asyncio.Event().wait()

        async def send(message):
            nonlocal started, ended, status_code
            if message["type"] == "http.response.start":
                if started:
                    raise RuntimeIpcError("runtime response was started twice")
                status_code = message["status"]
                if type(status_code) is not int or not 100 <= status_code <= 599:
                    raise RuntimeIpcError("runtime response status is invalid")
                response_headers = [
                    [key.decode("latin-1"), value.decode("latin-1")]
                    for key, value in message.get("headers", [])
                    if key.lower() not in {b"connection", b"transfer-encoding"}
                ]
                await self.delivery.append(
                    lease,
                    command_id,
                    "head",
                    _json(
                        {
                            "status": status_code,
                            "headers": response_headers,
                        }
                    ),
                )
                started = True
            elif message["type"] == "http.response.body":
                if not started or ended:
                    raise RuntimeIpcError("runtime response body is out of order")
                chunk = message.get("body", b"")
                for offset in range(0, len(chunk), MAX_FRAME_BYTES):
                    await self.delivery.append(
                        lease,
                        command_id,
                        "body",
                        chunk[offset : offset + MAX_FRAME_BYTES],
                    )
                if not message.get("more_body", False):
                    ended = True
            else:
                raise RuntimeIpcError("unsupported runtime HTTP response extension")

        attribution = memory_actor.set(actor)
        try:
            with runtime_principal(actor):
                await self.app(scope, receive, send)
        finally:
            memory_actor.reset(attribution)
        if not ended:
            raise RuntimeIpcError("runtime response ended without a durable completion")
        await self.delivery.complete(lease, command_id, status_code=status_code)
