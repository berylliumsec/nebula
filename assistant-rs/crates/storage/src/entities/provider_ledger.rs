//! Explicit, additive provider ledger installation for a recognized Nebula SQLite database.
//!
//! These APIs operate on the caller's owned connection; they do not open files or authorize
//! dispatch. The runtime must still reconcile receipts, row hashes, watched epochs, and byte
//! accounting before using retained evidence. No legacy migration marker is changed here.

pub mod writer;

use sha2::{Digest, Sha256};
use sqlx::{Connection, Row, SqliteConnection};
use uuid::Uuid;

const PREFIX: &str = "assistant_provider_";
const MAX_OBJECTS: usize = 64;
const MAX_MANIFEST_BYTES: usize = 1024 * 1024;
const MAX_OBJECT_BYTES: usize = 64 * 1024;
const MAX_SEQUENCE: i64 = 9_007_199_254_740_991;
const MAX_EVENT_BYTES: usize = 1024 * 1024;
const MAX_RECEIPT_BYTES: usize = 1024 * 1024;

#[derive(Clone, Copy, Debug)]
pub struct SchemaLimits {
    /// Current trusted capacity, not a persisted assertion of available physical disk.
    pub capacity_bytes: u64,
}

impl SchemaLimits {
    fn capacity(self) -> Result<i64> {
        i64::try_from(self.capacity_bytes)
            .ok()
            .filter(|value| *value > 0)
            .ok_or(Error::InvalidLimits)
    }
}

#[derive(Clone, Copy, Debug)]
pub struct InstallIdentity {
    pub database_uuid: Uuid,
    pub installed_at_us: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub database_uuid: Uuid,
    pub installed_at_us: i64,
    pub migration_sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Status {
    Absent,
    Installed(Identity),
}

/// Display and Debug intentionally never expose retained rows or SQL error text.
pub enum Error {
    InvalidLimits,
    IncompatibleBase,
    ForeignKeysDisabled,
    AlreadyInstalled,
    PartialOrUnknownSchema,
    ManifestLimit,
    InvalidIdentity,
    UnsupportedVersion,
    InvalidBudget,
    CapacityExceeded,
    Sql(sqlx::Error),
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::InvalidLimits => "Invalid provider ledger capacity",
            Self::IncompatibleBase => "Provider ledger requires the recognized Nebula schema",
            Self::ForeignKeysDisabled => "Provider ledger requires foreign key enforcement",
            Self::AlreadyInstalled => "Provider ledger is already installed",
            Self::PartialOrUnknownSchema => "Provider ledger schema is incomplete or unrecognized",
            Self::ManifestLimit => "Provider ledger schema metadata exceeds its bound",
            Self::InvalidIdentity => "Provider ledger identity is invalid",
            Self::UnsupportedVersion => "Provider ledger version is unsupported",
            Self::InvalidBudget => "Provider ledger budget is invalid",
            Self::CapacityExceeded => "Provider ledger exceeds the configured capacity",
            Self::Sql(_) => "Provider ledger SQLite operation failed",
        })
    }
}
impl std::fmt::Debug for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        std::fmt::Display::fmt(self, f)
    }
}
impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Sql(source) => Some(source),
            _ => None,
        }
    }
}
impl From<sqlx::Error> for Error {
    fn from(value: sqlx::Error) -> Self {
        Self::Sql(value)
    }
}
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug)]
struct Object {
    kind: &'static str,
    name: String,
    table: String,
    sql: String,
}

fn identifier(name: &str) -> String {
    format!(
        "typeof({name}) = 'text' AND length({name}) BETWEEN 1 AND 200 AND instr({name}, char(0)) = 0"
    )
}
fn hash(name: &str) -> String {
    format!(
        "typeof({name}) = 'text' AND length(CAST({name} AS BLOB)) = 64 AND instr({name}, char(0)) = 0 AND {name} NOT GLOB '*[^0-9a-f]*'"
    )
}
fn integer(name: &str) -> String {
    format!("typeof({name}) = 'integer'")
}
fn counter(name: &str) -> String {
    format!("{} AND {name} >= 0", integer(name))
}
fn table(objects: &mut Vec<Object>, suffix: &str, body: String) {
    let name = format!("{PREFIX}{suffix}");
    objects.push(Object {
        kind: "table",
        table: name.clone(),
        // Hidden rowids would add an unguarded replacement identity: REPLACE can remove
        // a victim without DELETE triggers when recursive_triggers is disabled.
        sql: format!("CREATE TABLE {name} ({body}) WITHOUT ROWID"),
        name,
    });
}
fn trigger(objects: &mut Vec<Object>, suffix: &str, table: &str, clause: String) {
    let name = format!("{PREFIX}{suffix}");
    objects.push(Object {
        kind: "trigger",
        name: name.clone(),
        table: table.to_owned(),
        sql: format!("CREATE TRIGGER {name} {clause}"),
    });
}
fn abort() -> &'static str {
    "BEGIN SELECT RAISE(ABORT, 'provider ledger invariant'); END"
}

