"""Retention of the run and operation event ledgers.

Both ledgers are append-only while the record that owns an event exists. No
update ever succeeds, and the database triggers of migration 0018 refuse a
delete until the owner is gone. An event's owners are:

* a run event: the Mission (``runs``), chat turn (``chat_turns``) or harness
  turn (``harness_turns``) whose id is its ``run_id``. Every run-event writer
  keys its events by one of these, so a ``run_id`` that no longer names a
  record is history of a deleted one;
* an operation event: its project (``engagement_id``) and, for the operation
  kinds in ``OPERATION_EVENT_OWNER_KINDS``, the record whose id is its
  ``operation_id``.

A long conversation or project holds tens of thousands of events, too many to
delete inside the request that deletes it: that would hold SQLite's single
write lock, and block the event loop, for as long as it takes. Deleting a
record therefore hands its id to ``EventHistoryPurger``, which removes the
history in short batches off the caller's thread. At startup Core runs
``prune_orphaned_event_history`` for history an earlier Core left behind.
"""

from __future__ import annotations

from .diagnostics import record_caught_exception, record_diagnostic

import threading
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .database import Database, EntityRow, OperationEventRow, RunEventRow
from .domain import utc_now

# Operation kind -> the kind of record whose id is the event's operation_id.
# Kinds left out keep their events for as long as their project exists:
#   container_terminal         the id is a terminal session, which is not a
#                              record: these events are its audit trail, and
#                              `nebula-core doctor` checks the project's
#                              terminal command rows against them;
#   terminal_recording_policy  the id names the project's policy stream;
#   browser_assessment         issue candidates and validation grants outlive
#                              a deleted assessment and look their receipts
#                              up in its ledger.
# A kind added later is kept until it is listed here and in the triggers.
OPERATION_EVENT_OWNER_KINDS: dict[str, str] = {
    "action_intent": "action_intents",
    "artifact": "artifacts",
    "browser_action": "browser_actions",
    "browser_actions": "browser_actions",
    "browser_attack": "browser_attacks",
    "browser_attack_results": "browser_attack_results",
    "browser_attacks": "browser_attacks",
    "browser_automation_lease": "browser_automation_leases",
    "browser_automation_leases": "browser_automation_leases",
    "browser_command": "browser_commands",
    "browser_commands": "browser_commands",
    "browser_crawl": "browser_crawl_jobs",
    "browser_crawl_jobs": "browser_crawl_jobs",
    "browser_handoff": "browser_handoffs",
    "browser_handoffs": "browser_handoffs",
    "browser_identities": "browser_identities",
    "browser_intercept": "browser_intercepts",
    "browser_intercepts": "browser_intercepts",
    "browser_proxy_rule": "browser_proxy_rules",
    "browser_proxy_rules": "browser_proxy_rules",
    "browser_repeater_results": "browser_repeater_results",
    "browser_repeater_tab": "browser_repeater_tabs",
    "browser_repeater_tabs": "browser_repeater_tabs",
    "browser_session": "browser_sessions",
    "browser_sessions": "browser_sessions",
    "browser_site_edges": "browser_site_edges",
    "browser_site_node": "browser_site_nodes",
    "browser_site_nodes": "browser_site_nodes",
    "browser_token_analyses": "browser_token_analyses",
    "browser_traffic": "browser_traffic",
    "browser_websocket_frames": "browser_websocket_frames",
    "execution": "operator_executions",
    "findings": "findings",
    "handoff": "handoff_envelopes",
    "harness_turn": "harness_turns",
    "report_render": "report_renders",
    "reports": "reports",
}
OWNED_OPERATION_KINDS = frozenset(OPERATION_EVENT_OWNER_KINDS)
RUN_EVENT_OWNER_KINDS = frozenset({"runs", "chat_turns", "harness_turns"})
# Record kinds whose delete also deletes event history.
EVENT_OWNER_ENTITY_KINDS = RUN_EVENT_OWNER_KINDS | frozenset(
    OPERATION_EVENT_OWNER_KINDS.values()
)

# A ledger written this recently is left for the next prune: its owner may be a
# record another writer has yet to commit.
SETTLE_TIME = timedelta(minutes=10)
# Rows per committed delete, and the pause after each so live writers get the
# write lock in between.
BATCH_SIZE = 1_000
BATCH_PAUSE_SECONDS = 0.05
# Ids per IN list: well under SQLite's bound-parameter limit.
_ID_CHUNK = 500
_SCAN_PAGE = 1_000


def _chunks(values: Collection[str]) -> Iterable[list[str]]:
    ordered = sorted(values)
    for start in range(0, len(ordered), _ID_CHUNK):
        yield ordered[start : start + _ID_CHUNK]


