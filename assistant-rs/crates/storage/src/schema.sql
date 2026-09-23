-- Private assistant laboratory schema, deliberately NOT a Nebula migration.
PRAGMA application_id = 1312964940;
PRAGMA user_version = 1;
CREATE TABLE assistant_sequences (
    turn_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL CHECK(last_sequence >= 1)
);
CREATE TABLE assistant_events (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(json_valid(payload)),
    actor_id TEXT,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT,
    UNIQUE(turn_id, sequence),
    UNIQUE(turn_id, idempotency_key)
);
CREATE TRIGGER assistant_events_no_update BEFORE UPDATE ON assistant_events
BEGIN SELECT RAISE(ABORT, 'assistant events are append-only'); END;
CREATE TRIGGER assistant_events_no_delete BEFORE DELETE ON assistant_events
BEGIN SELECT RAISE(ABORT, 'assistant events are append-only'); END;
