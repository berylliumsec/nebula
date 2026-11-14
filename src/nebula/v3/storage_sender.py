"""Storage sender contract shared by Core and the supervisor's API workers.

A storage sender authenticates with its own source token, never the operator's.
API workers authenticate it before saving a durable command, so this module
stays free of the chat runtime that only execution workers load.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator

from .domain import NebulaModel, StorageSource
from .runtime_coordination import STORAGE_REPORT_COMMAND
from .storage import NebulaStore, NotFoundError

MAX_EVENT_BYTES = 64 * 1024
# Durable principal for a report a storage source sent through an API worker.
STORAGE_SOURCE_ACTOR = "storage-source:"
STORAGE_EVENTS_PATH = "/api/v1/storage/events"
STORAGE_INVENTORY_PATH = "/api/v1/storage/inventory"
STORAGE_TEST_PATH = "/api/v1/storage/test"
# The only requests a storage-source principal may submit as durable commands.
STORAGE_REPORT_PATHS = frozenset({STORAGE_EVENTS_PATH, STORAGE_INVENTORY_PATH})
# A release whose execution workers can run STORAGE_REPORT_COMMAND.
STORAGE_REPORT_FEATURE = STORAGE_REPORT_COMMAND
# How long an API worker waits for the project's worker before acknowledging a
# saved report as queued. Well inside the reference sender's 30 s timeout, and
# short enough that a sender cannot hold a viewer slot for long.
STORAGE_ACK_WAIT_SECONDS = 5.0
# Sender reports wait in their own small pool, never the operator's viewers.
STORAGE_SENDER_SLOTS = 4


class StorageIdentity(NebulaModel):
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    label: str = Field(min_length=1, max_length=300)


class StorageVolumeReport(StorageIdentity):
    path: str | None = Field(default=None, max_length=2048)


class StorageEvent(NebulaModel):
    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    observed_at: datetime
    kind: Literal["snapshot", "transition"] = "snapshot"
    device: StorageIdentity
    volume: StorageVolumeReport
    state: Literal["healthy", "warning", "critical", "unavailable"]
    free_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at requires a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def coherent_bytes(self) -> "StorageEvent":
        if self.state != "unavailable" and (
            self.free_bytes is None or self.total_bytes is None
        ):
            raise ValueError("available volumes require free_bytes and total_bytes")
        if self.total_bytes is not None and self.free_bytes is not None:
            if self.total_bytes <= 0 or self.free_bytes > self.total_bytes:
                raise ValueError("total_bytes must be positive and at least free_bytes")
        return self


class StorageInventory(NebulaModel):
    schema_version: Literal[1]
    devices: list[StorageIdentity] = Field(max_length=500)


def sender_transport_allowed(scheme: str, client_host: str | None) -> bool:
    """Remote senders use HTTPS; a sender on the Core host may use loopback HTTP."""
    if scheme == "https":
        return True
    client = client_host or ""
    try:
        return ipaddress.ip_address(client).is_loopback
    except ValueError:
        # diagnostic-expected: a DNS client name is not an IP literal.
        return client == "localhost"


async def authenticate_storage_source(
    store: NebulaStore, authorization: str | None, *, allow_disabled: bool = False
) -> StorageSource:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "storage source bearer token required")
    raw = authorization[7:]
    source_id, dot, _ = raw.partition(".")
    if not dot or not source_id or len(raw) > 300:
        raise HTTPException(401, "invalid storage source token")
    try:
        source = await store.get(StorageSource, source_id)
    except NotFoundError as exc:
        raise HTTPException(401, "invalid storage source token") from exc
    if not hmac.compare_digest(
        hashlib.sha256(raw.split(".", 1)[1].encode()).hexdigest(),
        source.token_sha256,
    ):
        raise HTTPException(401, "invalid storage source token")
    if not source.enabled and not allow_disabled:
        raise HTTPException(403, "storage source is disabled")
    return source


def validate_storage_report(path: str, body: bytes) -> None:
    """Reject a report Core would refuse before it is saved or acknowledged."""
    if path == STORAGE_INVENTORY_PATH:
        StorageInventory.model_validate_json(body)
    else:
        StorageEvent.model_validate_json(body)


def storage_report_key(source: StorageSource, path: str, body: bytes) -> str:
    """Durable idempotency key for one sender request.

    An event's retry is the same command for the same project, and the event ID
    dedupes it again in Core. Inventory has no report identity and is safe to
    run again, so each inventory request is its own command.
    """
    if path == STORAGE_INVENTORY_PATH:
        return "storage-inventory-" + uuid4().hex
    digest = hashlib.sha256(
        source.engagement_id.encode() + b"\0" + path.encode() + b"\0" + body
    ).hexdigest()
    return "storage-event-" + digest


def storage_source_actor(source: StorageSource) -> str:
    """Bind the authenticated source and the token digest it presented."""
    return f"{STORAGE_SOURCE_ACTOR}{source.id}:{source.token_sha256}"


def parse_storage_source_actor(actor: str | None) -> tuple[str, str] | None:
    """Return (source id, token digest) for a storage-source principal."""
    if not actor or not actor.startswith(STORAGE_SOURCE_ACTOR):
        return None
    source_id, _, digest = actor.removeprefix(STORAGE_SOURCE_ACTOR).rpartition(":")
    if (
        not source_id
        or len(actor) > 200
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or any(character in source_id for character in "\x00\r\n")
    ):
        return None
    return source_id, digest


async def revalidate_storage_source(
    store: NebulaStore, actor: str | None
) -> StorageSource | None:
    """The source behind a durable report, if it is still enabled with that token.

    Disabling, revoking or rotating a source after its report was saved stops
    that report from running; the sender authenticated a token that is gone.
    """
    parsed = parse_storage_source_actor(actor)
    if parsed is None:
        return None
    source_id, digest = parsed
    try:
        source = await store.get(StorageSource, source_id)
    except NotFoundError:
        # diagnostic-expected: a removed source cannot run a report it saved earlier.
        return None
    if not source.enabled or not hmac.compare_digest(source.token_sha256, digest):
        return None
    return source
