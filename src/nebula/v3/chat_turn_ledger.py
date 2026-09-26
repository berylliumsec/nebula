"""Normalized, append-only provider turn history and deterministic checkpoints."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .context import estimate_tokens
from .database import ChatTurnCheckpointRow, ChatTurnStepEventRow, Database
from .domain import ChatTurn, utc_now
from .tool_activity import clipped, lookup_identifiers, result_summary, step_brief
from .tool_results import without_results_api_key

# The checkpoint advances in blocks: once this many steps, or this many
# tokens of them, have left the recent window since the last advance.
CHECKPOINT_STEP_INTERVAL = 16
CHECKPOINT_TOKEN_TRIGGER = 24_000
# The receipts' byte bound. It grows with the model's input capacity, from
# this floor to the ceiling: a 1M-token model can hold far more of a long
# turn's index than a 32K one, and the floor is what every model had before.
CHECKPOINT_BYTE_LIMIT = 16 * 1024
CHECKPOINT_BYTE_CEILING = 64 * 1024
# The share of input capacity the receipts may take, and the bytes one
# estimated token stands for (``context.estimate_tokens`` counts 3).
_CHECKPOINT_CAPACITY_SHARE = 0.03
_BYTES_PER_TOKEN = 3
RECENT_RESPONSE_GROUPS = 8
CHECKPOINT_SCHEMA = "nebula.chat-turn-checkpoint/v3"
_CHECKPOINT_STEP_FIELDS = [
    "number",
    "tool_index",
    "state",
    "did",
    "summary",
    "artifacts",
    "failure",
]
_RECEIPT_SUMMARY_CHARS = 200
# A v1 checkpoint, written while failed and denied steps were replayed whole
# on every request, never covered one of them.
_V1_UNCOVERED_STATUSES = frozenset({"failed", "denied"})
# A step still waiting on the operator or a callback is never folded.
PENDING_STATUSES = frozenset({"waiting_approval", "waiting_callback"})
# Replay fields every row of one step repeats: the issuing response's
# reasoning and prose and the call's own signature. The step's first row keeps
# them; a later row that repeats them verbatim names that row
# (``replay_from``).
SHARED_REPLAY_FIELDS = ("reasoning_state", "response_text", "provider_metadata")
_REPLAY_FROM = "replay_from"
_FAILURE_SCHEMA = "nebula.tool-failure/v1"
_RESTART_UNKNOWN_SCHEMA = "nebula.restart-uncertain/v1"
# Turns whose folded history one ledger keeps in memory. A routing step reads
# its turn's history several times; the rows it already folded never change.
HISTORY_CACHE_TURNS = 16


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _token_estimate(value: Any) -> int:
    """Tokens ``value`` costs as JSON, counted as every request estimate is."""

    return estimate_tokens(_canonical(value).decode("utf-8"))


def checkpoint_byte_limit(input_capacity: int) -> int:
    """The receipts' byte bound for a model with ``input_capacity`` tokens."""

    scaled = int(input_capacity * _CHECKPOINT_CAPACITY_SHARE) * _BYTES_PER_TOKEN
    return max(CHECKPOINT_BYTE_LIMIT, min(CHECKPOINT_BYTE_CEILING, scaled))


def _without_callback_key(entry: dict[str, Any]) -> dict[str, Any]:
    """``entry`` without the callback key its waiting receipt once carried.

    Rows recorded before the key left the receipt keep it at rest; neither
    the model, the operator's tool card nor any API reads it back.
    """

    result = entry.get("provider_result")
    scrubbed = without_results_api_key(result)
    return entry if scrubbed is result else {**entry, "provider_result": scrubbed}


def _event_type(entry: dict[str, Any]) -> str:
    return str(entry.get("status") or "recorded")[:80]


def _step(entry: Mapping[str, Any]) -> int:
    return int(entry.get("step", 0))


