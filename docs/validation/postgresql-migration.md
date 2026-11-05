# PostgreSQL migration acceptance

The operator entry is `nebula-core migrate-to-postgresql`. The main relational
database owns saved IDs, revisions, transcripts, event sequences and idempotency
keys. Existing data-directory files continue to own artifacts, workspaces,
credentials, vector indexes, Butler memory and native mission checkpoints.

| Journey | Required invariant | Evidence |
| --- | --- | --- |
| Discover/inspect | CLI and settings assistant find the offline workflow; inspection preserves source | CLI/help and product-index/catalog tests |
| Copy/verify | Every reflected table is copied, counted and content hashed before commit | 15 migration cases against PostgreSQL 16.15 |
| Failure/retry | Populated targets are refused; copy/drift/digest failures roll back; uncertain commit is explicit | Migration and CLI cases |
| Use/restart | Existing chats, revisions, scheduled native missions and event cursors survive PG startup/restart | Authenticated real-Core LAN journey |
| Concurrent writes | Run and operation streams retain contiguous sequences and retry identities | PostgreSQL multi-process/bootstrap and thread tests |
| Backup/restore | Real server dump is retained; unknown candidate objects disappear; failed state survives restore failure/retry | 17 deployment/backup cases, including real pg_dump/psql restoration |

The focused selection records 67 Python cases and one frontend catalog case in
`.github/test-selection.json`. All selected cases passed across the migration,
CLI/discovery, PostgreSQL/storage/packaging and deployment slices. Python 3.12 and
PostgreSQL 16.15 were used on `simulation`; synthetic test databases were separate
from production. The frontend catalog test passed locally.

The synthetic CLI journey used non-loopback origins
`http://192.168.1.225:46433` and restart at
`http://192.168.1.225:42355`. Normal authentication remained enabled. It copied
SQLite data, reopened chats on PostgreSQL, checked revision conflicts, replayed
ordered/idempotent events, preserved a scheduled native mission, created another,
restarted Core, and cancelled those synthetic missions. Fixtures and server
processes were cleaned up.

No browser layout or native desktop interaction changes are made; viewport,
physical-device and browser geometry matrices do not apply to this CLI/backend
journey. Real providers, production data, full suites and an event-loop worker
refactor are outside this test selection. Frozen-driver inclusion is checked by
the package archive contract and the deployment build.