def pause_between_batches() -> None:
    time.sleep(BATCH_PAUSE_SECONDS)


@dataclass(frozen=True)
class EventHistoryPruneReport:
    """What one prune or purge removed."""

    operation_events: int = 0
    run_events: int = 0
    projects: int = 0
    owners: int = 0
    batches: int = 0
    skipped_owners: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


_Target = tuple[str, Any, Any]


def _project_target(project_id: str) -> _Target:
    return (
        "projects",
        OperationEventRow,
        OperationEventRow.engagement_id == project_id,
    )


def _operation_target(owner_id: str) -> _Target:
    return (
        "owners",
        OperationEventRow,
        (OperationEventRow.operation_id == owner_id)
        & OperationEventRow.operation_kind.in_(OWNED_OPERATION_KINDS),
    )


def _run_target(owner_id: str) -> _Target:
    return ("owners", RunEventRow, RunEventRow.run_id == owner_id)


def _drain(
    database: Database,
    targets: Iterable[_Target],
    *,
    batch_size: int,
    pause: Callable[[], None] | None,
    stop: threading.Event | None,
) -> EventHistoryPruneReport:
    """Delete each target's rows, ``batch_size`` rows per transaction."""

    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    counts = dict.fromkeys(asdict(EventHistoryPruneReport()), 0)
    for counter, row, predicate in targets:
        if stop is not None and stop.is_set():
            break
        deleted = 0
        while not (stop is not None and stop.is_set()):
            try:
                with database.session() as session:
                    batch = select(row.id).where(predicate).limit(batch_size)
                    count = int(
                        session.execute(
                            delete(row)
                            .where(row.id.in_(batch))
                            .execution_options(synchronize_session=False)
                        ).rowcount
                        or 0
                    )
            except DBAPIError as exc:
                # A trigger refused the batch because the owner exists, or the
                # database stayed busy. That history is kept; the next prune
                # looks at it again.
                record_caught_exception(
                    "storage",
                    "storage.event_history.prune_batch_failed",
                    "An event-history prune batch failed; that history is kept.",
                    exc,
                    stage="event-history-prune",
                )
                counts["skipped_owners"] += 1
                break
            if not count:
                break
            deleted += count
            counts["batches"] += 1
            counts["run_events" if row is RunEventRow else "operation_events"] += count
            if pause is not None:
                pause()
            if count < batch_size:
                break
        if deleted:
            counts[counter] += 1
    report = EventHistoryPruneReport(**counts)
    if report.operation_events or report.run_events:
        record_diagnostic(
            "info",
            "storage",
            "storage.event_history.pruned",
            "Event history of deleted records was removed.",
            outcome="success",
            stage="event-history-prune",
            metadata=report.as_dict(),
        )
    return report


def purge_event_history(
    database: Database,
    *,
    owner_ids: Collection[str] = (),
    project_ids: Collection[str] = (),
    batch_size: int = BATCH_SIZE,
    pause: Callable[[], None] | None = None,
    stop: threading.Event | None = None,
) -> EventHistoryPruneReport:
    """Delete the history of records and projects that were just deleted.

    The triggers refuse a row whose owner still exists, so this only ever
    removes history that has lost its owner.
    """

    targets = [_project_target(project_id) for project_id in sorted(project_ids)]
    for owner_id in sorted(owner_ids):
        targets += [_operation_target(owner_id), _run_target(owner_id)]
    return _drain(database, targets, batch_size=batch_size, pause=pause, stop=stop)


def _missing_ids(session: Session, ids: list[str], *, kind: str | None) -> set[str]:
    missing: set[str] = set()
    for chunk in _chunks(ids):
        statement = select(EntityRow.id).where(EntityRow.id.in_(chunk))
        if kind is not None:
            statement = statement.where(EntityRow.kind == kind)
        missing.update(set(chunk) - set(session.scalars(statement)))
    return missing


def _distinct_owner_ids(session: Session, column: Any) -> Iterable[list[str]]:
    """Page through one ledger's distinct owner ids on its replay index."""

    after = ""
    while True:
        page = list(
            session.scalars(
                select(column)
                .where(column > after)
                .group_by(column)
                .order_by(column)
                .limit(_SCAN_PAGE)
            )
        )
        if not page:
            return
        yield page
        after = page[-1]


def _is_before(value: datetime, bound: datetime) -> bool:
    if value.tzinfo is None and bound.tzinfo is not None:
        value = value.replace(tzinfo=bound.tzinfo)
    return value < bound