/// A single exact manifest defines both installation and recognition. No IF NOT EXISTS repair.
fn manifest() -> Vec<Object> {
    let mut objects = Vec::new();
    table(
        &mut objects,
        "schema",
        format!(
            "singleton INTEGER PRIMARY KEY CHECK(singleton = 1),\nversion INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version = 1),\ndatabase_uuid TEXT NOT NULL UNIQUE CHECK(typeof(database_uuid) = 'text' AND length(database_uuid) = 36 AND instr(database_uuid, char(0)) = 0),\nmigration_sha256 TEXT NOT NULL CHECK({}),\ninstalled_at_us INTEGER NOT NULL CHECK({})",
            hash("migration_sha256"),
            integer("installed_at_us")
        ),
    );
    table(
        &mut objects,
        "streams",
        format!(
            "turn_id TEXT PRIMARY KEY NOT NULL CHECK({}),\nstream_id TEXT NOT NULL UNIQUE CHECK({}),\nsession_id TEXT NOT NULL CHECK({}),\nproject_id TEXT NOT NULL CHECK({}),\nstate TEXT NOT NULL CHECK(state IN ('active','terminal','quarantined','deleted')),\nlast_sequence INTEGER NOT NULL DEFAULT 0 CHECK({} AND last_sequence <= {MAX_SEQUENCE}),\nfirst_retained_sequence INTEGER NOT NULL DEFAULT 1 CHECK({} AND first_retained_sequence BETWEEN 1 AND {next_sequence}),\nevent_bytes INTEGER NOT NULL CHECK({}),\nreserved_bytes INTEGER NOT NULL CHECK({}),\nreceipt_head_sequence INTEGER NOT NULL DEFAULT 0 CHECK({}),\nreceipt_head_sha256 TEXT NOT NULL CHECK({}),\nobserved_turn_epoch INTEGER NOT NULL CHECK({}),\ndiscontinuity_reason TEXT CHECK(discontinuity_reason IS NULL OR (typeof(discontinuity_reason) = 'text' AND length(CAST(discontinuity_reason AS BLOB)) <= 4096)),\nterminal_sequence INTEGER CHECK(terminal_sequence IS NULL OR (typeof(terminal_sequence) = 'integer' AND terminal_sequence BETWEEN 1 AND last_sequence)),\ncreated_at_us INTEGER NOT NULL CHECK({}),\nupdated_at_us INTEGER NOT NULL CHECK({}),\nCHECK(first_retained_sequence <= last_sequence + 1),\nCHECK(event_bytes <= 9223372036854775807 - reserved_bytes)",
            identifier("turn_id"),
            identifier("stream_id"),
            identifier("session_id"),
            identifier("project_id"),
            counter("last_sequence"),
            integer("first_retained_sequence"),
            counter("event_bytes"),
            counter("reserved_bytes"),
            counter("receipt_head_sequence"),
            hash("receipt_head_sha256"),
            counter("observed_turn_epoch"),
            integer("created_at_us"),
            integer("updated_at_us"),
            next_sequence = MAX_SEQUENCE + 1
        ),
    );
    objects.push(Object { kind: "index", name: format!("{PREFIX}streams_recovery"), table: format!("{PREFIX}streams"), sql: "CREATE INDEX assistant_provider_streams_recovery ON assistant_provider_streams(state, project_id, session_id, turn_id)".to_owned() });
    table(
        &mut objects,
        "attempts",
        format!(
            "attempt_id TEXT PRIMARY KEY NOT NULL CHECK({}),\nturn_id TEXT NOT NULL CHECK({}),\nordinal INTEGER NOT NULL CHECK({} AND ordinal >= 1),\npredecessor_attempt_id TEXT CHECK(predecessor_attempt_id IS NULL OR ({})),\nowner_id TEXT NOT NULL CHECK({}),\nclaim_id TEXT NOT NULL CHECK({}),\nrequest_sha256 TEXT NOT NULL CHECK({}),\nroute_sha256 TEXT NOT NULL CHECK({}),\nphase TEXT NOT NULL CHECK(phase IN ('admitted','dispatch_intent','answer_saved','completed','released','cancelled','failed','uncertain')),\nUNIQUE(turn_id, ordinal),\nUNIQUE(turn_id, claim_id),\nUNIQUE(turn_id, attempt_id),\nFOREIGN KEY(turn_id) REFERENCES assistant_provider_streams(turn_id),\nFOREIGN KEY(turn_id, predecessor_attempt_id) REFERENCES assistant_provider_attempts(turn_id, attempt_id),\nCHECK(predecessor_attempt_id IS NULL OR predecessor_attempt_id != attempt_id)",
            identifier("attempt_id"),
            identifier("turn_id"),
            integer("ordinal"),
            identifier("predecessor_attempt_id"),
            identifier("owner_id"),
            identifier("claim_id"),
            hash("request_sha256"),
            hash("route_sha256")
        ),
    );
    table(
        &mut objects,
        "receipts",
        format!(
            "turn_id TEXT NOT NULL CHECK({}),\nreceipt_sequence INTEGER NOT NULL CHECK({} AND receipt_sequence >= 1),\nattempt_id TEXT CHECK(attempt_id IS NULL OR ({})),\nkind TEXT NOT NULL CHECK(kind IN ('admitted','claimed','dispatch_intent','answer_saved','completed','released','cancelled','failed','recovery_classified','lineage_invalidated','retention_boundary')),\ncommand_key TEXT NOT NULL CHECK({}),\ncommand_sha256 TEXT NOT NULL CHECK({}),\nreceipt_json TEXT NOT NULL CHECK(typeof(receipt_json) = 'text' AND length(CAST(receipt_json AS BLOB)) <= {MAX_RECEIPT_BYTES} AND instr(receipt_json, char(0)) = 0 AND json_valid(receipt_json)),\nprevious_sha256 TEXT NOT NULL CHECK({}),\nreceipt_sha256 TEXT NOT NULL CHECK({}),\ncommitted_at_us INTEGER NOT NULL CHECK({}),\nPRIMARY KEY(turn_id, receipt_sequence),\nUNIQUE(turn_id, command_key),\nFOREIGN KEY(turn_id) REFERENCES assistant_provider_streams(turn_id),\nFOREIGN KEY(turn_id, attempt_id) REFERENCES assistant_provider_attempts(turn_id, attempt_id)",
            identifier("turn_id"),
            integer("receipt_sequence"),
            identifier("attempt_id"),
            identifier("command_key"),
            hash("command_sha256"),
            hash("previous_sha256"),
            hash("receipt_sha256"),
            integer("committed_at_us")
        ),
    );
    objects.push(Object {
        kind: "index",
        name: format!("{PREFIX}receipts_control"),
        table: format!("{PREFIX}receipts"),
        sql: "CREATE INDEX assistant_provider_receipts_control ON assistant_provider_receipts(turn_id, kind, attempt_id)".to_owned(),
    });
    table(
        &mut objects,
        "events",
        format!(
            "turn_id TEXT NOT NULL CHECK({}),\nsequence INTEGER NOT NULL CHECK({} AND sequence BETWEEN 1 AND {MAX_SEQUENCE}),\nattempt_id TEXT CHECK(attempt_id IS NULL OR ({})),\nevent_type TEXT NOT NULL CHECK({}),\nevent_key TEXT NOT NULL CHECK({}),\nevent_json TEXT NOT NULL CHECK(typeof(event_json) = 'text' AND length(CAST(event_json AS BLOB)) <= {MAX_EVENT_BYTES} AND instr(event_json, char(0)) = 0 AND json_valid(event_json)),\nsse_bytes BLOB NOT NULL CHECK(typeof(sse_bytes) = 'blob'),\nsse_bytes_len INTEGER NOT NULL CHECK({} AND sse_bytes_len BETWEEN 1 AND {MAX_EVENT_BYTES} AND sse_bytes_len = length(sse_bytes)),\ncontent_sha256 TEXT NOT NULL CHECK({}),\nprevious_sha256 TEXT NOT NULL CHECK({}),\nevent_sha256 TEXT NOT NULL CHECK({}),\ncommitted_at_us INTEGER NOT NULL CHECK({}),\nsettlement_receipt_sequence INTEGER CHECK(settlement_receipt_sequence IS NULL OR (typeof(settlement_receipt_sequence) = 'integer' AND settlement_receipt_sequence >= 1)),\nCHECK((event_type IN ('done','error','cancelled','interrupted')) = (settlement_receipt_sequence IS NOT NULL)),\nPRIMARY KEY(turn_id, sequence),\nUNIQUE(turn_id, event_key),\nFOREIGN KEY(turn_id) REFERENCES assistant_provider_streams(turn_id),\nFOREIGN KEY(turn_id, attempt_id) REFERENCES assistant_provider_attempts(turn_id, attempt_id),\nFOREIGN KEY(turn_id, settlement_receipt_sequence) REFERENCES assistant_provider_receipts(turn_id, receipt_sequence)",
            identifier("turn_id"),
            integer("sequence"),
            identifier("attempt_id"),
            identifier("event_type"),
            identifier("event_key"),
            integer("sse_bytes_len"),
            hash("content_sha256"),
            hash("previous_sha256"),
            hash("event_sha256"),
            integer("committed_at_us")
        ),
    );
    table(
        &mut objects,
        "entity_epochs",
        format!(
            "entity_id TEXT PRIMARY KEY NOT NULL CHECK({}),\nmutation_epoch INTEGER NOT NULL DEFAULT 0 CHECK({}),\npresent INTEGER NOT NULL CHECK(typeof(present) = 'integer' AND present IN (0,1)),\nretained_rowid INTEGER CHECK(retained_rowid IS NULL OR typeof(retained_rowid) = 'integer'),\nCHECK((present = 0 AND retained_rowid IS NULL) OR (present = 1 AND retained_rowid IS NOT NULL))",
            identifier("entity_id"),
            counter("mutation_epoch")
        ),
    );
    // A legacy physical row has one current logical identity. This derived locator
    // bounds victim invalidation to one row, including when DELETE triggers are skipped
    // by SQLite REPLACE. Maintenance that rewrites physical rowids needs reconciliation.
    objects.push(Object {
        kind: "index",
        name: format!("{PREFIX}entity_epochs_locator"),
        table: format!("{PREFIX}entity_epochs"),
        sql: "CREATE UNIQUE INDEX assistant_provider_entity_epochs_locator ON assistant_provider_entity_epochs(retained_rowid) WHERE present = 1".to_owned(),
    });
    table(
        &mut objects,
        "budget",
        format!(
            "singleton INTEGER PRIMARY KEY CHECK(singleton = 1),\nused_bytes INTEGER NOT NULL CHECK({}),\nreserved_bytes INTEGER NOT NULL CHECK({}),\nCHECK(used_bytes <= 9223372036854775807 - reserved_bytes)",
            counter("used_bytes"),
            counter("reserved_bytes")
        ),
    );

    for suffix in ["schema", "budget", "streams", "entity_epochs"] {
        let name = format!("{PREFIX}{suffix}");
        trigger(
            &mut objects,
            &format!("{suffix}_no_delete"),
            &name,
            format!("BEFORE DELETE ON {name} {}", abort()),
        );
    }
    for suffix in ["schema", "budget"] {
        let name = format!("{PREFIX}{suffix}");
        trigger(
            &mut objects,
            &format!("{suffix}_no_replace"),
            &name,
            format!(
                "BEFORE INSERT ON {name} WHEN EXISTS(SELECT 1 FROM {name}) {}",
                abort()
            ),
        );
    }
    trigger(
        &mut objects,
        "schema_no_update",
        "assistant_provider_schema",
        format!("BEFORE UPDATE ON assistant_provider_schema {}", abort()),
    );
    trigger(
        &mut objects,
        "budget_identity",
        "assistant_provider_budget",
        format!(
            "BEFORE UPDATE ON assistant_provider_budget WHEN NEW.singleton IS NOT OLD.singleton {}",
            abort()
        ),
    );
    trigger(
        &mut objects,
        "streams_no_replace",
        "assistant_provider_streams",
        format!(
            "BEFORE INSERT ON assistant_provider_streams WHEN EXISTS(SELECT 1 FROM assistant_provider_streams WHERE turn_id = NEW.turn_id OR stream_id = NEW.stream_id) {}",
            abort()
        ),
    );
    trigger(
        &mut objects,
        "streams_identity",
        "assistant_provider_streams",
        format!(
            "BEFORE UPDATE ON assistant_provider_streams WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.stream_id IS NOT OLD.stream_id OR NEW.session_id IS NOT OLD.session_id OR NEW.project_id IS NOT OLD.project_id OR NEW.created_at_us IS NOT OLD.created_at_us OR NEW.last_sequence < OLD.last_sequence OR NEW.first_retained_sequence < OLD.first_retained_sequence OR NEW.receipt_head_sequence < OLD.receipt_head_sequence OR NEW.observed_turn_epoch < OLD.observed_turn_epoch OR (OLD.state = 'deleted' AND NEW.state != 'deleted') {}",
            abort()
        ),
    );
    trigger(
        &mut objects,
        "attempts_no_replace",
        "assistant_provider_attempts",
        format!(
            "BEFORE INSERT ON assistant_provider_attempts WHEN EXISTS(SELECT 1 FROM assistant_provider_attempts WHERE attempt_id = NEW.attempt_id OR (turn_id = NEW.turn_id AND (ordinal = NEW.ordinal OR claim_id = NEW.claim_id))) {}",
            abort()
        ),
    );
    trigger(
        &mut objects,
        "attempts_identity",
        "assistant_provider_attempts",
        format!(
            "BEFORE UPDATE ON assistant_provider_attempts WHEN NEW.attempt_id IS NOT OLD.attempt_id OR NEW.turn_id IS NOT OLD.turn_id OR NEW.ordinal IS NOT OLD.ordinal OR NEW.predecessor_attempt_id IS NOT OLD.predecessor_attempt_id OR NEW.owner_id IS NOT OLD.owner_id OR NEW.claim_id IS NOT OLD.claim_id OR NEW.request_sha256 IS NOT OLD.request_sha256 OR NEW.route_sha256 IS NOT OLD.route_sha256 {}",
            abort()
        ),
    );
    for (suffix, sequence, key) in [
        ("receipts", "receipt_sequence", "command_key"),
        ("events", "sequence", "event_key"),
    ] {
        let name = format!("{PREFIX}{suffix}");
        trigger(
            &mut objects,
            &format!("{suffix}_no_update"),
            &name,
            format!("BEFORE UPDATE ON {name} {}", abort()),
        );
        trigger(
            &mut objects,
            &format!("{suffix}_no_replace"),
            &name,
            format!(
                "BEFORE INSERT ON {name} WHEN EXISTS(SELECT 1 FROM {name} WHERE turn_id = NEW.turn_id AND ({sequence} = NEW.{sequence} OR {key} = NEW.{key})) {}",
                abort()
            ),
        );
    }
    for suffix in ["receipts", "events", "attempts"] {
        let name = format!("{PREFIX}{suffix}");
        trigger(
            &mut objects,
            &format!("{suffix}_delete_gate"),
            &name,
            format!(
                "BEFORE DELETE ON {name} WHEN NOT EXISTS(SELECT 1 FROM assistant_provider_streams WHERE turn_id = OLD.turn_id AND state = 'deleted') {}",
                abort()
            ),
        );
        trigger(
            &mut objects,
            &format!("{suffix}_insert_gate"),
            &name,
            format!(
                "BEFORE INSERT ON {name} WHEN NOT EXISTS(SELECT 1 FROM assistant_provider_streams WHERE turn_id = NEW.turn_id AND state != 'deleted') {}",
                abort()
            ),
        );
    }
    trigger(
        &mut objects,
        "epochs_insert",
        "assistant_provider_entity_epochs",
        format!(
            "BEFORE INSERT ON assistant_provider_entity_epochs WHEN NEW.mutation_epoch != 0 OR EXISTS(SELECT 1 FROM assistant_provider_entity_epochs WHERE entity_id = NEW.entity_id) {}",
            abort()
        ),
    );
    trigger(
        &mut objects,
        "epochs_increment",
        "assistant_provider_entity_epochs",
        format!(
            "BEFORE UPDATE ON assistant_provider_entity_epochs WHEN NEW.entity_id IS NOT OLD.entity_id OR OLD.mutation_epoch = 9223372036854775807 OR NEW.mutation_epoch != OLD.mutation_epoch + 1 {}",
            abort()
        ),
    );
    trigger(&mut objects, "watch_insert", "entities", "AFTER INSERT ON entities BEGIN UPDATE assistant_provider_entity_epochs SET mutation_epoch = mutation_epoch + 1, present = 0, retained_rowid = NULL WHERE present = 1 AND retained_rowid = NEW.rowid AND entity_id != NEW.id; UPDATE assistant_provider_entity_epochs SET mutation_epoch = mutation_epoch + 1, present = 1, retained_rowid = NEW.rowid WHERE entity_id = NEW.id; END".to_owned());
    trigger(&mut objects, "watch_update", "entities", "AFTER UPDATE ON entities BEGIN UPDATE assistant_provider_entity_epochs SET mutation_epoch = mutation_epoch + 1, present = 0, retained_rowid = NULL WHERE present = 1 AND retained_rowid = NEW.rowid AND entity_id != OLD.id AND entity_id != NEW.id; UPDATE assistant_provider_entity_epochs SET mutation_epoch = mutation_epoch + 1, present = CASE WHEN OLD.id = NEW.id THEN 1 ELSE 0 END, retained_rowid = CASE WHEN OLD.id = NEW.id THEN NEW.rowid ELSE NULL END WHERE entity_id = OLD.id; UPDATE assistant_provider_entity_epochs SET mutation_epoch = mutation_epoch + 1, present = 1, retained_rowid = NEW.rowid WHERE entity_id = NEW.id AND NEW.id != OLD.id; END".to_owned());
    trigger(&mut objects, "watch_delete", "entities", "AFTER DELETE ON entities BEGIN UPDATE assistant_provider_entity_epochs SET mutation_epoch = mutation_epoch + 1, present = 0, retained_rowid = NULL WHERE entity_id = OLD.id; END".to_owned());
    objects
}

