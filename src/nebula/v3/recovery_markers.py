"""Durable marks for operator pause and stop recorded while recovery was held.

Restart recovery continues interrupted work on its own. These marks keep it
from continuing work the operator paused or stopped before the project's
recovery finished; only the operator's own resume continues it.
"""

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
