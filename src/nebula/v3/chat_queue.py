"""Durable, revisioned follow-ups dispatched by Core rather than a browser tab."""

from __future__ import annotations
import asyncio
import json
from hashlib import sha256
from uuid import uuid4
from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from .async_utils import cancel_and_join
from .chat import ChatCompletionRequest, ChatHistoryConflict, unarchive_chat_session
from .harnesses import HarnessSkillInvocation
from .chat_turn_headers import latest_turn_header, turn_header
from .conditional_reads import conditional, content_etag, revision_etag
from .database import EntityRow
from .domain import (
    ChatQueue,
    ChatSession,
    ChatBackend,
    Engagement,
    HarnessProfile,
    ProviderProfile,
    PairedDeviceSession,
    utc_now,
)
from .storage import ConflictError, NotFoundError, readable_entities
from .diagnostics import record_caught_exception, create_diagnostic_task

TERMINAL = {"complete", "failed", "cancelled", "interrupted"}
EDITABLE = {"queued", "needs_review"}
# Follow-up item states after which Core never reads the request again.
RETIRED_ITEM_STATUSES = {"complete", "cancelled"}
# Settled follow-ups kept, without their request bodies, so a replayed enqueue
# of the same idempotency key is still recognized and the transcript can link
# a recent item to its turn. Older settled items are dropped.
RETAINED_SETTLED_ITEMS = 20
# Follow-up dispatch is driven by events. Every queue write and turn change a
# project's queues depend on is made by the worker that owns the project,
# through its store, which signals each unit of work as it ends. A queue write
# wakes the dispatcher at once; a turn starting, ending or changing status wakes
# it only for a conversation whose queue waits on that turn. While a queue waits
# on a running response the dispatcher also rechecks at the old half-second
# cadence. Otherwise a safety pass rereads queue versions, so liveness never
# rests on a signal alone.
ACTIVE_POLL_SECONDS = 0.5
SAFETY_POLL_SECONDS = 30.0
# Lets one burst of writes (a claim, then its linked turn) share one pass.
WAKE_COALESCE_SECONDS = 0.05
FAILURE_RETRY_MAX_SECONDS = 5.0
# A sent follow-up whose response waits on one of these rests until it changes.
RESTING_TURNS = {"waiting_approval", "waiting_callback"}


def _request_digest(request: object) -> str | None:
    if request is None:
        return None
    encoded = json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(encoded.encode()).hexdigest()


def _item_request_digest(item: dict) -> str | None:
    return item.get("request_digest") or _request_digest(item.get("original_request"))


def retire_settled_items(items: list[dict]) -> list[dict]:
    """Bound a queue's history: settled items lose their bodies, then the oldest go.

    A completed or cancelled follow-up is never dispatched, edited or retried
    again; its request and original request (the whole message and selected
    context) only kept growing the row that browsers poll every two seconds and
    the worker reads every half second. Undispatched, sending and reviewable
    items are always kept whole.
    """

    settled = [
        index
        for index, item in enumerate(items)
        if item.get("status") in RETIRED_ITEM_STATUSES
    ]
    dropped = set(settled[: max(0, len(settled) - RETAINED_SETTLED_ITEMS)])
    retained: list[dict] = []
    for index, item in enumerate(items):
        if index in dropped:
            continue
        if item.get("status") in RETIRED_ITEM_STATUSES and (
            "request" in item or "original_request" in item
        ):
            digest = _item_request_digest(item)
            item = {
                key: value
                for key, value in item.items()
                if key not in {"request", "original_request"}
            }
            if digest:
                item["request_digest"] = digest
        retained.append(item)
    return retained


def queue_is_dormant(queue: ChatQueue, *, reviews_resting: bool = False) -> bool:
    """True when ``step`` cannot act on this queue until the queue itself changes.

    This mirrors ``step``: it reads other records only for a reviewed item
    linked to a turn, a claiming or sending item, or the latest turn when the
    next editable item is ready to dispatch. Otherwise nothing but a write to
    the queue (which bumps its revision) can give it work.

    With ``reviews_resting``, a reviewed item linked to a turn does not count:
    ``step`` settles it only once that turn's status changes, which is chat
    activity in the conversation rather than time passing.
    """

    if any(
        item["status"] in {"claiming", "sending"}
        or (
            item["status"] == "needs_review"
            and item.get("turn_id")
            and not reviews_resting
        )
        for item in queue.items
    ):
        return False
    if queue.paused:
        return True
    ready = next((item for item in queue.items if item["status"] in EDITABLE), None)
    return ready is None or ready["status"] == "needs_review"


