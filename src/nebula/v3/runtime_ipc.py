"""Authenticated, versioned local RPC without credentials in argv or receipts."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
import httpx

from .deployment_protocol import IPC_PROTOCOL

RPC_PATH = "/internal/nebula-runtime/v1/call"
MAX_RPC_BYTES = 2 * 1024**2
RPC_CLOCK_WINDOW = 30


class RuntimeIpcError(RuntimeError):
    pass


def _encode(value) -> bytes:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(encoded) > MAX_RPC_BYTES:
        raise RuntimeIpcError("runtime IPC message exceeds its bound")
    return encoded


def _mac(secret: bytes, *parts: str | bytes) -> str:
    message = b"\0".join(
        part.encode() if isinstance(part, str) else part for part in parts
    )
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class RuntimeCredentials:
    def __init__(self, directory: Path):
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = self.directory.stat()
        if details.st_mode & 0o077 or details.st_uid != os.getuid():
            raise RuntimeIpcError(
                "runtime credentials require an owner-only host directory"
            )

    def create(self) -> tuple[str, bytes]:
        reference, secret = str(uuid4()), secrets.token_bytes(32)
        path = self.directory / reference
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(secret)
            output.flush()
            os.fsync(output.fileno())
        return reference, secret

    def read(self, reference: str) -> bytes:
        from uuid import UUID

        try:
            if not isinstance(reference, str):
                raise ValueError()
            if str(UUID(reference)) != reference:
                raise ValueError()
        except ValueError as error:
            raise RuntimeIpcError("invalid runtime credential reference") from error
        descriptor = os.open(
            self.directory / reference, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_mode & 0o077
                or details.st_uid != os.getuid()
            ):
                raise RuntimeIpcError("runtime credential file is not private")
            secret = source.read(33)
        if len(secret) != 32:
            raise RuntimeIpcError("runtime credential file is invalid")
        return secret


class RuntimeRpcServer:
    def __init__(self, secret: bytes, *, clock: Callable[[], float] = time.time):
        if len(secret) != 32:
            raise ValueError("runtime IPC requires a 256-bit private credential")
        self.secret, self.clock = secret, clock
        self.operations: dict[str, Callable[[dict], Awaitable[Any]]] = {}
        self.seen: OrderedDict[str, int] = OrderedDict()
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.app.add_api_route(RPC_PATH, self.call, methods=["POST"])

    def register(
        self, operation: str, handler: Callable[[dict], Awaitable[Any]]
    ) -> None:
        if not operation or len(operation) > 100 or operation in self.operations:
            raise ValueError("register one bounded, explicit runtime operation")
        self.operations[operation] = handler

    async def call(self, request: Request) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_RPC_BYTES:
                raise HTTPException(413, "runtime IPC message exceeds its bound")
            body.extend(chunk)
        raw = bytes(body)
        timestamp, nonce = (
            request.headers.get("X-Nebula-IPC-Time", ""),
            request.headers.get("X-Nebula-IPC-Nonce", ""),
        )
        supplied = request.headers.get("X-Nebula-IPC-MAC", "")
        now = int(self.clock())
        if (
            not timestamp.isdecimal()
            or len(timestamp) > 20
            or abs(now - int(timestamp)) > RPC_CLOCK_WINDOW
            or len(nonce) != 48
            or any(character not in "abcdef0123456789" for character in nonce)
            or len(supplied) != 64
            or any(character not in "abcdef0123456789" for character in supplied)
            or not hmac.compare_digest(
                supplied, _mac(self.secret, "request", timestamp, nonce, raw)
            )
        ):
            raise HTTPException(401, "authenticated runtime IPC is required")
        while self.seen and next(iter(self.seen.values())) < now - RPC_CLOCK_WINDOW:
            self.seen.popitem(last=False)
        if nonce in self.seen:
            raise HTTPException(409, "runtime IPC nonce was already used")
        if len(self.seen) >= 4096:
            raise HTTPException(429, "runtime IPC authentication capacity is busy")
        self.seen[nonce] = int(timestamp)
        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            # diagnostic-expected: unactionable protocol input never reaches a controller.
            raise HTTPException(422, "invalid runtime IPC message") from error
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"protocol", "request_id", "operation", "payload"}
            or envelope["protocol"] != IPC_PROTOCOL
            or not isinstance(envelope["request_id"], str)
            or not 1 <= len(envelope["request_id"]) <= 200
            or not isinstance(envelope["payload"], dict)
            or not isinstance(envelope["operation"], str)
            or not 1 <= len(envelope["operation"]) <= 100
        ):
            raise HTTPException(422, "unsupported runtime IPC envelope")
        operation = self.operations.get(envelope["operation"])
        if operation is None:
            raise HTTPException(404, "runtime operation is unavailable")
        result = await operation(envelope["payload"])
        response = _encode(
            {
                "protocol": IPC_PROTOCOL,
                "request_id": envelope["request_id"],
                "result": result,
            }
        )
        return Response(
            response,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "X-Nebula-IPC-MAC": _mac(self.secret, "response", nonce, response),
            },
        )


class RuntimeRpcClient:
    def __init__(
        self,
        endpoint: str,
        secret: bytes,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not endpoint.startswith("unix://") or len(secret) != 32:
            raise RuntimeIpcError(
                "runtime IPC requires a private local endpoint and credential"
            )
        self.endpoint, self.secret = endpoint, secret
        self.client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(uds=endpoint[7:]),
            base_url="http://nebula-runtime",
            timeout=30,
            trust_env=False,
        )

    async def call(
        self,
        operation: str,
        payload: dict,
        *,
        request_id: str | None = None,
        timeout: float = 30,
    ) -> Any:
        identity = request_id or str(uuid4())
        body = _encode(
            {
                "protocol": IPC_PROTOCOL,
                "request_id": identity,
                "operation": operation,
                "payload": payload,
            }
        )
        timestamp, nonce = str(int(time.time())), secrets.token_hex(24)
        async with self.client.stream(
            "POST",
            RPC_PATH,
            content=body,
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
                "X-Nebula-IPC-Time": timestamp,
                "X-Nebula-IPC-Nonce": nonce,
                "X-Nebula-IPC-MAC": _mac(
                    self.secret, "request", timestamp, nonce, body
                ),
            },
        ) as response:
            if response.is_error:
                raise RuntimeIpcError(
                    f"runtime IPC operation failed ({response.status_code})"
                )
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise RuntimeIpcError(
                    "runtime IPC compressed responses are unsupported"
                )
            buffered = bytearray()
            if response.is_stream_consumed:
                # In-process test transports may supply an already-buffered response.
                buffered.extend(response.content)
                if len(buffered) > MAX_RPC_BYTES:
                    raise RuntimeIpcError("runtime IPC response exceeds its bound")
            else:
                async for chunk in response.aiter_raw():
                    if len(buffered) + len(chunk) > MAX_RPC_BYTES:
                        raise RuntimeIpcError("runtime IPC response exceeds its bound")
                    buffered.extend(chunk)
            raw = bytes(buffered)
            supplied = response.headers.get("X-Nebula-IPC-MAC", "")
        if (
            len(supplied) != 64
            or any(character not in "abcdef0123456789" for character in supplied)
            or not hmac.compare_digest(
                supplied, _mac(self.secret, "response", nonce, raw)
            )
        ):
            raise RuntimeIpcError("runtime IPC response authentication failed")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise RuntimeIpcError(
                "runtime IPC response is not a valid envelope"
            ) from error
        if (
            not isinstance(result, dict)
            or set(result) != {"protocol", "request_id", "result"}
            or result.get("protocol") != IPC_PROTOCOL
            or result.get("request_id") != identity
        ):
            raise RuntimeIpcError(
                "runtime IPC response belongs to another request or protocol"
            )
        return result["result"]

    async def close(self):
        await self.client.aclose()
