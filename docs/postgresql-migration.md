# Move an existing Core database to PostgreSQL

Core supports PostgreSQL through `NEBULA_V3_DATABASE_URL`. PostgreSQL permits
independent agent transactions to write concurrently. Core retains transaction
locks for each ordered event stream and conflict checks for the same saved
record. Runtime SQL uses Psycopg 3 asynchronously; SQLite uses aiosqlite.
Each task owns its session, and database work completes before external calls.

`nebula-core migrate-to-postgresql` is an offline, one-way copy of the main
`nebula.db`. It preserves saved IDs, revisions, timestamps, JSON documents,
relationships, event sequences, and idempotency keys. It creates the current
PostgreSQL schema, copies rows in bounded batches, and compares row counts and
content digests before committing. A copy or verification failure rolls back the
target transaction. If the connection is lost during commit, the command reports
an uncertain outcome: inspect the destination before retrying. The command
refuses a populated target and does not change
Core's configuration. It never resets an existing database.

Artifacts, workspaces, credentials, mission checkpoints, Butler memory, and vector
indexes remain in the existing data directory or their configured locations.
Keep those files and use the same `--data-dir` when restarting Core. This command
does not migrate separate SQLite databases used by these services.

## Configure the runtime connection budget

PostgreSQL startup requires three explicit deployment allocations:

- `NEBULA_V3_DB_ADMIN_RESERVE`: additional administrative headroom, at least one
  connection, beyond PostgreSQL's own reserved slots.
- `NEBULA_V3_DB_OTHER_CONNECTIONS`: peak allocation for other clients, maintenance,
  probes and auxiliary pools, including consumers of the same role/database limits.
- `NEBULA_V3_DB_PEAK_PROCESSES`: maximum simultaneous Core processes, including
  rolling-deployment overlap. Every Core process owns one shared runtime pool.

Core reads `max_connections`, PostgreSQL's reserved slots, and role/database
connection limits. It subtracts the allocations above and divides the remaining
capacity across the declared process count. Startup refuses an infeasible budget.
For example, 100 server slots, three PostgreSQL-reserved slots, five additional
admin slots, twelve other-consumer slots, and two Core processes allow **40 slots
per Core**. This example is not a production default.

The pool never overflows. `NEBULA_V3_DB_POOL_SIZE` may select a smaller positive
pool, but cannot exceed the calculated ceiling. `NEBULA_V3_DB_POOL_TIMEOUT` is a
positive finite number of seconds, defaulting to 30. Zero or unlimited pool sizes
are invalid. Reconfigure the budget before increasing replicas or changing other
consumers; observed idle connections are not a capacity allocation.

Authenticated `GET /api/v1/diagnostics/performance` reports the declared budget,
pool occupancy and high-water mark, waiting callers, timeouts, acquisition time,
query/transaction time and event-loop lag. Bulk listings share bounded admission
so foreground requests retain capacity where the budget allows it.

## Prepare the server and source

PostgreSQL 16 is covered by the migration and startup tests. Use a dedicated database,
and a role that owns it. PostgreSQL must have enough disk for the copied data,
indexes, and WAL. Allow maintenance downtime for the copy and complete content
verification. The CLI is included in the Core package with its PostgreSQL driver;
a separate database conversion package is not required.

For a local Linux server, an administrator can create a role and database:

```sh
sudo -u postgres createuser --pwprompt nebula
sudo -u postgres createdb --owner=nebula nebula
```

Store the role password in the Core service user's protected libpq password file
(`~/.pgpass`, mode `0600`), or use another libpq-supported credential mechanism.
For a remote database, configure verified TLS with `sslmode=verify-full` and the
appropriate CA. Avoid putting passwords in shell history or command arguments.

Stop the desktop and every Core service using the source. Coordinate running
agents and goals before shutdown. Back up the complete data directory while Core
is stopped; preserve any separately configured artifact or workspace locations.
Keep this backup until PostgreSQL has passed recovery and normal-use checks.

If the source was created by an older Core, first run the current Core's normal
schema upgrade against SQLite, then stop Core again. The copy requires the
current schema and refuses an older or unexpected schema rather than guessing
how to transform it. `nebula-core migrate` performs schema upgrades; it does not
copy data between database engines.

## Inspect and copy

Inspect the SQLite source without configuring a target:

```sh
nebula-core migrate-to-postgresql --data-dir /path/to/core-data --inspect
```

`--source /path/to/snapshot.db` selects an existing SQLite snapshot explicitly.
Inspection is read-only; a live inspection describes that snapshot and does not
authorize copying from a running Core.

Create a private file containing only the destination URL. For example, the
following URL obtains its password from libpq's password file:

```text
postgresql+psycopg://nebula@127.0.0.1:5432/nebula
```

Protect the URL file with mode `0600`, then run:

```sh
nebula-core migrate-to-postgresql \
  --data-dir /path/to/core-data \
  --target-url-file /path/to/private-postgresql-url \
  --confirm-core-stopped \
  --receipt /path/to/new-migration-receipt.json
```

Alternatively, the command reads the target from `NEBULA_V3_DATABASE_URL`.
`--batch-size` bounds rows per batch; its default is 1,000. Progress is written to
standard error and the final verification report to standard output. The optional
receipt is created with mode `0600` and refuses to overwrite an existing file.
Reports contain counts and digests, not connection passwords.

Wait for the verified result before changing Core's database URL. A failed run
leaves the SQLite source unchanged. Correct the reported source or target issue
and repeat against an empty target. Never manually remove rows from a live target
to make it appear empty.

## Switch Core and verify

Configure `NEBULA_V3_DATABASE_URL` in the environment of the process that launches
Core. For systemd, use a protected service environment file or drop-in, then
reload the unit configuration if it changed. Restart Core with the same data
directory, artifact paths, credentials, and workspace mounts.

Run `nebula-core doctor --data-dir /path/to/core-data --json` with the same database
URL configured. Confirm its database dialect is PostgreSQL. Open the existing
projects and chats, verify transcripts and pending actions, make one ordinary
saved change, and reconnect to confirm that the change survives. Check normal
agent event ordering before resuming paused work.

Keep a PostgreSQL backup policy after cutover. The retained SQLite database is a
pre-cutover snapshot; new PostgreSQL activity is not written back into it.

The `simulation` deployment helper is aware of the configured database backend.
After PostgreSQL cutover it stores a custom-format `pg_dump` with a private
manifest and checksum beside the auxiliary SQLite snapshots. Restoration verifies
the dump and destination identity, decodes a private SQL staging file, then resets
and restores application schemas in one transaction. Allow scratch disk for the
uncompressed SQL staging file and install matching PostgreSQL client tools.
Failed server state is dumped before restoration and retained with its data files.
Restoring the pre-cutover SQLite rollback snapshot first requires restoring its
SQLite service configuration. The migration retains that coherent SQLite
snapshot for cutover recovery; ordinary healthy deployment retention keeps only
the deployer state's current rollback snapshot.

## Recover from a failed cutover

If Core cannot start or verification fails before work resumes, stop Core, preserve
the PostgreSQL target and migration receipt, restore the prior database URL (or
remove the override to select SQLite), and restart the accepted Core build with
the preserved complete data directory. Verify health and the original chats
before resuming work.

After PostgreSQL accepts new work, returning to the old SQLite snapshot would
discard that later activity. Preserve both stores and reconcile or restore from
a PostgreSQL backup before changing engines again. This command provides no
PostgreSQL-to-SQLite reverse migration.