def _step_ranges(steps: Iterable[int]) -> list[list[int]]:
    """``steps`` as sorted, inclusive ``[first, last]`` runs."""

    ranges: list[list[int]] = []
    for step in sorted(set(steps)):
        if ranges and ranges[-1][1] == step - 1:
            ranges[-1][1] = step
        else:
            ranges.append([step, step])
    return ranges


def _decoded(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (
        json.JSONDecodeError
    ):  # diagnostic-expected: a legacy result has no failure facts to fold
        return None
    return decoded if isinstance(decoded, dict) else None


def _failure_facts(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a folded failure still tells the model, without its output.

    The arguments digest identifies the exact call that failed; the rest is
    Core's own classification from the failure envelope (never tool text),
    so the model can tell a correctable input from an effect it must not
    repeat.
    """

    if str(entry.get("status") or "complete") == "complete":
        return None
    facts: dict[str, Any] = {
        "arguments_sha256": hashlib.sha256(
            _canonical(entry.get("arguments") or {})
        ).hexdigest()[:16]
    }
    result = _decoded(entry.get("provider_result")) or {}
    if result.get("schema") == _FAILURE_SCHEMA:
        for key in ("category", "problem", "invalid_input", "side_effects"):
            value = result.get(key)
            if isinstance(value, str) and value:
                facts[key] = value[:120]
        if isinstance(result.get("retry_safe"), bool):
            facts["retry_safe"] = result["retry_safe"]
    if (
        entry.get("recovered_from_restart_unknown") is True
        or result.get("schema") == _RESTART_UNKNOWN_SCHEMA
    ):
        facts.update(
            {
                "category": "outcome_unknown",
                "side_effects": "unknown",
                "retry_safe": False,
            }
        )
    return facts


def _copied(value: Any) -> Any:
    """A private copy of decoded JSON, so a caller cannot alter the cache."""

    if isinstance(value, dict):
        return {key: _copied(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copied(item) for item in value]
    return value


@dataclass
class _FoldedHistory:
    """The latest row per step for one turn, through ``through_sequence``."""

    through_sequence: int = 0
    latest: dict[int, tuple[int, dict[str, Any]]] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnCheckpoint:
    through_step: int
    summary: dict[str, Any]
    digest: str
    token_estimate: int

    def covers(self, entry: Mapping[str, Any]) -> bool:
        """Whether ``entry`` is folded into this checkpoint rather than replayed."""

        step = _step(entry)
        ranges = self.summary.get("covered_steps")
        if isinstance(ranges, list):
            return any(
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], int)
                and isinstance(item[1], int)
                and item[0] <= step <= item[1]
                for item in ranges
            )
        # v1: every step through the boundary except the ones it replayed.
        return step <= self.through_step and entry.get("status") not in (
            PENDING_STATUSES | _V1_UNCOVERED_STATUSES
        )


class ChatTurnLedger:
    """Single storage boundary for provider-visible tool history.

    Each row carries the complete latest projection of one step. Updating a
    paused step appends a new row; folding by step therefore reconstructs the
    authoritative history without mutating earlier evidence.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self._folded: OrderedDict[str, _FoldedHistory] = OrderedDict()
        self._folded_lock = threading.Lock()

    def _rows(self, turn_id: str) -> list[ChatTurnStepEventRow]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ChatTurnStepEventRow)
                    .where(ChatTurnStepEventRow.turn_id == turn_id)
                    .order_by(ChatTurnStepEventRow.sequence)
                )
            )

    def has_events(self, turn_id: str) -> bool:
        with self.database.session() as session:
            return bool(
                session.scalar(
                    select(func.count())
                    .select_from(ChatTurnStepEventRow)
                    .where(ChatTurnStepEventRow.turn_id == turn_id)
                )
            )

    def next_provider_group(self, turn_id: str) -> int:
        """Ordinal for the turn's next routing response, counted from 1.

        Steps carry it in ``provider_group``, so the ledger groups the calls
        one provider response issued, across pauses and restarts.
        """

        with self.database.session() as session:
            return (
                int(
                    session.scalar(
                        select(
                            func.coalesce(
                                func.max(ChatTurnStepEventRow.provider_group), 0
                            )
                        ).where(ChatTurnStepEventRow.turn_id == turn_id)
                    )
                    or 0
                )
                + 1
            )

    def import_legacy(self, turn: ChatTurn) -> None:
        if not turn.tool_history or self.has_events(turn.id):
            return
        for entry in turn.tool_history:
            if isinstance(entry, dict):
                self.append(
                    turn.id,
                    entry,
                    idempotency_key=f"legacy:{int(entry.get('step', 0))}:0",
                    event_type="legacy_imported",
                )

    @staticmethod
    def _stored_payload(
        session: Session, turn_id: str, step: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """``payload`` without the replay state its step already holds.

        A step is recorded as an intent before the broker runs it and again
        with its result, and both carry the issuing response's reasoning.
        The first copy is kept; a later row that repeats it names that row,
        so the reasoning is stored once however often the step changes.
        """

        payload.pop(_REPLAY_FROM, None)
        shared = {
            field: payload[field] for field in SHARED_REPLAY_FIELDS if field in payload
        }
        if not shared:
            return payload
        holder = session.scalar(
            select(ChatTurnStepEventRow)
            .where(
                ChatTurnStepEventRow.turn_id == turn_id,
                ChatTurnStepEventRow.step == step,
            )
            .order_by(ChatTurnStepEventRow.sequence.desc())
            .limit(1)
        )
        reference = holder.payload.get(_REPLAY_FROM) if holder is not None else None
        if isinstance(reference, int) and not isinstance(reference, bool):
            holder = session.scalar(
                select(ChatTurnStepEventRow).where(
                    ChatTurnStepEventRow.turn_id == turn_id,
                    ChatTurnStepEventRow.sequence == reference,
                )
            )
        if holder is None:
            return payload
        held = {
            field: holder.payload[field]
            for field in SHARED_REPLAY_FIELDS
            if field in holder.payload
        }
        if held != shared:
            return payload
        stored = {
            key: value
            for key, value in payload.items()
            if key not in SHARED_REPLAY_FIELDS
        }
        stored[_REPLAY_FROM] = holder.sequence
        return stored

    @staticmethod
    def _resolved(
        payload: Mapping[str, Any], rows: Mapping[int, ChatTurnStepEventRow]
    ) -> dict[str, Any]:
        """A stored row as the step entry it records, shared replay state included."""

        entry = dict(payload)
        reference = entry.pop(_REPLAY_FROM, None)
        holder = rows.get(reference) if isinstance(reference, int) else None
        if holder is not None:
            for field in SHARED_REPLAY_FIELDS:
                if field in holder.payload and field not in entry:
                    entry[field] = holder.payload[field]
        return _without_callback_key(entry)

    def append(
        self,
        turn_id: str,
        entry: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        event_type: str | None = None,
    ) -> int:
        entry = _without_callback_key(entry)
        step = int(entry.get("step", 0))
        key = idempotency_key or (
            f"step:{step}:{event_type or _event_type(entry)}:"
            f"{hashlib.sha256(_canonical(entry)).hexdigest()[:24]}"
        )
        with self.database.session() as session:
            existing = session.scalar(
                select(ChatTurnStepEventRow).where(
                    ChatTurnStepEventRow.turn_id == turn_id,
                    ChatTurnStepEventRow.idempotency_key == key,
                )
            )
            if existing is not None:
                return existing.sequence
            sequence = (
                int(
                    session.scalar(
                        select(
                            func.coalesce(func.max(ChatTurnStepEventRow.sequence), 0)
                        ).where(ChatTurnStepEventRow.turn_id == turn_id)
                    )
                    or 0
                )
                + 1
            )
            row = ChatTurnStepEventRow(
                id=str(uuid4()),
                turn_id=turn_id,
                sequence=sequence,
                step=step,
                provider_group=(
                    int(entry["provider_group"])
                    if isinstance(entry.get("provider_group"), int)
                    else None
                ),
                event_type=(event_type or _event_type(entry)),
                tool_call_id=(
                    str(entry["tool_call_id"]) if entry.get("tool_call_id") else None
                ),
                payload=self._stored_payload(
                    session, turn_id, step, json.loads(_canonical(entry))
                ),
                occurred_at=utc_now(),
                idempotency_key=key,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                # Another callback may have appended the identical transition.
                session.rollback()
                with self.database.session() as retry:
                    winner = retry.scalar(
                        select(ChatTurnStepEventRow).where(
                            ChatTurnStepEventRow.turn_id == turn_id,
                            ChatTurnStepEventRow.idempotency_key == key,
                        )
                    )
                    if winner is None:
                        raise
                    return winner.sequence
            return sequence

    def _rows_after(self, turn_id: str, sequence: int) -> list[tuple[int, int, Any]]:
        with self.database.session() as session:
            return [
                (row.sequence, row.step, row.payload)
                for row in session.execute(
                    select(
                        ChatTurnStepEventRow.sequence,
                        ChatTurnStepEventRow.step,
                        ChatTurnStepEventRow.payload,
                    )
                    .where(
                        ChatTurnStepEventRow.turn_id == turn_id,
                        ChatTurnStepEventRow.sequence > sequence,
                    )
                    .order_by(ChatTurnStepEventRow.sequence)
                )
            ]

    def history(self, turn: ChatTurn) -> list[dict[str, Any]]:
        """The latest projection of every step, in the order it was recorded.

        Rows are append-only and their sequences commit in order, so a fold
        already read stays valid: each call reads only the rows after it.
        """

        with self._folded_lock:
            cached = self._folded.get(turn.id)
            if cached is not None:
                self._folded.move_to_end(turn.id)
            folded = (
                _FoldedHistory(cached.through_sequence, dict(cached.latest))
                if cached is not None
                else _FoldedHistory()
            )
        rows = self._rows_after(turn.id, folded.through_sequence)
        if not rows and not folded.latest:
            return [
                _without_callback_key(dict(item))
                for item in turn.tool_history
                if isinstance(item, dict)
            ]
        read = {sequence: payload for sequence, _, payload in rows}
        for sequence, step, payload in rows:
            entry = dict(payload)
            reference = entry.pop(_REPLAY_FROM, None)
            if isinstance(reference, int) and not isinstance(reference, bool):
                # The shared replay state is held by an earlier row of the
                # same step: in this read, or already folded into the step.
                held: Mapping[str, Any] | None = (
                    read[reference]
                    if reference in read
                    else folded.latest[step][1]
                    if step in folded.latest
                    else self._payload_at(turn.id, reference)
                )
                for name in SHARED_REPLAY_FIELDS:
                    if held is not None and name in held and name not in entry:
                        entry[name] = held[name]
            folded.latest[step] = (sequence, _without_callback_key(entry))
            folded.through_sequence = max(folded.through_sequence, sequence)
        if rows:
            with self._folded_lock:
                current = self._folded.get(turn.id)
                if (
                    current is None
                    or current.through_sequence <= folded.through_sequence
                ):
                    self._folded[turn.id] = folded
                    self._folded.move_to_end(turn.id)
                while len(self._folded) > HISTORY_CACHE_TURNS:
                    self._folded.popitem(last=False)
        return [
            _copied(entry)
            for _, entry in sorted(folded.latest.values(), key=lambda item: item[0])
        ]

    def _payload_at(self, turn_id: str, sequence: int) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.scalar(
                select(ChatTurnStepEventRow).where(
                    ChatTurnStepEventRow.turn_id == turn_id,
                    ChatTurnStepEventRow.sequence == sequence,
                )
            )
            return dict(row.payload) if row is not None else None

    def event(self, turn_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """The step entry one row recorded, shared replay state included."""

        with self.database.session() as session:
            row = session.scalar(
                select(ChatTurnStepEventRow).where(
                    ChatTurnStepEventRow.turn_id == turn_id,
                    ChatTurnStepEventRow.idempotency_key == idempotency_key,
                )
            )
            if row is None:
                return None
            reference = row.payload.get(_REPLAY_FROM)
            holder = (
                session.scalar(
                    select(ChatTurnStepEventRow).where(
                        ChatTurnStepEventRow.turn_id == turn_id,
                        ChatTurnStepEventRow.sequence == reference,
                    )
                )
                if isinstance(reference, int) and not isinstance(reference, bool)
                else None
            )
            rows = {holder.sequence: holder} if holder is not None else {}
            return self._resolved(row.payload, rows)

    def tail(self, turn_id: str, count: int) -> tuple[int, list[dict[str, Any]]] | None:
        """The step count and last ``count`` steps of ``history``, read
        without the rest; None when the turn has no ledger rows."""

        with self.database.session() as session:
            steps = list(
                session.scalars(
                    select(ChatTurnStepEventRow.step)
                    .where(ChatTurnStepEventRow.turn_id == turn_id)
                    .distinct()
                    .order_by(ChatTurnStepEventRow.step.desc())
                    .limit(count)
                )
            )
            if not steps:
                return None
            rows = list(
                session.scalars(
                    select(ChatTurnStepEventRow)
                    .where(
                        ChatTurnStepEventRow.turn_id == turn_id,
                        ChatTurnStepEventRow.step >= min(steps),
                    )
                    .order_by(ChatTurnStepEventRow.sequence)
                )
            )
        # A row naming its step's first row for shared replay state finds it
        # here: that row belongs to the same step.
        by_sequence = {row.sequence: row for row in rows}
        latest: dict[int, ChatTurnStepEventRow] = {}
        for row in rows:
            latest[row.step] = row
        return max(steps) + 1, [
            self._resolved(row.payload, by_sequence)
            for row in sorted(latest.values(), key=lambda row: row.sequence)
        ]

    def tool_call_ids(self, turn: ChatTurn) -> list[str]:
        ids = [
            str(entry["tool_call_id"])
            for entry in self.history(turn)
            if entry.get("tool_call_id")
        ]
        if turn.tool_call_ids and not self.has_events(turn.id):
            # A turn from before the ledger kept its calls in this list.
            return list(dict.fromkeys([*turn.tool_call_ids, *ids]))
        return ids

    def latest_checkpoint(self, turn_id: str) -> TurnCheckpoint | None:
        with self.database.session() as session:
            row = session.scalar(
                select(ChatTurnCheckpointRow)
                .where(ChatTurnCheckpointRow.turn_id == turn_id)
                .order_by(ChatTurnCheckpointRow.through_step.desc())
                .limit(1)
            )
            if row is None:
                return None
            return TurnCheckpoint(
                through_step=row.through_step,
                summary=dict(row.summary),
                digest=row.digest,
                token_estimate=row.token_estimate,
            )

    @staticmethod
    def _group(entry: dict[str, Any]) -> str:
        return str(
            entry.get("response_group")
            or entry.get("provider_group")
            or f"step:{entry.get('step', 0)}"
        )

    def _foldable(
        self,
        history: list[dict[str, Any]],
        recent_groups: int = RECENT_RESPONSE_GROUPS,
    ) -> list[dict[str, Any]]:
        """The steps of ``history`` a checkpoint may fold.

        Every step outside the latest ``recent_groups`` response groups,
        except one still waiting on the operator or a callback.
        """

        groups: list[str] = []
        for entry in history:
            group = self._group(entry)
            if group not in groups:
                groups.append(group)
        keep_groups = set(groups[-recent_groups:]) if recent_groups > 0 else set()
        return [
            entry
            for entry in history
            if self._group(entry) not in keep_groups
            and entry.get("status") not in PENDING_STATUSES
        ]

    def foldable(
        self, turn: ChatTurn, recent_groups: int = RECENT_RESPONSE_GROUPS
    ) -> list[dict[str, Any]]:
        """The turn's steps its next checkpoint may fold, in recorded order."""

        return self._foldable(self.history(turn), recent_groups)

    def compacted_history(
        self,
        turn: ChatTurn,
        *,
        advance: bool = False,
        byte_limit: int = CHECKPOINT_BYTE_LIMIT,
        working_notes: Callable[[], dict[str, Any] | None] | None = None,
        recent_groups: int = RECENT_RESPONSE_GROUPS,
        progress: Callable[[set[int]], dict[str, Any] | None] | None = None,
    ) -> tuple[TurnCheckpoint | None, list[dict[str, Any]]]:
        """The turn's checkpoint and the entries replayed whole after it.

        Every step outside the latest ``RECENT_RESPONSE_GROUPS`` response
        groups, failed and denied ones included, can be folded into compact
        receipts; only a step still waiting stays whole. The checkpoint
        advances in blocks, when ``CHECKPOINT_STEP_INTERVAL`` steps or
        ``CHECKPOINT_TOKEN_TRIGGER`` tokens of them are left unfolded, or
        when ``advance`` asks because the request outgrew its target. Between
        advances the checkpoint and every replayed entry stay byte-identical,
        so each request extends the one before it and a provider's prefix
        cache keeps serving all but the newest step.

        A checkpoint that advances bounds its receipts by ``byte_limit`` and
        carries the session's working notes as ``working_notes`` returns them
        then: a folded ``notes.write`` call no longer replays its content.
        It also carries the turn's progress memory as ``progress`` returns it
        for the steps it folds; the memory changes only when the checkpoint
        does.

        An advance with fewer ``recent_groups`` folds deeper, for a request
        that no longer fits even with every result cleared. Coverage only
        grows: later calls with the default window keep replaying just what
        the deeper checkpoint left out.
        """

        history = self.history(turn)
        if not history:
            return None, []
        fold = self._foldable(history, recent_groups)
        checkpoint = self.latest_checkpoint(turn.id)
        uncheckpointed = [
            entry
            for entry in fold
            if checkpoint is None or not checkpoint.covers(entry)
        ]
        should_checkpoint = bool(uncheckpointed) and (
            advance
            or len(uncheckpointed) >= CHECKPOINT_STEP_INTERVAL
            or _token_estimate(uncheckpointed) >= CHECKPOINT_TOKEN_TRIGGER
        )
        if should_checkpoint:
            checkpoint = self._write_checkpoint(
                turn.id,
                fold,
                byte_limit=byte_limit,
                notes=working_notes() if working_notes is not None else None,
                progress=(
                    progress({_step(entry) for entry in fold})
                    if progress is not None
                    else None
                ),
            )
        if checkpoint is None:
            return None, history
        return checkpoint, [entry for entry in history if not checkpoint.covers(entry)]

    def _write_checkpoint(
        self,
        turn_id: str,
        entries: Iterable[dict[str, Any]],
        *,
        byte_limit: int = CHECKPOINT_BYTE_LIMIT,
        notes: dict[str, Any] | None = None,
        progress: dict[str, Any] | None = None,
    ) -> TurnCheckpoint:
        entries = list(entries)
        through_step = max(_step(item) for item in entries)
        tools: list[str] = []
        steps: list[list[Any]] = []
        for item in entries:
            tool = str(item.get("name") or "unknown")[:80]
            if tool not in tools:
                tools.append(tool)
            receipt: list[Any] = [
                _step(item),
                tools.index(tool),
                str(item.get("status") or "complete")[:40],
            ]
            # What the call acted on, beside what came of it: a receipt that
            # says only "completed" cannot tell the model which file it read.
            brief = step_brief(item.get("arguments"))
            outcome = result_summary(item.get("result_summary"), _RECEIPT_SUMMARY_CHARS)
            # A folded lookup keeps what it found, so its receipt still answers.
            found = lookup_identifiers(
                item.get("name"), item.get("arguments"), item.get("provider_result")
            )
            if found:
                outcome = clipped(
                    "; ".join(
                        part for part in (outcome, "found " + ", ".join(found)) if part
                    ),
                    _RECEIPT_SUMMARY_CHARS,
                )
            references = [
                str(ref.get("artifact_id"))[:120]
                for ref in item.get("artifacts") or []
                if isinstance(ref, dict) and ref.get("artifact_id")
            ][:8]
            if item.get("result_artifact_id"):
                references.insert(0, str(item["result_artifact_id"])[:120])
            failure = _failure_facts(item)
            if failure and failure.get("problem") == item.get("result_summary"):
                del failure["problem"]
            # Trailing fields are left off; an empty placeholder keeps the
            # position of a later one.
            trailing: list[Any] = [brief, outcome, references, failure]
            while trailing and not trailing[-1]:
                trailing.pop()
            receipt.extend(
                value if value else ([] if index == 2 else "")
                for index, value in enumerate(trailing)
            )
            steps.append(receipt)
        digest = hashlib.sha256(_canonical(entries)).hexdigest()
        summary: dict[str, Any] = {
            "schema": CHECKPOINT_SCHEMA,
            "through_step": through_step,
            "covered_steps": _step_ranges(_step(item) for item in entries),
            "step_count": len(steps),
            "tools": tools,
            "step_fields": list(_CHECKPOINT_STEP_FIELDS),
            "steps": steps,
            "integrity_sha256": digest,
            "note": (
                "Each receipt says what its step acted on and how it ended. "
                "Full outputs remain readable with tool_output.read through "
                "their artifact references. A failure's arguments_sha256 "
                "identifies the exact arguments that failed."
            ),
        }
        # Over the bound, the oldest successful receipts go first: a failure
        # is what the model must not repeat blindly.
        excess = len(_canonical(summary)) - byte_limit
        if excess > 0:
            failed = len(_CHECKPOINT_STEP_FIELDS)
            order = [i for i, item in enumerate(steps) if len(item) < failed] + [
                i for i, item in enumerate(steps) if len(item) == failed
            ]
            # The count that replaces the dropped receipts costs a few bytes.
            excess += len(f',"omitted_steps":{len(steps)}')
            dropped: set[int] = set()
            for index in order:
                if excess <= 0:
                    break
                dropped.add(index)
                excess -= len(_canonical(steps[index])) + 1
            summary["steps"] = [
                item for index, item in enumerate(steps) if index not in dropped
            ]
            summary["omitted_steps"] = len(dropped)
        if notes and notes.get("content"):
            # The notes are bounded by their own tool, outside the receipts'
            # bound: dropping receipts to make room for them would trade one
            # memory for the other.
            summary["working_notes"] = dict(notes)
        if progress:
            # The progress memory is bounded by the compactor's allowance, and
            # like the notes it stays outside the receipts' bound.
            summary["progress"] = dict(progress)
        token_estimate = _token_estimate(summary)
        checkpoint = TurnCheckpoint(through_step, summary, digest, token_estimate)
        with self.database.session() as session:
            exists = session.scalar(
                select(ChatTurnCheckpointRow).where(
                    ChatTurnCheckpointRow.turn_id == turn_id,
                    ChatTurnCheckpointRow.through_step == through_step,
                )
            )
            if exists is not None:
                # The boundary is already recorded; replay keeps using that
                # record so every request after it stays identical.
                return TurnCheckpoint(
                    through_step=exists.through_step,
                    summary=dict(exists.summary),
                    digest=exists.digest,
                    token_estimate=exists.token_estimate,
                )
            session.add(
                ChatTurnCheckpointRow(
                    id=str(uuid4()),
                    turn_id=turn_id,
                    through_step=through_step,
                    summary=summary,
                    digest=digest,
                    token_estimate=token_estimate,
                    created_at=utc_now(),
                )
            )
        return checkpoint


def turn_history(database: Database, turn: ChatTurn) -> list[dict[str, Any]]:
    """Compatibility helper for readers outside ChatService."""

    return ChatTurnLedger(database).history(turn)