pub fn manifest_sha256() -> String {
    let mut objects = manifest();
    objects.sort_unstable_by(|a, b| (a.kind, a.name.as_str()).cmp(&(b.kind, b.name.as_str())));
    let mut hash = Sha256::new();
    hash.update(b"nebula.assistant-provider-schema/v1\0");
    for object in objects {
        for part in [object.kind, &object.name, &object.table, &object.sql] {
            hash.update((part.len() as u64).to_be_bytes());
            hash.update(part.as_bytes());
        }
    }
    format!("{:x}", hash.finalize())
}

/// A read snapshot validates schema identity only. It never treats an intact schema as
/// proof of safe dispatch, a valid receipt chain, correct totals, or available host disk.
pub async fn inspect(connection: &mut SqliteConnection, limits: SchemaLimits) -> Result<Status> {
    let capacity = limits.capacity()?;
    let mut tx = connection.begin().await?;
    let result = inspect_inner(&mut tx, capacity).await?;
    tx.commit().await?;
    Ok(result)
}

/// Explicit installation on the existing owned writer connection, or an isolated migration
/// copy. The caller must arrange process cutover; a SQLite transaction does not stop a
/// legacy process from making later writes. Cancellation before commit submission rolls
/// back. Once commit is submitted, losing its acknowledgement leaves an unknown outcome:
/// the caller must inspect the database before assuming absence or retrying installation.
pub async fn install(
    connection: &mut SqliteConnection,
    identity: InstallIdentity,
    limits: SchemaLimits,
) -> Result<Identity> {
    let capacity = limits.capacity()?;
    if identity.database_uuid.is_nil() {
        return Err(Error::InvalidIdentity);
    }
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
    if !matches!(inspect_inner(&mut tx, capacity).await?, Status::Absent) {
        return Err(Error::AlreadyInstalled);
    }
    for object in manifest() {
        sqlx::query(&object.sql).execute(&mut *tx).await?;
    }
    let checksum = manifest_sha256();
    sqlx::query("INSERT INTO assistant_provider_schema(singleton, version, database_uuid, migration_sha256, installed_at_us) VALUES (1, 1, ?, ?, ?)")
        .bind(identity.database_uuid.hyphenated().to_string()).bind(&checksum).bind(identity.installed_at_us).execute(&mut *tx).await?;
    sqlx::query("INSERT INTO assistant_provider_budget(singleton, used_bytes, reserved_bytes) VALUES (1, 0, 0)").execute(&mut *tx).await?;
    let Status::Installed(installed) = inspect_inner(&mut tx, capacity).await? else {
        return Err(Error::PartialOrUnknownSchema);
    };
    tx.commit().await?;
    Ok(installed)
}

