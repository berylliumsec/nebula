# Simulation runtime admission repair

Operator journey: open an existing project, read its chats and settings, and
receive delivery through its authoritative execution worker.

| Journey | Invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Open project | Execution receives an unexpired ownership grant | PostgreSQL ownership and runtime leases | live deployment readback |
| Retry admission | Expired ownership uses a new incarnation and fencing token | recovery operation ledger | live recovery receipt |
| Refresh | Same durable project and chat data remain readable | Core database and API | authenticated readback |
| Failure | Uncertain effects remain held rather than replayed | recovery blockers | recovery receipt |

A compatibility scan can take longer than the 30-second ownership lease.
Previously the grant used the clock read before that scan, allowing a newly
committed grant to be expired at the first worker attachment. Independent
read-only review confirmed the stale-clock fault and the logically redundant
project filter that bounds the snapshot-part scan.

Admission and recovery now revalidate runtime liveness and refresh the database
clock after compatibility/checkpoint reads, immediately before granting ownership.
Fencing and expired-owner recovery requirements remain intact.

The operator explicitly prohibited tests, backups, and Butler coordination.
No automated tests, browser matrix, or synthetic LAN instance are run for this
change. Production build and authenticated operational readbacks are the allowed
verification; they do not establish full product acceptance. No settings or
operator feature is added, so assistant catalogs and product knowledge do not change.

The follow-up repair bounds cold activity polling by indexed pending turn status.
Terminal and settled interrupted turns are excluded before payload hydration;
unknown legacy projections retain their fallback, and candidate rows must still
belong to an existing project conversation. Header revalidation and duplicate
pending-turn detection remain in place. Independent read-only review traced the
cold-cache transaction to heartbeat starvation and confirmed these guards.
