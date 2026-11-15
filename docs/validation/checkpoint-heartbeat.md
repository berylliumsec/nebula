# Checkpoint scans and lease renewal

Recovery and handoff must fingerprint all saved project references without
holding the ownership row that heartbeat renewal updates. The scan runs in a
read transaction; database triggers record changes to entities, provider queue
state and tool manifests, including writes from retained releases. Publication
locks ownership first, then the generation counter, and rejects a changed
generation. The v1 checkpoint digest and fresh lease checks remain required.

The operator journey is restoring a held project and resuming its saved goal.
SQL owns the project, checkpoint, and goal state. Relevant invariants are live
heartbeat renewal during a slow scan, no stale checkpoint publication, and no
automatic replay of uncertain effects. This change adds no UI controls, origin,
authentication, or browser behavior; viewport tests are not relevant.

Validation is restricted to CI by explicit operator instruction. Select the
checkpoint heartbeat regressions and existing batched digest compatibility
tests on SQLite and PostgreSQL, Python 3.12 only. No local tests or backups.
Production acceptance remains required after merge: restore the previously held
project, confirm fresh heartbeats and only the requested font goals running.
