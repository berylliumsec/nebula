# Compatibility memory import

Supervisor startup now imports the compatibility Butler SQLite file into fenced
Core SQL before opening shared services or admitting new runtimes. The initial
transition requires the active host lock and no resident execution process,
including a worker whose lease expired. Normal supervisor restarts use the
completed marker and preserve continuing SQL work; they never copy an old memory
snapshot over newer records.

The source remains a private, owned regular file and is opened read-only in a
SQLite snapshot, including committed WAL state. Batches contain at most 500
records. IDs, content hashes, visibility, revisions, source references, dates and
monotonic event cursors are preserved. Matching committed checkpoints make
interruption resumable. Changed checkpoints, malformed records, unsafe paths and
invalid files leave a durable blocked marker and preserve the source. Other
coordinator pools also reject runtime admission while the marker is incomplete.
No compatibility file is created for a fresh host. A completed import records
counts and source preservation in a deployment receipt without memory contents.

All 40 collected cases passed in 48.18 seconds: 24 new SQLite/PostgreSQL import
cases, 12 affected real Unix-service cases and four exact API promotion/rollback
and supervisor replacement cases. They include failure/cancellation after a
committed batch, source drift, symlink/permission/corruption rejection, blocking
another SQL pool during import, expired-worker retirement, source preservation
and new-work preservation after a completed import. Ruff and targeted mypy passed.
Two existing WebSocket-library deprecation warnings remain. Raw collection,
selection receipts and results are `/tmp/nebula-memory-import-*`.

Only repository code and synthetic compatibility files, SQL schemas and inert
workers were used. No existing installation, operational provider, SSH, deployment
or compute workload was accessed. This completes the memory-store migration
boundary; shared knowledge operations, production execution/client/desktop/deployer
wiring and final production LAN/WebKit/packaged continuity gates remain pending.
