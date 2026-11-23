"""Durable marks for operator pause and stop recorded while recovery was held.

Restart recovery continues interrupted work on its own. These marks keep it
from continuing work the operator paused or stopped before the project's
recovery finished; only the operator's own resume continues it.
"""

# ChatGoal.metadata key: the goal was paused by the operator during recovery.
OPERATOR_RECOVERY_PAUSE = "operator_paused_during_recovery"
# ChatTurn.request_snapshot key: the operator asked to stop this response.
OPERATOR_RECOVERY_STOP = "operator_stop_requested_at"
# ChatGoal.metadata key: a transient provider failure ended this goal's turn
# and Core is continuing it once on its own. A second failure in a row pauses
# the goal; a completed turn or an operator resume clears the mark.
GOAL_PROVIDER_RETRY = "provider_auto_retry"
# ChatTurn.request_snapshot key: this failed turn's goal was retried
# automatically; its saved outcome note says so.
TURN_GOAL_AUTO_RETRY = "goal_auto_retry"
