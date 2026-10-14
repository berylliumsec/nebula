# Optional notebook file index

From a project's **Notebook** view, expand **File index**, choose **Browse index
files**, and select a SQLite database on the Nebula host. The index is stored
per project. Remove it from the same control. A phone browser chooses a file on
the Nebula host, not on the phone.

Nebula supports a SQLite repository inventory with these tables and columns:

```sql
CREATE TABLE repositories (
  id INTEGER PRIMARY KEY, root TEXT UNIQUE, complete INTEGER,
  pending_files INTEGER, last_reconciled_at TEXT
);
CREATE TABLE files (
  repository_id INTEGER, path TEXT, size INTEGER, mtime_ns INTEGER
);
CREATE INDEX files_repo_path ON files(repository_id, path);
```

`root` must equal the resolved project folder on the Nebula host. `path` is a
project-relative POSIX path. `last_reconciled_at` is an ISO 8601 UTC timestamp.
The index owner should keep the inventory updated. Nebula uses indexed notebook
paths even while reconciliation is in progress. It shows a partial-index warning
when `complete=0`, `pending_files` is nonzero, or the last reconciliation is over
five minutes old. Some notebooks may be missing while the index is partial.
Nebula opens the database read-only and never writes to it. The database may use
SQLite WAL while its owner updates it.

Notebook discovery queries the index first. If it is unreadable, Nebula scans the
project folder. If a partial index has no notebooks, Nebula also scans the folder.
A complete index with no notebooks returns an empty list without a folder scan.
The index may exclude files by its own rules; an existing selected notebook still
opens directly from the project folder. Notebook reads and writes always validate
the real workspace path. Remove the index to use filesystem discovery alone.
