"""Normalized, append-only provider turn history and deterministic checkpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .database import ChatTurnCheckpointRow, ChatTurnStepEventRow, Database
from .domain import ChatTurn, utc_now

CHECKPOINT_STEP_INTERVAL = 16
CHECKPOINT_TOKEN_TRIGGER = 24_000
CHECKPOINT_TOKEN_LIMIT = 4_000
CHECKPOINT_BYTE_LIMIT = 16 * 1024
REPLAY_TOKEN_LIMIT = 48_000
RECENT_RESPONSE_GROUPS = 8


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _token_estimate(value: Any) -> int:
    return max(1, (len(_canonical(value)) + 3) // 4)


def _event_type(entry: dict[str, Any]) -> str:
    return str(entry.get("status") or "recorded")[:80]


@dataclass(frozen=True)
class TurnCheckpoint:
    through_step: int
    summary: dict[str, Any]
    digest: str
    token_estimate: int


class ChatTurnLedger:
    """Single storage boundary for provider-visible tool history.

    Each row carries the complete latest projection of one step. Updating a
    paused step appends a new row; folding by step therefore reconstructs the
    authoritative history without mutating earlier evidence.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

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

    def append(
        self,
        turn_id: str,
        entry: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        event_type: str | None = None,
    ) -> int:
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
                payload=json.loads(_canonical(entry)),
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

    def history(self, turn: ChatTurn) -> list[dict[str, Any]]:
        rows = self._rows(turn.id)
        if not rows:
            return [dict(item) for item in turn.tool_history if isinstance(item, dict)]
        latest: dict[int, tuple[int, dict[str, Any]]] = {}
        for row in rows:
            latest[row.step] = (row.sequence, dict(row.payload))
        return [payload for _, payload in sorted(latest.values())]

    def tool_call_ids(self, turn: ChatTurn) -> list[str]:
        return [
            str(entry["tool_call_id"])
            for entry in self.history(turn)
            if entry.get("tool_call_id")
        ]

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

    def compacted_history(
        self, turn: ChatTurn
    ) -> tuple[TurnCheckpoint | None, list[dict[str, Any]]]:
        history = self.history(turn)
        if not history:
            return None, []
        groups: list[str] = []
        for entry in history:
            group = self._group(entry)
            if group not in groups:
                groups.append(group)
        keep_groups = set(groups[-RECENT_RESPONSE_GROUPS:])
        protected_steps = {
            int(entry.get("step", 0))
            for entry in history
            if entry.get("status")
            in {"waiting_approval", "waiting_callback", "failed", "denied"}
        }
        fold = [
            entry
            for entry in history
            if self._group(entry) not in keep_groups
            and int(entry.get("step", 0)) not in protected_steps
        ]
        checkpoint = self.latest_checkpoint(turn.id)
        uncheckpointed = [
            entry
            for entry in fold
            if checkpoint is None or int(entry.get("step", 0)) > checkpoint.through_step
        ]
        should_checkpoint = bool(uncheckpointed) and (
            checkpoint is not None
            or len(uncheckpointed) >= CHECKPOINT_STEP_INTERVAL
            or _token_estimate(uncheckpointed) >= CHECKPOINT_TOKEN_TRIGGER
        )
        if should_checkpoint:
            checkpoint = self._write_checkpoint(turn.id, fold)
        if checkpoint is None:
            return None, history
        replay = [
            entry
            for entry in history
            if int(entry.get("step", 0)) > checkpoint.through_step
            or int(entry.get("step", 0)) in protected_steps
        ]
        return checkpoint, replay

    def _write_checkpoint(
        self, turn_id: str, entries: Iterable[dict[str, Any]]
    ) -> TurnCheckpoint:
        entries = list(entries)
        through_step = max(int(item.get("step", 0)) for item in entries)
        tools: list[str] = []
        steps: list[list[Any]] = []
        for item in entries:
            tool = str(item.get("name") or "unknown")[:80]
            if tool not in tools:
                tools.append(tool)
            receipt: list[Any] = [
                int(item.get("step", 0)),
                tools.index(tool),
                str(item.get("status") or "complete")[:40],
            ]
            if item.get("result_summary"):
                receipt.append(str(item["result_summary"])[:80])
            references = [
                str(ref.get("artifact_id"))[:120]
                for ref in item.get("artifacts") or []
                if isinstance(ref, dict) and ref.get("artifact_id")
            ][:8]
            if item.get("result_artifact_id"):
                references.insert(0, str(item["result_artifact_id"])[:120])
            if references:
                if len(receipt) == 3:
                    receipt.append("")
                receipt.append(references)
            steps.append(receipt)
        digest = hashlib.sha256(_canonical(entries)).hexdigest()
        summary: dict[str, Any] = {
            "schema": "nebula.chat-turn-checkpoint/v1",
            "through_step": through_step,
            "step_count": len(steps),
            "tools": tools,
            "step_fields": ["number", "tool_index", "state", "summary", "artifacts"],
            "steps": steps,
            "integrity_sha256": digest,
            "note": "Full outputs remain available through their tool-call and artifact references.",
        }
        while len(_canonical(summary)) > CHECKPOINT_BYTE_LIMIT and summary["steps"]:
            summary["steps"] = summary["steps"][1:]
            summary["omitted_steps"] = len(steps) - len(summary["steps"])
        token_estimate = min(CHECKPOINT_TOKEN_LIMIT, _token_estimate(summary))
        checkpoint = TurnCheckpoint(through_step, summary, digest, token_estimate)
        with self.database.session() as session:
            exists = session.scalar(
                select(ChatTurnCheckpointRow).where(
                    ChatTurnCheckpointRow.turn_id == turn_id,
                    ChatTurnCheckpointRow.through_step == through_step,
                )
            )
            if exists is None:
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