async fn recognized_base(connection: &mut SqliteConnection) -> Result<()> {
    let fk: i64 = sqlx::query_scalar("PRAGMA foreign_keys")
        .fetch_one(&mut *connection)
        .await?;
    if fk != 1 {
        return Err(Error::ForeignKeysDisabled);
    }
    let result: std::result::Result<bool, sqlx::Error> = async {
        let version: Option<i64> = sqlx::query_scalar("SELECT version FROM schema_versions ORDER BY version DESC LIMIT 1").fetch_optional(&mut *connection).await?;
        let revisions: Vec<String> = sqlx::query_scalar("SELECT CASE WHEN length(CAST(version_num AS BLOB)) <= 128 THEN version_num ELSE '' END FROM alembic_version LIMIT 2").fetch_all(&mut *connection).await?;
        sqlx::query("SELECT id, kind, engagement_id, revision, payload, chat_session_id, created_at, updated_at FROM entities LIMIT 0").execute(&mut *connection).await?;
        sqlx::query("SELECT rowid FROM entities LIMIT 0").execute(&mut *connection).await?;
        sqlx::query("SELECT id, project_id, resource_kind, resource_id, revision, label, description, breadcrumb, content, updated_at FROM search_documents LIMIT 0").execute(&mut *connection).await?;
        let columns: Vec<String> = sqlx::query_scalar("SELECT CASE WHEN length(CAST(name AS BLOB)) <= 128 THEN name ELSE '' END FROM pragma_index_info('ix_entities_kind_chat_session_created') ORDER BY seqno LIMIT 5").fetch_all(&mut *connection).await?;
        let base_tables: i64 = sqlx::query_scalar("SELECT count(*) FROM sqlite_schema WHERE type='table' AND name IN ('entities','search_documents','schema_versions','alembic_version')").fetch_one(&mut *connection).await?;
        let index_owner: Option<String> = sqlx::query_scalar("SELECT CASE WHEN length(CAST(tbl_name AS BLOB)) <= 128 THEN tbl_name ELSE '' END FROM sqlite_schema WHERE type='index' AND name='ix_entities_kind_chat_session_created' LIMIT 1").fetch_optional(&mut *connection).await?;
        let shadowed_rowid: i64 = sqlx::query_scalar("SELECT count(*) FROM pragma_table_xinfo('entities') WHERE lower(name)='rowid'").fetch_one(&mut *connection).await?;
        Ok(version == Some(5) && revisions == ["0016_chat_session_lookup"] && columns == ["kind", "chat_session_id", "created_at", "id"] && base_tables == 4 && index_owner.as_deref() == Some("entities") && shadowed_rowid == 0)
    }.await;
    match result {
        Ok(true) => Ok(()),
        Ok(false) | Err(sqlx::Error::ColumnDecode { .. }) | Err(sqlx::Error::Decode(_)) => {
            Err(Error::IncompatibleBase)
        }
        Err(sqlx::Error::Database(error)) if error.code().as_deref() == Some("1") => {
            Err(Error::IncompatibleBase)
        }
        Err(error) => Err(Error::Sql(error)),
    }
}