def _orphaned_projects(database: Database, settled_before: datetime) -> list[str]:
    with database.session() as session:
        project_ids = list(
            session.scalars(select(OperationEventRow.engagement_id).distinct())
        )
        settled = []
        for project_id in sorted(
            _missing_ids(session, project_ids, kind="engagements")
        ):
            newest = session.scalar(
                select(OperationEventRow.occurred_at)
                .where(OperationEventRow.engagement_id == project_id)
                .order_by(OperationEventRow.occurred_at.desc())
                .limit(1)
            )
            if newest is not None and _is_before(newest, settled_before):
                settled.append(project_id)
        return settled


def _orphaned_owners(
    database: Database,
    row: Any,
    owner: Any,
    settled_before: datetime,
    *criteria: Any,
) -> list[str]:
    """Owner ids no record has, whose newest prunable event has settled."""

    with database.session() as session:
        orphans: list[str] = []
        for page in _distinct_owner_ids(session, owner):
            for owner_id in sorted(_missing_ids(session, page, kind=None)):
                newest = session.scalar(
                    select(row.occurred_at)
                    .where(owner == owner_id, *criteria)
                    .order_by(row.sequence.desc())
                    .limit(1)
                )
                if newest is not None and _is_before(newest, settled_before):
                    orphans.append(owner_id)
        return orphans


def prune_orphaned_event_history(
    database: Database,
    *,
    batch_size: int = BATCH_SIZE,
    min_age: timedelta = SETTLE_TIME,
    pause: Callable[[], None] | None = None,
    stop: threading.Event | None = None,
) -> EventHistoryPruneReport:
    """Find and delete run and operation events whose owner no longer exists.

    Only history of a deleted project, of a deleted record of a kind in
    ``OPERATION_EVENT_OWNER_KINDS``, or of a deleted Mission, chat turn or
    harness turn goes. Each batch of at most ``batch_size`` rows commits on
    its own, so a large backlog never holds one long write transaction, and
    a rerun finds nothing left to remove. A ledger whose newest event is
    younger than ``min_age`` is left for the next prune; ``pause`` runs after
    each batch and ``stop`` ends the prune after the current one.
    """

    settled_before = utc_now() - min_age
    # A terminal session or other unlisted kind is never a missing record.
    operation_owners = _orphaned_owners(
        database,
        OperationEventRow,
        OperationEventRow.operation_id,
        settled_before,
        OperationEventRow.operation_kind.in_(OWNED_OPERATION_KINDS),
    )
    run_owners = _orphaned_owners(
        database, RunEventRow, RunEventRow.run_id, settled_before
    )
    targets = [
        *map(_project_target, _orphaned_projects(database, settled_before)),
        *map(_operation_target, operation_owners),
        *map(_run_target, run_owners),
    ]
    return _drain(database, targets, batch_size=batch_size, pause=pause, stop=stop)


class EventHistoryPurger:
    """Remove the history of deleted records on one background thread.

    A delete hands over the ids it removed and returns at once. The thread
    exits when nothing is pending and starts again with the next delete; an
    interrupted purge leaves history the next startup prune removes.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock = threading.Lock()
        self._owners: set[str] = set()
        self._projects: set[str] = set()
        self._worker: threading.Thread | None = None
        self._idle = threading.Event()
        self._idle.set()
        self._stop = threading.Event()

    def submit(
        self, owner_ids: Iterable[str] = (), project_ids: Iterable[str] = ()
    ) -> None:
        with self._lock:
            self._owners.update(owner_ids)
            self._projects.update(project_ids)
            if not (self._owners or self._projects):
                return
            self._idle.clear()
            self._stop.clear()
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._run, name="nebula-event-history-purge", daemon=True
                )
                self._worker.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stop.is_set() or not (self._owners or self._projects):
                    self._worker = None
                    self._idle.set()
                    return
                owners, self._owners = self._owners, set()
                projects, self._projects = self._projects, set()
            try:
                purge_event_history(
                    self.database,
                    owner_ids=owners,
                    project_ids=projects,
                    pause=pause_between_batches,
                    stop=self._stop,
                )
            except Exception as exc:
                # The records are already gone; their history waits for the
                # next startup prune.
                record_caught_exception(
                    "storage",
                    "storage.event_history.purge_failed",
                    "Removing a deleted record's event history failed; Core retries at startup.",
                    exc,
                    stage="event-history-purge",
                )

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until every submitted history is removed or set aside."""

        return self._idle.wait(timeout)

    def close(self, timeout: float = 10) -> None:
        """Stop after the current batch; what is pending waits for startup."""

        with self._lock:
            worker = self._worker
            self._stop.set()
        if worker is not None:
            worker.join(timeout)
