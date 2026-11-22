"""Which chat changed which workspace file: Overview mode's attribution reads.

Two durable records name the chat behind a file change:

* Workspace provenance observations. A turn or tool scope with a native
  lifecycle hook fingerprints Git's dirty paths before and after it runs, so
  the observation names the paths that changed and the chat that owned the
  scope. Path level only; projects without hooks write none.
* Harness file-change activity. Codex reports each file change with its patch,
  Claude reports Write and Edit tool calls with their arguments, and any
  completed file change that carried a unified diff keeps it (also as a
  ``harness-changes.diff`` artifact). These give line-level hunks.

Both reads are bounded by a time window and row limits over indexed columns.
They read durable rows only, so the supervisor's API worker serves them
without waking a project worker. Paths are relative to the project workspace,
in the POSIX form the workspace file browser uses.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import func, select

from .stored_entities import decode_stored
from .artifacts import ArtifactStore, ArtifactStoreError
from .butler_board import FAILED_WINDOW, MAX_FAILED_ITEMS
from .chat_turn_headers import (
    ChatTurnHeader,
    latest_turn_header,
    pending_engagement_turn_headers,
)
from .conditional_reads import conditional, content_etag
from .database import EntityRow, OperationEventRow
from .diagnostics import record_caught_exception
from .domain import (
    Artifact,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessTurn,
    WorkspaceProvenanceObservation,
    utc_now,
)
from .linked_project_chats import (
    UNFINISHED_TURN_STATUSES,
    session_state,
    visible_pending,
)
from .storage import NebulaStore, NotFoundError

# The summary lists at most this many paths, newest change first.
MAX_ATTRIBUTED_PATHS = 500
MAX_HISTORY_EVENTS = 50
DEFAULT_WINDOW_HOURS = 168
MAX_WINDOW_HOURS = 720
# Rows read from each source per request, newest first. A busy project that
# exceeds them within the window reports ``truncated`` rather than scanning on.
MAX_OBSERVATION_ROWS = 1_000
MAX_HARNESS_EVENT_ROWS = 5_000
# A diff artifact is read only to replace an inline patch the activity record
# cut short, and only this much of it.
MAX_DIFF_ARTIFACT_BYTES = 8 * 1024 * 1024
_TRUNCATION_MARKER = "…[truncated]"
_HARNESS_EVENT_TYPES = (
    "harness.tool_started",
    "harness.tool_completed",
    "harness.item_upsert",
    "harness.item_completed",
)
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_CONTAINER_WORKSPACE = "/workspace"

ChangeKind = Literal["created", "modified", "deleted"]


@dataclass(frozen=True)
class DiffHunk:
    """A run of new-file lines a patch added or changed."""

    start: int
    lines: int
    kind: Literal["added", "changed"]

    def as_json(self) -> dict[str, Any]:
        return {"start": self.start, "lines": self.lines, "kind": self.kind}


@dataclass
class FileDiff:
    """One file's section of a unified diff."""

    old_path: str | None = None
    new_path: str | None = None
    added: int = 0
    removed: int = 0
    hunks: list[DiffHunk] = field(default_factory=list)

    @property
    def path(self) -> str | None:
        return self.new_path or self.old_path

    @property
    def kind(self) -> ChangeKind:
        if self.old_path is None and self.new_path is not None:
            return "created"
        if self.new_path is None and self.old_path is not None:
            return "deleted"
        return "modified"


