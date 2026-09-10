"""Read only non-secret runtime configuration for disposable acceptance sessions."""

import json
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1]).resolve()
with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
    row = connection.execute(
        "SELECT payload FROM entities WHERE kind='harnesses' "
        "AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.enabled')=1 ORDER BY updated_at DESC LIMIT 1",
        (sys.argv[2],),
    ).fetchone()
if row is None:
    raise SystemExit("No enabled configured runtime of the selected kind")
profile = json.loads(row[0])
if profile.get("connection_mode") != "spawn":
    raise SystemExit("This bounded acceptance fixture requires a spawned local runtime")
print(json.dumps({key: profile[key] for key in (
    "kind", "executable", "arguments", "connection_mode", "transport", "auth_mode", "default_model"
) if key in profile}))