async def link_queue_turn(transaction, claim, turn_id, harness_turn_id=None):
    """Called inside the same transaction that persists the new turn."""
    if claim is None:
        return
    queue_id, revision, item_id = claim
    from .domain import ChatQueue

    row = await transaction.session.get(EntityRow, queue_id)
    if row is None:
        raise ConflictError("Follow-up queue was removed")
    queue = ChatQueue.model_validate(row.payload)
    items = [dict(item) for item in queue.items]
    item = next((item for item in items if item["id"] == item_id), None)
    if item is None or item["status"] != "claiming" or queue.paused:
        raise ConflictError("Follow-up changed before dispatch")
    request = item["request"]
    profile_id = (
        request.get("harness_profile_id")
        if request["backend"] == "harness"
        else request.get("provider_id")
    )
    profile_row = await transaction.session.get(EntityRow, profile_id)
    if (
        not profile_row
        or not profile_row.payload.get("enabled")
        or profile_row.revision != item["profile_revision"]
    ):
        raise ConflictError("Runtime authorization changed during preparation")
    if item.get("device_id"):
        device_row = await transaction.session.get(EntityRow, item["device_id"])
        if device_row is None:
            raise ConflictError("Authorizing device was removed")
        device = PairedDeviceSession.model_validate(device_row.payload)
        now = utc_now()
        if (
            device.revoked_at
            or device.idle_expires_at <= now
            or device.absolute_expires_at <= now
        ):
            raise ConflictError("Authorizing device expired during preparation")
    item.update(status="sending", turn_id=turn_id, harness_turn_id=harness_turn_id)
    (
        await transaction.update(
            ChatQueue, queue_id, {"items": items}, expected_revision=revision
        )
    )


class QueueWrite(BaseModel):
    expected_revision: int = Field(ge=0)
    action: str = Field(
        pattern=r"^(enqueue|edit|remove|reorder|pause|resume|clear|retry)$"
    )
    item_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=200)
    request: ChatCompletionRequest | None = None
    order: list[str] = Field(default_factory=list, max_length=20)
    paused: bool = False
    first: bool = False
    imported_uncertain: bool = False


