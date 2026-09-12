# Chat connection recovery contract

Entry: Workbench chat, send or reopen an active turn. Core owns execution and saved messages; the viewer owns only its connection, received sequence and connection status. URL/session selection controls attachment lifetime.

| Journey | Invariant | Test layer |
| --- | --- | --- |
| Send | Submit once; an unknown acceptance outcome is never automatically resubmitted | client |
| Stream/disconnect | Show reconnecting, retain transcript, attach read-only to the same accepted turn | client + real-Core browser |
| Replay | Resume after the last delivered sequence without duplicate text or activity | client + API |
| Quiet/stalled connection | Heartbeats keep quiet work observable; bounded timeout/backoff detects a dead viewer | client + API |
| Complete while offline | Read the durable final response; do not restart execution | API + browser |
| Switch/stop | Cancel viewer recovery and ignore late events; explicit Stop remains an execution action | client + browser |
| Refresh/background | Restore active identity and replay saved state; returning visibility wakes a reconnect attempt | client + browser |
| Failure/auth/restart | Distinguish connection recovery from execution failure; do not replay uncertain work | client + API |
| Create/delete/fork | No mutation changes; new execution still requires its existing explicit entry point | not applicable |

Focused tests will cover new transport/API contracts, existing viewer-independent execution tests, and a production real-Core LAN reconnection journey across the existing eight desktop/mobile profiles. No unrelated suites, live provider work or physical-device claims. Product principle: a lost viewer connection must never masquerade as failed execution or authorize a duplicate submission.