async fn inspect_inner(connection: &mut SqliteConnection, capacity: i64) -> Result<Status> {
    recognized_base(connection).await?;
    // Account metadata in SQLite before materializing SQL strings in Rust. The LIMIT also
    // bounds aggregate arithmetic even when the database contains an unknown namespace.
    let summary = sqlx::query("SELECT count(*) AS objects, coalesce(sum(length(CAST(name AS BLOB)) + length(CAST(tbl_name AS BLOB)) + length(type) + coalesce(length(CAST(sql AS BLOB)),0)),0) AS bytes FROM (SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE lower(substr(name,1,19)) = 'assistant_provider_' OR lower(substr(tbl_name,1,19)) = 'assistant_provider_' LIMIT 65)")
        .fetch_one(&mut *connection).await?;
    if summary.try_get::<i64, _>("objects")? > MAX_OBJECTS as i64
        || summary.try_get::<i64, _>("bytes")? > MAX_MANIFEST_BYTES as i64
    {
        return Err(Error::ManifestLimit);
    }
    // Bound each SQL/name before materializing it. SQLx's connection row buffer belongs to
    // the caller; even its default prefetch cannot exceed this fixed 65-object result.
    let rows = sqlx::query("SELECT type, CASE WHEN length(CAST(name AS BLOB)) <= 256 THEN name END AS name, CASE WHEN length(CAST(tbl_name AS BLOB)) <= 256 THEN tbl_name END AS tbl_name, CASE WHEN length(CAST(sql AS BLOB)) <= ? THEN sql END AS sql, sql IS NULL AS implicit FROM sqlite_schema WHERE lower(substr(name,1,19)) = 'assistant_provider_' OR lower(substr(tbl_name,1,19)) = 'assistant_provider_' ORDER BY type, name LIMIT 65")
        .bind(MAX_OBJECT_BYTES as i64).fetch_all(&mut *connection).await?;
    if rows.is_empty() {
        return Ok(Status::Absent);
    }
    if rows.len() > MAX_OBJECTS {
        return Err(Error::ManifestLimit);
    }
    let expected = manifest();
    let mut matched = 0;
    let mut bytes = 0usize;
    for row in rows {
        let kind: String = row.try_get("type")?;
        let name: Option<String> = row.try_get("name")?;
        let table: Option<String> = row.try_get("tbl_name")?;
        let Some((name, table)) = name.zip(table) else {
            return Err(Error::ManifestLimit);
        };
        let implicit: bool = row.try_get("implicit")?;
        if implicit {
            if kind != "index"
                || !name.starts_with(&format!("sqlite_autoindex_{table}_"))
                || !expected
                    .iter()
                    .any(|object| object.kind == "table" && object.name == table)
            {
                return Err(Error::PartialOrUnknownSchema);
            }
            continue;
        }
        let sql: Option<String> = row.try_get("sql")?;
        let Some(sql) = sql else {
            return Err(Error::ManifestLimit);
        };
        bytes = bytes
            .checked_add(sql.len() + name.len() + table.len() + kind.len())
            .ok_or(Error::ManifestLimit)?;
        if bytes > MAX_MANIFEST_BYTES {
            return Err(Error::ManifestLimit);
        }
        if !expected.iter().any(|object| {
            object.kind == kind && object.name == name && object.table == table && object.sql == sql
        }) {
            return Err(Error::PartialOrUnknownSchema);
        }
        matched += 1;
    }
    if matched != expected.len() {
        return Err(Error::PartialOrUnknownSchema);
    }
    let rows = sqlx::query("SELECT singleton, version, CASE WHEN length(CAST(database_uuid AS BLOB)) = 36 THEN database_uuid END AS database_uuid, CASE WHEN length(CAST(migration_sha256 AS BLOB)) = 64 THEN migration_sha256 END AS migration_sha256, installed_at_us FROM assistant_provider_schema LIMIT 2").fetch_all(&mut *connection).await?;
    if rows.len() != 1 {
        return Err(Error::InvalidIdentity);
    }
    let row = &rows[0];
    if row.try_get::<i64, _>("singleton")? != 1 {
        return Err(Error::InvalidIdentity);
    }
    if row.try_get::<i64, _>("version")? != 1 {
        return Err(Error::UnsupportedVersion);
    }
    let uuid: Option<String> = row.try_get("database_uuid")?;
    let hash: Option<String> = row.try_get("migration_sha256")?;
    let (Some(uuid), Some(hash)) = (uuid, hash) else {
        return Err(Error::InvalidIdentity);
    };
    let database_uuid = Uuid::parse_str(&uuid).map_err(|_| Error::InvalidIdentity)?;
    if database_uuid.is_nil()
        || database_uuid.hyphenated().to_string() != uuid
        || hash != manifest_sha256()
    {
        return Err(Error::InvalidIdentity);
    }
    let installed_at_us: i64 = row.try_get("installed_at_us")?;
    let budgets = sqlx::query(
        "SELECT singleton, used_bytes, reserved_bytes FROM assistant_provider_budget LIMIT 2",
    )
    .fetch_all(&mut *connection)
    .await?;
    if budgets.len() != 1 {
        return Err(Error::InvalidBudget);
    }
    let budget = &budgets[0];
    let singleton: i64 = budget.try_get("singleton")?;
    let used: i64 = budget.try_get("used_bytes")?;
    let reserved: i64 = budget.try_get("reserved_bytes")?;
    let total = used
        .checked_add(reserved)
        .filter(|_| singleton == 1 && used >= 0 && reserved >= 0)
        .ok_or(Error::InvalidBudget)?;
    if total > capacity {
        return Err(Error::CapacityExceeded);
    }
    Ok(Status::Installed(Identity {
        database_uuid,
        installed_at_us,
        migration_sha256: hash,
    }))
}
