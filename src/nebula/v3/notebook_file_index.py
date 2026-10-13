"""Read-only, optional SQLite file inventory for project notebook discovery.

The supported format is the repository/files inventory described in the
documentation.  The database is a hint; the project workspace owns file reads.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote


class FileIndexUnavailable(ValueError):
    pass


def list_indexed_notebooks(database: str, workspace: Path) -> list[dict[str, object]]:
    root = workspace.expanduser().resolve(strict=True)
    index = Path(database).expanduser().resolve(strict=True)
    if not index.is_file() or index.suffix.lower() not in {
        ".db",
        ".sqlite",
        ".sqlite3",
    }:
        raise FileIndexUnavailable("Choose a SQLite file index on the Nebula host.")
    uri = f"file:{quote(str(index), safe='/')}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + 3
            connection.set_progress_handler(
                lambda: int(time.monotonic() > deadline), 10000
            )
            row = connection.execute(
                "SELECT id, complete, pending_files, last_reconciled_at "
                "FROM repositories WHERE root = ?",
                (str(root),),
            ).fetchone()
            if row is None:
                raise FileIndexUnavailable(
                    "This index does not contain the project folder."
                )
            repository_id, complete, pending, reconciled = row
            if not complete or pending:
                raise FileIndexUnavailable("The file index is still reconciling.")
            try:
                updated = datetime.fromisoformat(str(reconciled).replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - updated).total_seconds()
            except (TypeError, ValueError):
                raise FileIndexUnavailable(
                    "The file index has no valid refresh time."
                ) from None
            if age < -60 or age > 300:
                raise FileIndexUnavailable("The file index is out of date.")
            rows = connection.execute(
                "SELECT path, size, mtime_ns FROM files "
                "WHERE repository_id = ? AND path GLOB '*.ipynb' "
                "ORDER BY path LIMIT 1001",
                (repository_id,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise FileIndexUnavailable("The SQLite file index cannot be read.") from exc
    if len(rows) > 1000:
        rows = rows[:1000]
    result: list[dict[str, object]] = []
    for path, size, mtime_ns in rows:
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            continue
        relative = PurePosixPath(path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix != ".ipynb"
        ):
            continue
        try:
            result.append(
                {
                    "path": path,
                    "size": int(size or 0),
                    "modified_at": datetime.fromtimestamp(
                        int(mtime_ns or 0) / 1e9, timezone.utc
                    ).isoformat(),
                }
            )
        except (OverflowError, TypeError, ValueError):
            continue
    return result
