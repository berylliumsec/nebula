"""Durable marks for operator pause and stop recorded while recovery was held.

Restart recovery continues interrupted work on its own. These marks keep it
from continuing work the operator paused or stopped before the project's
recovery finished; only the operator's own resume continues it.
"""

# ChatGoal.metadata key: the goal was paused by the operator during recovery.
OPERATOR_RECOVERY_PAUSE = "operator_paused_during_recovery"
# ChatTurn.request_snapshot key: the operator asked to stop this response.
OPERATOR_RECOVERY_STOP = "operator_stop_requested_at"