class ChatQueueService:
    def __init__(self, store, chat, harness):
        self.store, self.chat, self.harness = store, chat, harness
        self.task = None
        # Item ids whose dispatch this process is running right now. A "claiming"
        # row outside this set has no owner: its failure review lost a revision
        # race, or Core restarted mid-dispatch.
        self._inflight = set()
        # Queue id -> revision at which ``step`` found nothing to act on.
        self._dormant = {}
        # Queue id -> (revision, session id) whose open work waits only for a
        # turn of its conversation to change status: a review linked to a turn,
        # or a follow-up behind a response (its own or the conversation's latest)
        # that waits for an approval or callback. Chat activity in that
        # conversation or a safety pass rereads it.
        self._parked = {}
        # Session id -> latest turn header ``step`` read during this pass.
        self._latest_seen = {}
        # Conversations whose queue waits on a running response.
        self._watched = set()
        # Conversations with chat activity during the current pass.
        self._pass_activity = None
        self._wake = asyncio.Event()
        # Set by shutdown: ends the loop even if its cancellation is swallowed
        # in SQL (Python 3.11 wait_for in the pool checkout) and keeps a
        # stopping Core from dispatching another follow-up.
        self._stopping = False
        store.on_follow_up_change(self._signal)

    def _signal(self, queue_written, sessions):
        """A unit of work ended: wake for queue writes and waited-on turns."""
        if self._pass_activity is not None:
            self._pass_activity.update(sessions)
        if queue_written or self._reopen(sessions) or sessions & self._watched:
            self._wake.set()

    def _reopen(self, sessions):
        stale = [key for key, seen in self._parked.items() if seen[1] in sessions]
        for key in stale:
            del self._parked[key]
        return bool(stale)

    async def get(self, session_id):
        session = await self.store.get(ChatSession, session_id)
        try:
            return await self.store.get(ChatQueue, "chat-queue-" + session_id)
        except NotFoundError:  # diagnostic-expected: optional or removed historical record remains unavailable
            return ChatQueue(
                id="chat-queue-" + session_id,
                session_id=session_id,
                engagement_id=session.engagement_id,
            )

    async def exists(self, session_id):
        try:
            (await self.store.get(ChatQueue, "chat-queue-" + session_id))
            return True
        except NotFoundError:  # diagnostic-expected: optional or removed historical record remains unavailable
            return False

    async def latest_turn(self, session_id):
        """The conversation's newest turn, as a header: the worker asks every
        half second and needs only its identity and status."""
        async with self.store.database.session() as database:
            latest = await latest_turn_header(database, session_id)
        self._latest_seen[session_id] = latest
        return latest

    async def turn(self, session_id, turn_id):
        async with self.store.database.session() as database:
            header = await turn_header(database, session_id, turn_id)
        if header is None:
            raise NotFoundError(f"chat turn not found: {turn_id}")
        return header

    async def write(self, session_id, body, device_id=None):
        session = await self.store.get(ChatSession, session_id)
        project_workspace_path = (
            await self.store.get(Engagement, session.engagement_id)
        ).workspace_path
        present = await self.exists(session_id)
        queue = await self.get(session_id)
        items = [dict(item) for item in queue.items]
        if body.action == "enqueue" and body.idempotency_key:
            previous = next(
                (item for item in items if item.get("key") == body.idempotency_key),
                None,
            )
            if previous:
                expected = (
                    body.request.model_dump(mode="json") if body.request else None
                )
                # A settled item keeps only its request digest.
                if _item_request_digest(previous) != _request_digest(expected):
                    raise ConflictError(
                        "This follow-up key was already used for different content"
                    )
                return queue
        if body.expected_revision != (queue.revision if present else 0):
            raise ConflictError(
                "Queue changed on another device. Reload it before reapplying your edit"
            )
        changes = {}
        if body.action in {"enqueue", "edit"}:
            request = body.request
            if (
                request is None
                or len(request.messages) != 1
                or request.messages[0].role.value != "user"
            ):
                raise HTTPException(422, "A follow-up requires one user message")
            if (
                request.session_id != session_id
                or request.engagement_id != session.engagement_id
                or request.backend != session.backend
            ):
                raise HTTPException(
                    422, "Follow-up must use this conversation and project"
                )
            if (
                session.metadata.get("control_assistant") is True
            ) != request.control_assistant:
                raise HTTPException(
                    422,
                    "Butler conversation and follow-up must use the same assistant mode",
                )
            expected_profile = (
                session.harness_profile_id
                if session.backend == ChatBackend.HARNESS
                else session.provider_profile_id
            )
            actual_profile = (
                request.harness_profile_id
                if session.backend == ChatBackend.HARNESS
                else request.provider_id
            )
            # A harness follow-up carries the model picked for its own turn and
            # Core moves the vendor session over when it differs. A provider
            # conversation switches model on its own, before anything is queued.
            if actual_profile != expected_profile or (
                session.backend != ChatBackend.HARNESS
                and request.model != session.model
            ):
                raise HTTPException(
                    422, "Follow-up cannot change the conversation runtime"
                )
            profile = await self.store.get(
                HarnessProfile
                if session.backend == ChatBackend.HARNESS
                else ProviderProfile,
                expected_profile,
            )
            if not profile.enabled:
                raise HTTPException(409, "The selected runtime is disabled")
            if request.backend == ChatBackend.HARNESS and any(
                block.type != "text" for block in request.messages[0].content_blocks
            ):
                raise HTTPException(
                    422,
                    "This harness queue accepts text and selected context. Image submission is not advertised by its chat path.",
                )
            payload = {
                "request": request.model_dump(mode="json"),
                "profile_revision": profile.revision,
                "device_id": device_id,
            }
            if body.action == "enqueue":
                if not body.idempotency_key:
                    raise HTTPException(422, "An idempotency key is required")
                if (
                    sum(
                        item["status"] not in {"complete", "cancelled"}
                        for item in items
                    )
                    >= 20
                ):
                    raise ConflictError(
                        "The queue is full; remove a follow-up before adding another"
                    )
                item = {
                    "id": str(uuid4()),
                    "key": body.idempotency_key,
                    "original_request": request.model_dump(mode="json"),
                    "status": "queued",
                    "created_at": utc_now().isoformat(),
                    **payload,
                }
                if body.first:
                    items.insert(0, item)
                else:
                    items.append(item)
                # A user explicitly enqueueing while idle authorizes moving on from a previous failure.
                latest = await self.latest_turn(session_id)
                if latest and latest.status in TERMINAL:
                    changes["resume_after_turn_id"] = latest.id
                if not present or body.paused or body.imported_uncertain:
                    changes["paused"] = body.paused or body.imported_uncertain
                if body.imported_uncertain:
                    item.update(
                        status="needs_review",
                        detail="Imported browser delivery is uncertain. Inspect the conversation before retrying.",
                    )
            else:
                item = next(
                    (item for item in items if item["id"] == body.item_id), None
                )
                if item is None or item["status"] not in EDITABLE:
                    raise ConflictError("Only undispatched follow-ups can be edited")
                item.update(payload)
        elif body.action in {"pause", "resume"}:
            changes["paused"] = body.action == "pause"
            latest = await self.latest_turn(session_id)
            if body.action == "resume":
                changes["resume_after_turn_id"] = latest.id if latest else None
        elif body.action in {"remove", "clear"}:
            for item in items:
                if body.action == "clear" or item["id"] == body.item_id:
                    if item["status"] in EDITABLE or (
                        item["status"] == "claiming"
                        and item["id"] not in self._inflight
                    ):
                        # An unowned claim is safe to drop: link_queue_turn refuses
                        # a claim that is no longer "claiming".
                        item["status"] = "cancelled"
                    elif body.action == "remove":
                        raise ConflictError(
                            "This follow-up is being dispatched"
                            if item["status"] == "claiming"
                            else "This follow-up has already been dispatched"
                        )
        elif body.action == "reorder":
            editable = [item for item in items if item["status"] in EDITABLE]
            if len(body.order) != len(editable) or set(body.order) != {
                item["id"] for item in editable
            }:
                raise ConflictError("Queue order is stale; reload before reordering")
            by_id = {item["id"]: item for item in editable}
            items = [item for item in items if item["status"] not in EDITABLE] + [
                by_id[key] for key in body.order
            ]
        elif body.action == "retry":
            item = next((item for item in items if item["id"] == body.item_id), None)
            if not item or item["status"] != "needs_review":
                raise ConflictError("Only a reviewed failed follow-up can be retried")
            if (
                sum(row["status"] not in {"complete", "cancelled"} for row in items)
                >= 20
            ):
                raise ConflictError("Remove a follow-up first")
            item["status"] = "cancelled"
            retry = {
                key: value
                for key, value in item.items()
                if key not in {"turn_id", "harness_turn_id", "detail"}
            }
            retry.update(
                id=str(uuid4()),
                key=str(uuid4()),
                status="queued",
                replaces_id=item["id"],
                device_id=device_id,
            )
            items.append(retry)
            changes["paused"] = True
        changes["items"] = retire_settled_items(items)
        async with self.store.transaction(immediate=True) as transaction:
            (
                await transaction.assert_project_workspace(
                    session.engagement_id, project_workspace_path
                )
            )
            if present:
                return await transaction.update(
                    ChatQueue,
                    queue.id,
                    changes,
                    expected_revision=body.expected_revision,
                )
            return await transaction.add(queue.model_copy(update=changes))

    async def review(self, queue, item_id, detail):
        items = [dict(item) for item in queue.items]
        for item in items:
            if item["id"] == item_id:
                item.update(status="needs_review", detail=detail)
        return await self.store.update(
            ChatQueue,
            queue.id,
            {"items": items, "paused": True},
            expected_revision=queue.revision,
        )

    async def authorization_valid(self, item):
        device_id = item.get("device_id")
        if device_id:
            device = await self.store.get(PairedDeviceSession, device_id)
            now = utc_now()
            if (
                device.revoked_at
                or device.idle_expires_at <= now
                or device.absolute_expires_at <= now
            ):
                raise ConflictError(
                    "The device that authorized this follow-up has expired or was revoked"
                )
        request = ChatCompletionRequest.model_validate(item["request"])
        profile = await self.store.get(
            HarnessProfile
            if request.backend == ChatBackend.HARNESS
            else ProviderProfile,
            request.harness_profile_id
            if request.backend == ChatBackend.HARNESS
            else request.provider_id,
        )
        if not profile.enabled or profile.revision != item["profile_revision"]:
            raise ConflictError(
                "Runtime configuration changed. Review and edit the follow-up before resuming"
            )
        return request

    async def _requeue_claim(self, queue_id, item_id):
        """Return an unlinked claim to the queue so the next poll retries it."""
        for _ in range(3):
            current = await self.store.get(ChatQueue, queue_id)
            items = [dict(row) for row in current.items]
            row = next((row for row in items if row["id"] == item_id), None)
            if row is None or row["status"] != "claiming":
                return
            row["status"] = "queued"
            try:
                (
                    await self.store.update(
                        ChatQueue,
                        current.id,
                        {"items": items},
                        expected_revision=current.revision,
                    )
                )
                return
            except (
                ConflictError
            ):  # diagnostic-expected: reread the queue and retry the requeue
                continue

    async def _session_busy(self, session_id, seen):
        """True when a response the drainer did not start now occupies the session."""
        current = await self.latest_turn(session_id)
        if current is None:
            return False
        if seen is None or current.id != seen.id:
            return True
        return current.status not in TERMINAL

    async def _review_failed_dispatch(self, queue_id, item_id, exc):
        record_caught_exception(
            "chat",
            "chat.queue.dispatch_failed",
            "A queued follow-up could not be dispatched",
            exc,
            stage="queue",
        )
        current = await self.store.get(ChatQueue, queue_id)
        (
            await self.review(
                current,
                item_id,
                "Follow-up could not be dispatched. Inspect the conversation before retrying",
            )
        )

    async def step(self, queue, recovering=False):
        # Older releases classified deliberate stops as failures needing review,
        # and a review written while a turn was only parked (waiting_callback
        # across a restart) is stale once that turn finishes. Reconcile from the
        # durable turn without re-dispatching its request.
        settled = {}
        for item in queue.items:
            if item["status"] == "needs_review" and item.get("turn_id"):
                status = (await self.turn(queue.session_id, item["turn_id"])).status
                if status in {"cancelled", "complete"}:
                    settled[item["id"]] = status
        if settled:
            items = []
            for item in queue.items:
                if item["id"] not in settled:
                    items.append(item)
                elif settled[item["id"]] == "cancelled":
                    items.append(
                        {**item, "status": "cancelled", "detail": "Stopped by operator"}
                    )
                else:
                    completed = {**item, "status": "complete"}
                    completed.pop("detail", None)
                    items.append(completed)
            (
                await self.store.update(
                    ChatQueue,
                    queue.id,
                    {
                        "items": retire_settled_items(items),
                        "paused": queue.paused or "cancelled" in settled.values(),
                    },
                    expected_revision=queue.revision,
                )
            )
            return
        sending = next(
            (item for item in queue.items if item["status"] in {"claiming", "sending"}),
            None,
        )
        if sending:
            if sending["status"] == "claiming":
                if sending["id"] not in self._inflight:
                    (
                        await self.review(
                            queue,
                            sending["id"],
                            "Core restarted during dispatch. Review before retrying as a new message"
                            if recovering
                            else "Dispatch was interrupted before a response was linked. Review before retrying as a new message",
                        )
                    )
                return
            turn = await self.turn(queue.session_id, sending["turn_id"])
            if turn.status in TERMINAL:
                if turn.status not in {"complete", "cancelled"}:
                    (
                        await self.review(
                            queue,
                            sending["id"],
                            f"Response stopped or failed: {turn.error or turn.status}. Inspect its source turn before retrying as a new message",
                        )
                    )
                else:
                    items = [dict(item) for item in queue.items]
                    next(item for item in items if item["id"] == sending["id"])[
                        "status"
                    ] = turn.status
                    (
                        await self.store.update(
                            ChatQueue,
                            queue.id,
                            {
                                "items": retire_settled_items(items),
                                "paused": queue.paused or turn.status == "cancelled",
                            },
                            expected_revision=queue.revision,
                        )
                    )
            elif turn.status in {"waiting_approval", "waiting_callback"}:
                # Pending input or a background result blocks dispatch; resolving
                # it allows this same turn to finish.
                return
            elif (
                turn.backend == ChatBackend.PROVIDER.value
                and self.chat.has_active_provider_turn(turn.id)
            ):
                # Startup already reclaimed this safe turn. Keep its linked
                # follow-up sending until the same turn reaches a durable end.
                return
            elif recovering:
                (
                    await self.review(
                        queue,
                        sending["id"],
                        "Core restarted with a response in progress. Delivery is uncertain; inspect the conversation",
                    )
                )
            return
        if queue.paused:
            return
        item = next((item for item in queue.items if item["status"] in EDITABLE), None)
        if not item or item["status"] == "needs_review":
            return
        latest = await self.latest_turn(queue.session_id)
        if latest and latest.status not in TERMINAL:
            return
        if (
            latest
            and latest.status != "complete"
            and queue.resume_after_turn_id != latest.id
        ):
            (
                await self.review(
                    queue,
                    item["id"],
                    "Previous response stopped or failed. Review the next message before resuming",
                )
            )
            return
        self._inflight.add(item["id"])
        try:
            request = await self.authorization_valid(item)
            if request.backend == ChatBackend.PROVIDER:
                # Model and effort are conversation settings the operator can
                # change while earlier work runs; the follow-up is the next turn,
                # so it uses the current ones. Another provider is a different
                # authorization, which the operator reviews instead.
                session = await self.store.get(ChatSession, queue.session_id)
                if session.provider_profile_id != request.provider_id:
                    (
                        await self.review(
                            queue,
                            item["id"],
                            "The conversation moved to another provider after this message was queued. Review it before sending",
                        )
                    )
                    return
                request = request.model_copy(
                    update={
                        "model": session.model,
                        "reasoning_effort": session.metadata.get(
                            "reasoning_effort", request.reasoning_effort
                        ),
                    }
                )
            items = [dict(row) for row in queue.items]
            next(row for row in items if row["id"] == item["id"])["status"] = "claiming"
            claimed = await self.store.update(
                ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
            )
            claim = (claimed.id, claimed.revision, item["id"])
            if request.backend == ChatBackend.PROVIDER:
                request.stream = True
                request._queue_claim = claim
                prepared = await self.chat.prepare_async(request)
                await self.chat.start_provider_turn(prepared)
            else:
                context = (
                    "\n\nNebula-selected context:\n"
                    + json.dumps(
                        [
                            entry.model_dump(mode="json")
                            for entry in request.context_attachments
                        ],
                        ensure_ascii=False,
                    )
                    if request.context_attachments
                    else ""
                )
                _, _, turn = await self.harness.prepare_chat(
                    engagement_id=request.engagement_id,
                    profile_id=request.harness_profile_id,
                    model=request.model,
                    prompt=request.messages[-1].content,
                    chat_session_id=request.session_id,
                    harness_session_id=None,
                    mcp_server_ids=request.mcp_server_ids,
                    runtime_context=context,
                    context_attachments=[
                        entry.model_dump(mode="json")
                        for entry in request.context_attachments
                    ],
                    allow_remote_mcp=request.allow_cloud_tool_results,
                    include_knowledge=request.include_knowledge,
                    allow_cloud_knowledge=request.allow_cloud_knowledge,
                    max_artifact_queries=request.max_artifact_queries,
                    harness_mode=request.harness_mode,
                    planning_enabled=request.planning_enabled,
                    harness_skill=HarnessSkillInvocation.model_validate(
                        request.harness_skill
                    )
                    if request.harness_skill
                    else None,
                    harness_reasoning_effort=request.harness_reasoning_effort,
                    harness_service_tier=request.harness_service_tier,
                    provider_subagent=request.harness_provider_subagent(),
                    pending_provider_subagent=request.harness_pending_provider_subagent(),
                    allow_agent_messaging=request.allow_agent_messaging,
                    control_assistant=request.control_assistant,
                    queue_claim=claim,
                )
                await self.harness.start_chat_turn(turn.id)
        except (
            ConflictError
        ):  # diagnostic-expected: concurrent mutation wins and dispatch is paused
            # A concurrent edit/pause or turn reservation wins. Do not run a stale request.
            current = await self.store.get(ChatQueue, queue.id)
            if any(
                row["id"] == item["id"] and row["status"] == "claiming"
                for row in current.items
            ):
                (
                    await self.review(
                        current,
                        item["id"],
                        "Queue or runtime changed during dispatch. Reload and review this message",
                    )
                )
            elif current.revision == queue.revision:
                (
                    await self.review(
                        current,
                        item["id"],
                        "Runtime authorization changed. Edit the follow-up to review its configuration",
                    )
                )
        except ChatHistoryConflict as exc:  # diagnostic-expected: a direct Send that won the race is busy, not a failure
            # The conversation grew a response this drainer did not start: an
            # operator's direct Send won the race. That is not a failure of the
            # follow-up, so leave it queued and let a later poll dispatch it once
            # the response settles instead of parking the queue for review.
            if await self._session_busy(queue.session_id, latest):
                (await self._requeue_claim(queue.id, item["id"]))
            else:
                (await self._review_failed_dispatch(queue.id, item["id"], exc))
        except Exception as exc:  # diagnostic-expected: recorded and parked for review by _review_failed_dispatch
            (await self._review_failed_dispatch(queue.id, item["id"], exc))
        finally:
            self._inflight.discard(item["id"])

    async def queues(self, *, skip_dormant=False):
        """Every queue, or with ``skip_dormant`` only those ``step`` could act on.

        Row versions are read without payloads; a queue last seen dormant at
        its current revision is not parsed again until a write changes it.
        """
        async with self.store.database.session() as database:
            versions = dict(
                (
                    await database.execute(
                        select(EntityRow.id, EntityRow.revision).where(
                            EntityRow.kind == "chat_queues"
                        )
                    )
                ).all()
            )
            # Forget queues that were deleted.
            self._dormant = {
                key: revision
                for key, revision in self._dormant.items()
                if key in versions
            }
            self._parked = {
                key: seen for key, seen in self._parked.items() if key in versions
            }
            wanted = [
                key
                for key, revision in versions.items()
                if not skip_dormant
                or (
                    self._dormant.get(key) != revision
                    and self._parked.get(key, (None,))[0] != revision
                )
            ]
            if not wanted:
                return []
            # An unreadable queue is skipped and recorded; it must not stop
            # Core starting or every other conversation's follow-ups.
            return readable_entities(
                (
                    await database.scalars(
                        select(EntityRow).where(
                            EntityRow.kind == "chat_queues", EntityRow.id.in_(wanted)
                        )
                    )
                ),
                ChatQueue,
            )

    def _remember_dormancy(self, queue):
        """True, and remembered, when only a write to the queue gives it work."""
        self._parked.pop(queue.id, None)
        if queue_is_dormant(queue):
            self._dormant[queue.id] = queue.revision
            return True
        self._dormant.pop(queue.id, None)
        return False

    async def _waits_on_turn(self, queue):
        """True while ``queue`` waits on a response that is still running.

        A queue that waits only for a turn of its conversation to change
        status is parked instead: chat activity in that conversation, a write
        to the queue or a safety pass reads it again.
        """
        if self._remember_dormancy(queue):
            return False
        if any(item["status"] == "claiming" for item in queue.items):
            return True
        sending = next(
            (item for item in queue.items if item["status"] == "sending"), None
        )
        if sending is not None:
            if not sending.get("turn_id"):
                return True
            turn = await self.turn(queue.session_id, sending["turn_id"])
            if turn.status not in RESTING_TURNS:
                return True
        elif not queue_is_dormant(queue, reviews_resting=True):
            # The next follow-up is ready; step dispatches it once the latest
            # turn ends. One waiting for an approval or callback can take hours.
            if queue.session_id in self._latest_seen:
                latest = self._latest_seen[queue.session_id]
            else:
                latest = await self.latest_turn(queue.session_id)
            if latest is None or latest.status not in RESTING_TURNS:
                return True
        self._parked[queue.id] = (queue.revision, queue.session_id)
        return False

    async def startup(self):
        self._stopping = False
        for queue in await self.queues():
            try:
                await self.step(queue, recovering=True)
            except (
                ConflictError,
                NotFoundError,
            ):  # diagnostic-expected: concurrent queue changes will be reread by the worker
                continue
            except Exception as exc:
                # The worker retries this queue; the others still recover.
                record_caught_exception(
                    "chat",
                    "chat.queue.restart_recovery_failed",
                    "A conversation's follow-ups could not be recovered at startup; the worker retries.",
                    exc,
                    stage="queue",
                )
        self.task = create_diagnostic_task(
            self.run(),
            feature="chat",
            event_code="chat.queue.worker_failed",
            failure_message="Core follow-up dispatch stopped unexpectedly",
            name="nebula-chat-follow-ups",
        )

    async def reconcile(self):
        """Step every queue that can act; return conversations waiting on a turn."""
        watched = set()
        for queue in await self.queues(skip_dormant=True):
            if self._stopping:
                break
            try:
                self._latest_seen.pop(queue.session_id, None)
                await self.step(queue)
                if await self._waits_on_turn(queue):
                    watched.add(queue.session_id)
            except (
                ConflictError,
                NotFoundError,
            ):  # diagnostic-expected: reload concurrent queue changes on the next poll
                # Concurrent edits/deletion are re-read on the next iteration.
                watched.add(queue.session_id)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.queue.poll_failed",
                    "Follow-up reconciliation failed",
                    exc,
                    stage="queue",
                )
                watched.add(queue.session_id)
        self._latest_seen.clear()
        return watched

    async def run(self):
        loop = asyncio.get_running_loop()
        safety_at = loop.time() + SAFETY_POLL_SECONDS
        failures = 0
        while not self._stopping:
            if loop.time() >= safety_at:
                self._parked.clear()
                safety_at = loop.time() + SAFETY_POLL_SECONDS
            # Clear before reading so a write during this pass wakes the next.
            self._wake.clear()
            self._pass_activity = set()
            try:
                self._watched = await self.reconcile()
                failures = 0
            except Exception as exc:
                # An unavailable database must not end dispatch for good.
                failures += 1
                if failures == 1:
                    record_caught_exception(
                        "chat",
                        "chat.queue.read_failed",
                        "Follow-up queues could not be read; dispatch keeps retrying.",
                        exc,
                        stage="queue",
                    )
                await asyncio.sleep(
                    min(
                        FAILURE_RETRY_MAX_SECONDS,
                        ACTIVE_POLL_SECONDS * 2 ** min(failures, 10),
                    )
                )
                continue
            finally:
                activity, self._pass_activity = self._pass_activity, None
            if self._stopping:
                return
            # A turn that changed while this pass read it is read again.
            if self._reopen(activity) or activity & self._watched:
                self._wake.set()
            deadline = (
                min(safety_at, loop.time() + ACTIVE_POLL_SECONDS)
                if self._watched
                else safety_at
            )
            try:
                # asyncio.timeout never swallows the cancellation that stops
                # this task, unlike wait_for on Python 3.11.
                async with asyncio.timeout_at(deadline):
                    await self._wake.wait()
            except TimeoutError:
                # diagnostic-expected: the poll interval elapsed without a wake; the next pass rereads queue versions.
                continue
            await asyncio.sleep(WAKE_COALESCE_SECONDS)

    async def shutdown(self):
        self._stopping = True
        # Wake a waiting loop so it sees the flag even if a cancel is lost.
        self._wake.set()
        if self.task:
            await cancel_and_join(self.task)


def queue_router(service):
    router = APIRouter(tags=["chat"])

    @router.get("/chat/sessions/{session_id}/queue")
    async def get_queue(session_id: str, request: Request, response: Response):
        queue = await service.get(session_id)
        value = queue.model_dump(mode="json")
        present = await service.exists(session_id)
        if not present:
            value["revision"] = 0
        # Open conversations poll this; an unchanged queue answers 304. A queue
        # not created yet is synthesized with fresh timestamps on every read,
        # so its validator names the absence instead of that content.
        etag = (
            content_etag("queue", value)
            if present
            else revision_etag("queue", "absent")
        )
        unchanged = conditional(request, response, etag)
        return unchanged or value

    @router.post("/chat/sessions/{session_id}/queue")
    async def write_queue(session_id: str, body: QueueWrite, request: Request):
        queue = await service.write(
            session_id, body, getattr(request.state, "auth_device_id", None)
        )
        if body.action == "enqueue":
            (await unarchive_chat_session(service.store, session_id))
        return queue

    return router
