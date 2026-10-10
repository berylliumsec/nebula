# Subagent restart recovery

Operator journey: restart Core with existing subagent conversations, reopen the
Assistant, and continue or inspect any work that was interrupted. Core owns the
durable subagent records, messages, reports, goal charges, and restart markers;
the browser only renders their current state.

| State at restart | Observable invariant | Focused evidence |
| --- | --- | --- |
| Settled root child with posted report and no pending message or charge | Core serves the Assistant without replaying delivery for that child | Core recovery test, packaged Core startup |
| Unposted report, pending message, or goal charge | Recovery posts or charges exactly once | Core recovery tests |
| Interrupted child awaiting recovery | Its restart marker is reconciled before reporting | Existing Core lifecycle tests |
| Nested child | Its report still reaches the ancestor conversation | Core recovery test |

The deployment gate checks the packaged binary against a copy of the live data,
then verifies the production LAN origin after the switch. Browser emulation is
not physical-device evidence.