def _diff_path(header: str) -> str | None:
    value = header.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = value[1:-1]
    if value[:2] in {"a/", "b/"}:
        value = value[2:]
    return value or None


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Split a unified diff into files with counts and new-file hunks.

    Hunks are runs of consecutive ``+``/``-`` lines. A run that adds lines is
    reported at its first added line in the new file: ``added`` when it removed
    nothing, ``changed`` when it replaced lines. A run that only removes lines
    is a ``changed`` hunk of zero lines starting at the new-file line that
    follows the removed lines. A diff with hunks but no file headers yields one file with no
    path (Codex reports each change's patch that way).
    """

    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_left = new_left = 0
    new_line = 0
    run_start: int | None = None
    run_added = 0
    run_removed = 0
    pending_old: str | None = None
    pending_old_seen = False

    def close_run() -> None:
        nonlocal run_start, run_added, run_removed
        if current is not None and run_start is not None:
            current.hunks.append(
                DiffHunk(
                    start=run_start,
                    lines=run_added,
                    kind="changed" if run_removed else "added",
                )
            )
        run_start = None
        run_added = run_removed = 0

    for line in text.splitlines():
        if old_left > 0 or new_left > 0:
            marker = line[:1]
            if marker == "+":
                if run_start is None:
                    run_start = new_line
                run_added += 1
                new_line += 1
                new_left -= 1
                if current is not None:
                    current.added += 1
                continue
            if marker == "-":
                if run_start is None:
                    run_start = new_line
                run_removed += 1
                old_left -= 1
                if current is not None:
                    current.removed += 1
                continue
            if marker == "\\":
                continue
            if marker == " " or line == "":
                close_run()
                new_line += 1
                old_left -= 1
                new_left -= 1
                continue
            # A malformed count: leave the hunk and read this line as a header.
            close_run()
            old_left = new_left = 0
        if line.startswith("\\"):
            continue
        close_run()
        if line.startswith("diff --git "):
            current = FileDiff()
            files.append(current)
            parts = line[len("diff --git ") :].split(" b/", 1)
            if len(parts) == 2:
                current.old_path = _diff_path(parts[0])
                current.new_path = _diff_path("b/" + parts[1])
            pending_old_seen = False
            continue
        if line.startswith("--- "):
            pending_old = _diff_path(line[4:])
            pending_old_seen = True
            continue
        if line.startswith("+++ ") and pending_old_seen:
            new_path = _diff_path(line[4:])
            if current is None or current.hunks or current.added or current.removed:
                current = FileDiff()
                files.append(current)
            current.old_path = pending_old
            current.new_path = new_path
            pending_old_seen = False
            continue
        if line.startswith("new file mode") and current is not None:
            current.old_path = None
            continue
        if line.startswith("deleted file mode") and current is not None:
            current.new_path = None
            continue
        match = _HUNK_HEADER.match(line)
        if match:
            if current is None:
                current = FileDiff(old_path="", new_path="")
                files.append(current)
            old_left = int(match.group(2) if match.group(2) is not None else 1)
            new_left = int(match.group(4) if match.group(4) is not None else 1)
            new_line = int(match.group(3))
            if new_left == 0:
                # A hunk that only removes lines names the line before them.
                new_line += 1
            continue
    close_run()
    for item in files:
        if item.old_path == "" and item.new_path == "":
            item.old_path = item.new_path = None
    return files


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _edit_counts(old: str, new: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


@dataclass
class _Change:
    path: str
    session_id: str
    turn_id: str | None
    at: datetime
    kind: ChangeKind
    attribution: Literal["owned", "uncertain"]
    source: Literal["provenance", "harness"]
    added: int | None = None
    removed: int | None = None
    hunks: list[DiffHunk] | None = None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:  # diagnostic-expected: an unreadable time leaves the row's own timestamp in use
            return None
    return None


def _clean_relative(path: str) -> str | None:
    """A safe POSIX workspace path, or ``None`` for one that escapes."""

    if not path or "\x00" in path or "\\" in path:
        return None
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        return None
    parts = [part for part in candidate.parts if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    return PurePosixPath(*parts).as_posix()


class _WorkspacePaths:
    """Map recorded paths onto the project's workspace-relative paths."""

    def __init__(self, project: Engagement) -> None:
        roots: list[PurePosixPath] = [PurePosixPath(_CONTAINER_WORKSPACE)]
        if project.workspace_path:
            configured = project.workspace_path.rstrip("/") or "/"
            roots.append(PurePosixPath(os.path.normpath(configured)))
            resolved = os.path.realpath(configured)
            if resolved != configured:
                roots.append(PurePosixPath(resolved))
        self.roots = roots
        # A managed workspace is named after the project ID's SHA-256.
        self.managed = (
            None
            if project.workspace_path
            else hashlib.sha256(project.id.encode("utf-8")).hexdigest()
        )

    def relative(self, path: str) -> str | None:
        if not path or "\x00" in path:
            return None
        candidate = PurePosixPath(os.path.normpath(path))
        if not candidate.is_absolute():
            return _clean_relative(path)
        for root in self.roots:
            if root in candidate.parents:
                return _clean_relative(candidate.relative_to(root).as_posix())
        if self.managed and self.managed in candidate.parts[:-1]:
            index = candidate.parts.index(self.managed)
            return _clean_relative(
                PurePosixPath(*candidate.parts[index + 1 :]).as_posix()
            )
        return None

    def under(self, root: str, path: str) -> str | None:
        """A path an observation recorded relative to its Git root."""

        relative = _clean_relative(path)
        if relative is None:
            return None
        return self.relative(str(PurePosixPath(root) / relative))


def _observation_changes(
    payload: dict[str, Any], paths: _WorkspacePaths
) -> list[_Change]:
    try:
        observation = decode_stored(WorkspaceProvenanceObservation, payload)
    except ValueError as exc:
        record_caught_exception(
            "file_attribution",
            "file_attribution.observation_invalid",
            "A workspace provenance observation could not be read for file attribution.",
            exc,
            stage="observation",
        )
        return []
    if (
        observation.status != "complete"
        or not observation.supported
        or not observation.chat_session_id
    ):
        return []
    at = _aware(observation.completed_at or observation.started_at)
    changes: list[_Change] = []
    for mutation in observation.mutations:
        after = mutation.get("after")
        raw_path = mutation.get("path")
        if not isinstance(raw_path, str) or not isinstance(after, dict):
            # A path that became clean was committed or reverted, not edited.
            continue
        path = paths.under(observation.workspace_root, raw_path)
        if path is None:
            continue
        before = mutation.get("before")
        status = str(after.get("status") or "")
        kind: ChangeKind
        if after.get("kind") == "absent" or "D" in status:
            kind = "deleted"
        elif (before is None or before.get("kind") == "absent") and (
            "?" in status or "A" in status
        ):
            kind = "created"
        else:
            kind = "modified"
        confidence = mutation.get("confidence") or observation.confidence
        changes.append(
            _Change(
                path=path,
                session_id=observation.chat_session_id,
                turn_id=observation.chat_turn_id,
                at=at,
                kind=kind,
                attribution="owned" if confidence == "exact" else "uncertain",
                source="provenance",
            )
        )
    return changes


@dataclass
class _HarnessFileEvent:
    harness_turn_id: str
    at: datetime
    path: str
    kind: ChangeKind
    attribution: Literal["owned", "uncertain"]
    added: int | None = None
    removed: int | None = None
    hunks: list[DiffHunk] | None = None


def _truncated(text: str) -> bool:
    return text.endswith(_TRUNCATION_MARKER)


def _codex_change(change: dict[str, Any]) -> tuple[str, ChangeKind, Any] | None:
    raw_kind = change.get("kind")
    kind_type = raw_kind.get("type") if isinstance(raw_kind, dict) else raw_kind
    moved = raw_kind.get("move_path") if isinstance(raw_kind, dict) else None
    path = moved or change.get("path")
    if not isinstance(path, str):
        return None
    kind: ChangeKind = (
        "created"
        if kind_type == "add"
        else "deleted"
        if kind_type == "delete"
        else "modified"
    )
    return path, kind, change.get("diff")


def _whole_file_stats(
    kind: ChangeKind, text: str
) -> tuple[int | None, int | None, list[DiffHunk] | None]:
    lines = _line_count(text)
    if kind == "created":
        return lines, 0, [DiffHunk(1, lines, "added")] if lines else []
    if kind == "deleted":
        return 0, lines, []
    return None, None, None


def _patch_stats(
    kind: ChangeKind, diff: Any
) -> tuple[int | None, int | None, list[DiffHunk] | None]:
    if not isinstance(diff, str) or not diff or _truncated(diff):
        return None, None, None
    parsed = parse_unified_diff(diff) if "@@" in diff else []
    if not parsed:
        return _whole_file_stats(kind, diff)
    added = sum(item.added for item in parsed)
    removed = sum(item.removed for item in parsed)
    hunks = [hunk for item in parsed for hunk in item.hunks]
    return added, removed, hunks


def _harness_events(
    rows: list[Any],
    paths: _WorkspacePaths,
    diff_text: Any,
) -> list[_HarnessFileEvent]:
    """File changes reported by completed harness file-change items."""

    completions: dict[tuple[str, str], tuple[str, datetime]] = {}
    for operation_id, event_type, payload, occurred_at in rows:
        if event_type == "harness.tool_completed" and isinstance(payload, dict):
            item_id = payload.get("item_id")
            if isinstance(item_id, str):
                completions[(operation_id, item_id)] = (
                    str(payload.get("item_status") or ""),
                    _aware(occurred_at),
                )
    events: list[_HarnessFileEvent] = []
    for operation_id, event_type, payload, occurred_at in rows:
        if not isinstance(payload, dict):
            continue
        inner = payload.get("payload")
        if not isinstance(inner, dict):
            continue
        status = str(payload.get("item_status") or "")
        at = _parse_time(payload.get("occurred_at")) or _aware(occurred_at)
        attribution: Literal["owned", "uncertain"] = (
            "uncertain"
            if inner.get("attribution") == "uncertain_parallel_turns"
            else "owned"
        )

        def add(
            raw_path: str,
            kind: ChangeKind,
            stats: tuple[int | None, int | None, list[DiffHunk] | None],
            when: datetime,
        ) -> None:
            path = paths.relative(raw_path)
            if path is None:
                return
            added, removed, hunks = stats
            events.append(
                _HarnessFileEvent(
                    harness_turn_id=operation_id,
                    at=when,
                    path=path,
                    kind=kind,
                    attribution=attribution,
                    added=added,
                    removed=removed,
                    hunks=hunks,
                )
            )

        arguments = inner.get("arguments")
        if event_type == "harness.tool_started" and isinstance(arguments, dict):
            # Claude reports the file in the tool call's arguments; the change
            # counts only once its result reports success.
            item_id = payload.get("item_id")
            completion = (
                completions.get((operation_id, item_id))
                if isinstance(item_id, str)
                else None
            )
            if completion is None or completion[0] != "completed":
                continue
            target = arguments.get("file_path") or arguments.get("notebook_path")
            if not isinstance(target, str):
                continue
            edits = arguments.get("edits")
            stats: tuple[int | None, int | None, list[DiffHunk] | None] = (
                None,
                None,
                None,
            )
            pairs: list[tuple[Any, Any]] = []
            if isinstance(edits, list):
                pairs = [
                    (edit.get("old_string"), edit.get("new_string"))
                    for edit in edits
                    if isinstance(edit, dict)
                ]
            elif "old_string" in arguments:
                pairs = [(arguments.get("old_string"), arguments.get("new_string"))]
            if pairs and all(
                isinstance(old, str)
                and isinstance(new, str)
                and not _truncated(old)
                and not _truncated(new)
                for old, new in pairs
            ):
                counts = [_edit_counts(old, new) for old, new in pairs]
                stats = (
                    sum(item[0] for item in counts),
                    sum(item[1] for item in counts),
                    None,
                )
            add(target, "modified", stats, completion[1])
            continue
        if status != "completed":
            continue
        changes = inner.get("changes")
        if isinstance(changes, list):
            # Codex reports a completed fileChange item with each file's patch.
            for change in changes:
                if not isinstance(change, dict):
                    continue
                parsed = _codex_change(change)
                if parsed is None:
                    continue
                raw_path, kind, diff = parsed
                add(raw_path, kind, _patch_stats(kind, diff), at)
            continue
        diff = inner.get("diff")
        if isinstance(diff, str) and diff:
            if _truncated(diff):
                diff = diff_text(inner.get("diff_artifact_id")) or diff
            files = parse_unified_diff(diff)
            complete = not _truncated(diff)
            for item in files:
                if item.path is None:
                    continue
                add(
                    item.path,
                    item.kind,
                    (item.added, item.removed, list(item.hunks))
                    if complete
                    else (None, None, None),
                    at,
                )
    return events


def _clamp_window(hours: int) -> timedelta:
    return timedelta(hours=max(1, min(hours, MAX_WINDOW_HOURS)))


@dataclass
class _Scan:
    changes: list[_Change]
    truncated: bool


async def _scan(
    store: NebulaStore,
    artifacts: ArtifactStore | None,
    project: Engagement,
    window: timedelta,
) -> _Scan:
    """Every attributed change in the window, from both sources."""

    cutoff = utc_now() - window
    paths = _WorkspacePaths(project)
    async with store.database.session() as database:
        observation_rows = list(
            (
                await database.scalars(
                    select(EntityRow.payload)
                    .where(
                        EntityRow.kind == WorkspaceProvenanceObservation.entity_kind,
                        EntityRow.engagement_id == project.id,
                        EntityRow.updated_at >= cutoff,
                        EntityRow.chat_session_id.is_not(None),
                    )
                    .order_by(EntityRow.updated_at.desc(), EntityRow.id.desc())
                    .limit(MAX_OBSERVATION_ROWS + 1)
                )
            ).all()
        )
        event_rows = list(
            await database.execute(
                select(
                    OperationEventRow.operation_id,
                    OperationEventRow.event_type,
                    OperationEventRow.payload,
                    OperationEventRow.occurred_at,
                )
                .where(
                    OperationEventRow.engagement_id == project.id,
                    OperationEventRow.occurred_at >= cutoff,
                    OperationEventRow.operation_kind == "harness_turn",
                    OperationEventRow.event_type.in_(_HARNESS_EVENT_TYPES),
                    OperationEventRow.payload["item_kind"].as_string() == "file_change",
                )
                .order_by(OperationEventRow.occurred_at.desc())
                .limit(MAX_HARNESS_EVENT_ROWS + 1)
            )
        )
        truncated = (
            len(observation_rows) > MAX_OBSERVATION_ROWS
            or len(event_rows) > MAX_HARNESS_EVENT_ROWS
        )
        observation_rows = observation_rows[:MAX_OBSERVATION_ROWS]
        event_rows = event_rows[:MAX_HARNESS_EVENT_ROWS]
        artifact_payloads: dict[str, dict[str, Any]] = {}
        if artifacts is not None:
            wanted = {
                str(inner["diff_artifact_id"])
                for _operation, _type, payload, _at in event_rows
                if isinstance(payload, dict)
                and isinstance(inner := payload.get("payload"), dict)
                and isinstance(inner.get("diff"), str)
                and _truncated(inner["diff"])
                and isinstance(inner.get("diff_artifact_id"), str)
            }
            if wanted:
                artifact_payloads = {
                    identity: payload
                    for identity, payload in await database.execute(
                        select(EntityRow.id, EntityRow.payload).where(
                            EntityRow.kind == Artifact.entity_kind,
                            EntityRow.engagement_id == project.id,
                            EntityRow.id.in_(sorted(wanted)),
                        )
                    )
                }

        def diff_text(artifact_id: Any) -> str | None:
            payload = artifact_payloads.get(str(artifact_id))
            if artifacts is None or payload is None:
                return None
            try:
                artifact = decode_stored(Artifact, payload)
                with artifacts.open(artifact) as stream:
                    data = stream.read(MAX_DIFF_ARTIFACT_BYTES + 1)
            except (OSError, ArtifactStoreError, ValueError) as exc:
                record_caught_exception(
                    "file_attribution",
                    "file_attribution.diff_artifact_unreadable",
                    "A harness diff artifact could not be read for file attribution.",
                    exc,
                    stage="diff-artifact",
                )
                return None
            if len(data) > MAX_DIFF_ARTIFACT_BYTES:
                return None
            return data.decode("utf-8", errors="replace")

        harness = _harness_events(event_rows, paths, diff_text)
        turn_owners: dict[str, tuple[str, str | None]] = {}
        harness_turn_ids = sorted({event.harness_turn_id for event in harness})
        for offset in range(0, len(harness_turn_ids), 500):
            batch = harness_turn_ids[offset : offset + 500]
            for identity, session_id, turn_id in await database.execute(
                select(
                    EntityRow.id,
                    EntityRow.chat_session_id,
                    EntityRow.payload["chat_turn_id"].as_string(),
                ).where(
                    EntityRow.kind == HarnessTurn.entity_kind,
                    EntityRow.engagement_id == project.id,
                    EntityRow.id.in_(batch),
                )
            ):
                if session_id:
                    turn_owners[identity] = (session_id, turn_id)

    changes: list[_Change] = []
    for payload in observation_rows:
        changes.extend(_observation_changes(payload, paths))
    harness_turns: set[tuple[str | None, str]] = set()
    for event in harness:
        owner = turn_owners.get(event.harness_turn_id)
        if owner is None:
            continue
        session_id, turn_id = owner
        harness_turns.add((turn_id, event.path))
        changes.append(
            _Change(
                path=event.path,
                session_id=session_id,
                turn_id=turn_id,
                at=event.at,
                kind=event.kind,
                attribution=event.attribution,
                source="harness",
                added=event.added,
                removed=event.removed,
                hunks=event.hunks,
            )
        )
    # A turn observation and its tool observations can report the same path;
    # keep the newest of them, and none when the harness reported that turn's
    # change with its patch.
    merged: dict[tuple[str, str | None, str], _Change] = {}
    kept: list[_Change] = []
    for change in changes:
        if change.source == "harness":
            kept.append(change)
            continue
        if (change.turn_id, change.path) in harness_turns:
            continue
        key = (change.session_id, change.turn_id, change.path)
        previous = merged.get(key)
        if previous is None or change.at > previous.at:
            merged[key] = change
    kept.extend(merged.values())
    kept = [change for change in kept if change.at >= cutoff]
    kept.sort(key=lambda change: (change.at, change.path), reverse=True)
    return _Scan(kept, truncated)


async def _project(store: NebulaStore, project_id: str) -> Engagement:
    try:
        return await store.get(Engagement, project_id)
    except NotFoundError as exc:
        raise HTTPException(404, "project not found") from exc


async def _sessions(
    store: NebulaStore, project_id: str, session_ids: set[str]
) -> dict[str, str]:
    """Titles of the project's visible chats among ``session_ids``."""

    titles: dict[str, str] = {}
    ordered = sorted(session_ids)
    async with store.database.session() as database:
        for offset in range(0, len(ordered), 500):
            for identity, title in await database.execute(
                select(EntityRow.id, EntityRow.payload["title"].as_string()).where(
                    EntityRow.kind == ChatSession.entity_kind,
                    EntityRow.engagement_id == project_id,
                    EntityRow.id.in_(ordered[offset : offset + 500]),
                )
            ):
                titles[identity] = title or ""
    return titles


async def _states(
    store: NebulaStore, project_id: str, session_ids: list[str]
) -> dict[str, str]:
    """Each chat's board state: waiting, working, failed (last day) or idle."""

    states = {session_id: "idle" for session_id in session_ids}
    if not session_ids:
        return states
    wanted = set(session_ids)
    cutoff = utc_now() - FAILED_WINDOW
    async with store.database.session() as database:
        headers = await pending_engagement_turn_headers(
            database, project_id, statuses=UNFINISHED_TURN_STATUSES
        )
        pending: dict[str, ChatTurnHeader] = {}
        for header in visible_pending(headers):
            if header.session_id in wanted:
                pending.setdefault(header.session_id, header)
        failed_candidates = [
            session_id
            for session_id in dict.fromkeys(
                (
                    await database.scalars(
                        select(EntityRow.chat_session_id)
                        .where(
                            EntityRow.kind == ChatTurn.entity_kind,
                            EntityRow.engagement_id == project_id,
                            EntityRow.chat_turn_status == ChatTurnStatus.FAILED.value,
                            EntityRow.updated_at >= cutoff,
                        )
                        .order_by(EntityRow.updated_at.desc())
                        .limit(MAX_FAILED_ITEMS * 4)
                    )
                ).all()
            )
            if session_id in wanted and session_id not in pending
        ]
        for session_id in failed_candidates[:MAX_FAILED_ITEMS]:
            latest = await latest_turn_header(database, session_id)
            if latest and latest.status == ChatTurnStatus.FAILED.value:
                states[session_id] = "failed"
    for session_id, header in pending.items():
        try:
            session = await store.get(ChatSession, session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: a chat deleted after the turn scan stays idle
            continue
        states[session_id] = await session_state(store, session, header)
    return states


async def _turn_indexes(
    store: NebulaStore, project_id: str, turn_ids: set[str]
) -> dict[str, int]:
    """Each turn's 1-based position in its conversation."""

    if not turn_ids:
        return {}
    indexes: dict[str, int] = {}
    async with store.database.session() as database:
        rows = list(
            await database.execute(
                select(
                    EntityRow.id, EntityRow.chat_session_id, EntityRow.created_at
                ).where(
                    EntityRow.kind == ChatTurn.entity_kind,
                    EntityRow.engagement_id == project_id,
                    EntityRow.id.in_(sorted(turn_ids)),
                )
            )
        )
        for identity, session_id, created_at in rows:
            if not session_id:
                continue
            position = await database.scalar(
                select(func.count())
                .select_from(EntityRow)
                .where(
                    EntityRow.kind == ChatTurn.entity_kind,
                    EntityRow.chat_session_id == session_id,
                    EntityRow.created_at <= created_at,
                )
            )
            indexes[identity] = int(position or 0) or 1
    return indexes


def _validated_path(path: str) -> str:
    relative = _clean_relative(path.strip())
    if relative is None:
        raise HTTPException(
            422, "path must be a workspace-relative POSIX path without '..'"
        )
    return relative


def file_attribution_router(
    store: NebulaStore, artifacts: ArtifactStore | None = None
) -> APIRouter:
    """Mount the project file-attribution reads.

    ``artifacts`` lets a reader replace an inline patch that the activity
    record cut short with its retained diff artifact. The SQL-only API worker
    passes none and reports such a change without line counts.
    """

    router = APIRouter(tags=["files"])

    @router.get("/projects/{project_id}/files/attribution", response_model=None)
    async def read_file_attribution(
        project_id: str,
        request: Request,
        response: Response,
        since_hours: int = Query(
            default=DEFAULT_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS
        ),
    ) -> Any:
        """The chat that most recently changed each workspace path."""

        response.headers["Cache-Control"] = "no-store"
        project = await _project(store, project_id)
        scan = await _scan(store, artifacts, project, _clamp_window(since_hours))
        titles = await _sessions(
            store, project.id, {change.session_id for change in scan.changes}
        )
        latest: dict[str, _Change] = {}
        for change in scan.changes:
            if change.session_id in titles and change.path not in latest:
                latest[change.path] = change
        shown = list(latest.values())[:MAX_ATTRIBUTED_PATHS]
        states = await _states(
            store, project.id, sorted({change.session_id for change in shown})
        )
        body = {
            "files": [
                {
                    "path": change.path,
                    "sessionId": change.session_id,
                    "sessionTitle": titles[change.session_id],
                    "turnId": change.turn_id,
                    "changedAt": change.at,
                    "state": states.get(change.session_id, "idle"),
                }
                for change in shown
            ],
            "truncated": scan.truncated or len(latest) > MAX_ATTRIBUTED_PATHS,
        }
        if unchanged := conditional(
            request, response, content_etag("file-attribution", body)
        ):
            return unchanged
        return body

    @router.get("/projects/{project_id}/files/attribution/history", response_model=None)
    async def read_file_attribution_history(
        project_id: str,
        request: Request,
        response: Response,
        path: str = Query(min_length=1, max_length=4096),
        since_hours: int = Query(default=MAX_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS),
    ) -> Any:
        """Chat changes to one workspace path, newest first."""

        response.headers["Cache-Control"] = "no-store"
        relative = _validated_path(path)
        project = await _project(store, project_id)
        scan = await _scan(store, artifacts, project, _clamp_window(since_hours))
        matching = [change for change in scan.changes if change.path == relative]
        titles = await _sessions(
            store, project.id, {change.session_id for change in matching}
        )
        shown = [change for change in matching if change.session_id in titles][
            :MAX_HISTORY_EVENTS
        ]
        indexes = await _turn_indexes(
            store, project.id, {change.turn_id for change in shown if change.turn_id}
        )
        body = {
            "path": relative,
            "events": [
                {
                    "sessionId": change.session_id,
                    "sessionTitle": titles[change.session_id],
                    "turnId": change.turn_id,
                    "turnIndex": indexes.get(change.turn_id)
                    if change.turn_id
                    else None,
                    "at": change.at,
                    "kind": change.kind,
                    "added": change.added,
                    "removed": change.removed,
                    "hunks": [hunk.as_json() for hunk in change.hunks]
                    if change.hunks is not None
                    else None,
                    "attribution": change.attribution,
                }
                for change in shown
            ],
        }
        if unchanged := conditional(
            request, response, content_etag("file-attribution-history", body)
        ):
            return unchanged
        return body

    return router


__all__ = [
    "DiffHunk",
    "FileDiff",
    "file_attribution_router",
    "parse_unified_diff",
]
