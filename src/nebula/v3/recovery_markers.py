"""Durable marks for operator pause and stop recorded while recovery was held.

Restart recovery continues interrupted work on its own. These marks keep it
from continuing work the operator paused or stopped before the project's
recovery finished; only the operator's own resume continues it.

It also names what interrupted work: a Core restart, or a stop of only the
project runtime while Core kept running (for example a deployment handoff).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# ChatGoal.metadata key: the goal was paused by the operator during recovery.
OPERATOR_RECOVERY_PAUSE = "operator_paused_during_recovery"
# ChatTurn.request_snapshot key: the operator asked to stop this response.
OPERATOR_RECOVERY_STOP = "operator_stop_requested_at"
# ChatGoal.metadata key: a transient provider or harness failure ended this
# goal's turn and Core is continuing it once on its own. A second failure in a
# row pauses the goal; a completed turn or an operator resume clears the mark.
GOAL_PROVIDER_RETRY = "provider_auto_retry"
# ChatTurn.request_snapshot key: this failed turn's goal was retried
# automatically; a provider turn's saved outcome note says so.
TURN_GOAL_AUTO_RETRY = "goal_auto_retry"
# ChatGoal.metadata key: Core interrupted one of this goal's turns because its
# callback wait made no progress, and resumed it once. A second stall before
# a turn completes pauses the goal; a completed turn or an operator resume
# clears the mark.
GOAL_CALLBACK_STALL = "callback_stall_recovered"
# ChatTurn.request_snapshot key: Core's stall watchdog interrupted this turn's
# callback wait; a second stall of the same turn is left for the operator.
TURN_CALLBACK_STALL = "callback_stall"
# Recovery causes the stall watchdog records. Only the first resumes by itself.
CALLBACK_STALL_CAUSE = "callback_stall"
CALLBACK_STALL_REPEATED_CAUSE = "callback_stall_repeated"
# Recovery cause of a response interrupted because its project runtime (the
# project's worker process) stopped while Core kept running: a deployment
# handoff that evicted the worker, a crash, or an OOM kill. Core resumes it by
# itself, as after a Core restart.
PROJECT_RUNTIME_STOP_CAUSE = "project_runtime_stop"
# Operator-facing causes ``_interrupt_orphaned_turn`` records for that stop.
PROJECT_RUNTIME_STOPPED = "Project runtime stopped"
PROJECT_RUNTIME_STOPPED_FOR_DEPLOYMENT = (
    "Project runtime stopped for a deployment handoff"
)
# HarnessTurn.metadata key: dead-owner reconciliation found this harness turn
# unfinished on a project runtime that stopped while Core kept running. The
# new worker's startup settlement says so instead of reporting a Core restart.
HARNESS_RUNTIME_STOP = "project_runtime_stop"

# Goal pause reasons the harness startup settlement records. Butler goal
# hygiene classifies them by these exact prefixes (butler_goal_hygiene.py).
HARNESS_CORE_RESTART_PAUSE = (
    "Core restarted during a harness goal turn. Review the saved reply before resuming."
)
HARNESS_QUEUED_CORE_RESTART_PAUSE = (
    "Core restarted before the harness goal turn started. Review it before resuming."
)
HARNESS_RUNTIME_STOP_PAUSE_PREFIX = (
    "Project runtime stopped during a harness goal turn;"
)
HARNESS_DEPLOYMENT_STOP_PAUSE_PREFIX = (
    "Project runtime stopped for a deployment handoff during a harness goal turn;"
)
HARNESS_QUEUED_RUNTIME_STOP_PAUSE_PREFIX = (
    "Project runtime stopped before the harness goal turn started;"
)
HARNESS_QUEUED_DEPLOYMENT_STOP_PAUSE_PREFIX = (
    "Project runtime stopped for a deployment handoff before the harness goal "
    "turn started;"
)


class _MarkedTurn(Protocol):
    @property
    def metadata(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HarnessStopCause:
    """How a harness turn's settlement after its producer died describes it."""

    subject: str
    reason: str
    goal_pause: str
    queued_goal_pause: str


def harness_stop_cause(turn: _MarkedTurn) -> HarnessStopCause:
    """Name what stopped an unfinished harness turn found at worker startup.

    Without the dead-owner reconciliation mark the previous Core stopped with
    it. With the mark only the project runtime stopped; Core kept running,
    and a deployment handoff is named when the mark records one. The harness
    outcome stays uncertain either way, so the goal waits for review.
    """

    mark = turn.metadata.get(HARNESS_RUNTIME_STOP)
    if not isinstance(mark, Mapping):
        return HarnessStopCause(
            subject="Nebula Core restarted",
            reason="core_restart",
            goal_pause=HARNESS_CORE_RESTART_PAUSE,
            queued_goal_pause=HARNESS_QUEUED_CORE_RESTART_PAUSE,
        )
    deployment = mark.get("deployment_handoff") is True
    return HarnessStopCause(
        subject=(
            "The project runtime stopped for a deployment handoff"
            if deployment
            else "The project runtime stopped"
        ),
        reason=PROJECT_RUNTIME_STOP_CAUSE,
        goal_pause=(
            HARNESS_DEPLOYMENT_STOP_PAUSE_PREFIX
            if deployment
            else HARNESS_RUNTIME_STOP_PAUSE_PREFIX
        )
        + " Core did not restart. Review the saved reply before resuming.",
        queued_goal_pause=(
            HARNESS_QUEUED_DEPLOYMENT_STOP_PAUSE_PREFIX
            if deployment
            else HARNESS_QUEUED_RUNTIME_STOP_PAUSE_PREFIX
        )
        + " Core did not restart. Review it before resuming.",
    )
