#[allow(dead_code)]
mod support;

use nebula_assistant_storage::entities::provider_ledger::{
    Error, InstallIdentity, SchemaLimits, Status, inspect, install, manifest_sha256,
};
use serde_json::{Value, json};
use sqlx::{Connection, Row, SqliteConnection};
use support::{database, raw};
use uuid::Uuid;

const CAPACITY: u64 = 64 * 1024 * 1024;
const HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";
fn limits() -> SchemaLimits {
    SchemaLimits {
        capacity_bytes: CAPACITY,
    }
}
fn identity() -> InstallIdentity {
    InstallIdentity {
        database_uuid: Uuid::from_u128(0x810502378a4c4e97b3c860c76621897e),
        installed_at_us: 1_893_499_200_000_000,
    }
}
async fn fresh() -> (tempfile::TempDir, SqliteConnection) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("nebula.db");
    database(&path).await;
    let mut db = raw(&path).await;
    sqlx::query("PRAGMA foreign_keys=ON")
        .execute(&mut db)
        .await
        .unwrap();
    (dir, db)
}
async fn q(db: &mut SqliteConnection, sql: &str) {
    sqlx::query(sql).execute(db).await.unwrap();
}
async fn scalar(db: &mut SqliteConnection, sql: &str) -> i64 {
    sqlx::query_scalar(sql).fetch_one(db).await.unwrap()
}
async fn legacy(db: &mut SqliteConnection) -> Value {
    let mut tables = serde_json::Map::new();
    for (table, query) in [
        (
            "entities",
            "SELECT json_array(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) FROM entities ORDER BY id",
        ),
        (
            "search_documents",
            "SELECT json_array(id,project_id,resource_kind,resource_id,revision,label,description,breadcrumb,content,updated_at) FROM search_documents ORDER BY id",
        ),
        (
            "schema_versions",
            "SELECT json_array(version,applied_at) FROM schema_versions ORDER BY version",
        ),
        (
            "alembic_version",
            "SELECT json_array(version_num) FROM alembic_version ORDER BY version_num",
        ),
        (
            "schema",
            "SELECT json_array(type,name,tbl_name,sql) FROM sqlite_schema WHERE lower(substr(name,1,19)) != 'assistant_provider_' AND lower(substr(tbl_name,1,19)) != 'assistant_provider_' ORDER BY type,name",
        ),
    ] {
        let rows: Vec<String> = sqlx::query_scalar(query).fetch_all(&mut *db).await.unwrap();
        tables.insert(table.to_owned(), json!(rows));
    }
    for pragma in [
        "application_id",
        "user_version",
        "journal_mode",
        "synchronous",
        "foreign_keys",
    ] {
        let row = sqlx::query(&format!("PRAGMA {pragma}"))
            .fetch_one(&mut *db)
            .await
            .unwrap();
        let value = if pragma == "journal_mode" {
            json!(row.get::<String, _>(0))
        } else {
            json!(row.get::<i64, _>(0))
        };
        tables.insert(pragma.to_owned(), value);
    }
    Value::Object(tables)
}
async fn owned_schema(db: &mut SqliteConnection) -> Vec<String> {
    sqlx::query_scalar("SELECT json_array(type,name,tbl_name,sql) FROM sqlite_schema WHERE lower(substr(name,1,19)) = 'assistant_provider_' OR lower(substr(tbl_name,1,19)) = 'assistant_provider_' ORDER BY type,name").fetch_all(db).await.unwrap()
}
async fn trigger_sql(db: &mut SqliteConnection, name: &str) -> String {
    sqlx::query_scalar("SELECT sql FROM sqlite_schema WHERE name=?")
        .bind(name)
        .fetch_one(db)
        .await
        .unwrap()
}
async fn stream(db: &mut SqliteConnection, turn: &str) {
    sqlx::query("INSERT INTO assistant_provider_streams(turn_id,stream_id,session_id,project_id,state,event_bytes,reserved_bytes,receipt_head_sha256,observed_turn_epoch,created_at_us,updated_at_us) VALUES(?,?, 'session','project','active',0,0,?,0,1,1)")
        .bind(turn).bind(format!("stream-{turn}")).bind(HASH).execute(db).await.unwrap();
}
async fn attempt(db: &mut SqliteConnection, turn: &str, id: &str) {
    sqlx::query("INSERT INTO assistant_provider_attempts(attempt_id,turn_id,ordinal,owner_id,claim_id,request_sha256,route_sha256,phase) VALUES(?,?,1,'owner',?,?,?,'admitted')")
        .bind(id).bind(turn).bind(format!("claim-{id}")).bind(HASH).bind(HASH).execute(db).await.unwrap();
}
async fn receipt(db: &mut SqliteConnection, turn: &str) {
    sqlx::query("INSERT INTO assistant_provider_receipts(turn_id,receipt_sequence,kind,command_key,command_sha256,receipt_json,previous_sha256,receipt_sha256,committed_at_us) VALUES(?,1,'admitted','admission',?,'{}',?,?,1)")
        .bind(turn).bind(HASH).bind(HASH).bind(HASH).execute(db).await.unwrap();
}
async fn event(db: &mut SqliteConnection, turn: &str) {
    sqlx::query("INSERT INTO assistant_provider_events(turn_id,sequence,event_type,event_key,event_json,sse_bytes,sse_bytes_len,content_sha256,previous_sha256,event_sha256,committed_at_us) VALUES(?,1,'accepted','accept','{}',x'78',1,?,?,?,1)")
        .bind(turn).bind(HASH).bind(HASH).bind(HASH).execute(db).await.unwrap();
}
async fn epoch(db: &mut SqliteConnection, id: &str) -> (i64, i64) {
    sqlx::query_as(
        "SELECT mutation_epoch,present FROM assistant_provider_entity_epochs WHERE entity_id=?",
    )
    .bind(id)
    .fetch_one(db)
    .await
    .unwrap()
}
async fn watch_existing(db: &mut SqliteConnection, id: &str) {
    let result = sqlx::query("INSERT INTO assistant_provider_entity_epochs(entity_id,present,retained_rowid) SELECT id,1,rowid FROM entities WHERE id=?")
        .bind(id).execute(db).await.unwrap();
    assert_eq!(result.rows_affected(), 1);
}
async fn locator(db: &mut SqliteConnection, id: &str) -> Option<i64> {
    sqlx::query_scalar(
        "SELECT retained_rowid FROM assistant_provider_entity_epochs WHERE entity_id=?",
    )
    .bind(id)
    .fetch_one(db)
    .await
    .unwrap()
}

#[tokio::test]
async fn provider_ledger_install_reopen_preserves_legacy_rows_and_markers() {
    let (dir, mut db) = fresh().await;
    // Nonzero preexisting pragma markers and opaque whitespace survive installation.
    q(&mut db, "PRAGMA application_id=1234").await;
    q(&mut db, "PRAGMA user_version=987").await;
    q(
        &mut db,
        "UPDATE entities SET payload=payload || '  ' WHERE id='fixture-chat_sessions'",
    )
    .await;
    let before = legacy(&mut db).await;
    assert_eq!(inspect(&mut db, limits()).await.unwrap(), Status::Absent);
    let installed = install(&mut db, identity(), limits()).await.unwrap();
    assert_eq!(installed.database_uuid, identity().database_uuid);
    assert_eq!(installed.installed_at_us, identity().installed_at_us);
    assert_eq!(installed.migration_sha256, manifest_sha256());
    assert_eq!(installed.migration_sha256.len(), 64);
    assert_eq!(legacy(&mut db).await, before);
    let schema = owned_schema(&mut db).await;
    assert!(matches!(
        install(&mut db, identity(), limits()).await,
        Err(Error::AlreadyInstalled)
    ));
    assert_eq!(owned_schema(&mut db).await, schema);
    db.close().await.unwrap();
    let mut reopened = raw(&dir.path().join("nebula.db")).await;
    assert_eq!(
        inspect(&mut reopened, limits()).await.unwrap(),
        Status::Installed(installed)
    );
    assert_eq!(owned_schema(&mut reopened).await, schema);
    assert_eq!(legacy(&mut reopened).await, before);
    assert_eq!(
        scalar(
            &mut reopened,
            "SELECT count(*) FROM assistant_provider_entity_epochs"
        )
        .await,
        0
    );
    assert_eq!(
        scalar(
            &mut reopened,
            "SELECT used_bytes+reserved_bytes FROM assistant_provider_budget"
        )
        .await,
        0
    );
}

#[tokio::test]
async fn provider_ledger_refuses_partial_unknown_or_changed_schema_without_writes() {
    for mode in [
        "partial",
        "uppercase-prefix",
        "missing-trigger",
        "extra-index",
        "changed-index",
        "checksum",
        "future",
        "oversized-ddl",
        "many-objects",
        "base",
        "foreign-keys",
    ] {
        let (_dir, mut db) = fresh().await;
        if matches!(
            mode,
            "missing-trigger" | "extra-index" | "changed-index" | "checksum" | "future"
        ) {
            install(&mut db, identity(), limits()).await.unwrap();
        }
        match mode {
            "partial" => q(&mut db, "CREATE TABLE assistant_provider_unknown(x)").await,
            "uppercase-prefix" => q(&mut db, "CREATE TABLE ASSISTANT_PROVIDER_unknown(x)").await,
            "missing-trigger" => q(&mut db, "DROP TRIGGER assistant_provider_watch_update").await,
            "extra-index" => {
                q(
                    &mut db,
                    "CREATE INDEX unrelated_name ON assistant_provider_events(event_type)",
                )
                .await
            }
            "changed-index" => {
                q(&mut db, "DROP INDEX assistant_provider_streams_recovery").await;
                q(&mut db, "CREATE INDEX assistant_provider_streams_recovery ON assistant_provider_streams(state,turn_id)").await;
            }
            "checksum" | "future" => {
                let ddl = trigger_sql(&mut db, "assistant_provider_schema_no_update").await;
                q(&mut db, "DROP TRIGGER assistant_provider_schema_no_update").await;
                if mode == "checksum" {
                    q(
                        &mut db,
                        "UPDATE assistant_provider_schema SET migration_sha256=printf('%064d',1)",
                    )
                    .await;
                } else {
                    q(&mut db, "PRAGMA ignore_check_constraints=ON").await;
                    q(&mut db, "UPDATE assistant_provider_schema SET version=2").await;
                    q(&mut db, "PRAGMA ignore_check_constraints=OFF").await;
                }
                q(&mut db, &ddl).await;
            }
            "oversized-ddl" => {
                q(
                    &mut db,
                    &format!(
                        "CREATE VIEW assistant_provider_unknown AS SELECT '{}' AS value",
                        "x".repeat(70 * 1024)
                    ),
                )
                .await
            }
            "many-objects" => {
                for index in 0..65 {
                    q(
                        &mut db,
                        &format!("CREATE TABLE assistant_provider_extra_{index}(x)"),
                    )
                    .await;
                }
            }
            "base" => q(&mut db, "UPDATE alembic_version SET version_num='future'").await,
            "foreign-keys" => q(&mut db, "PRAGMA foreign_keys=OFF").await,
            _ => unreachable!(),
        }
        let before = legacy(&mut db).await;
        let schema = owned_schema(&mut db).await;
        let result = inspect(&mut db, limits()).await;
        assert!(result.is_err(), "{mode}");
        match mode {
            "checksum" => assert!(matches!(result, Err(Error::InvalidIdentity))),
            "future" => assert!(matches!(result, Err(Error::UnsupportedVersion))),
            "oversized-ddl" | "many-objects" => {
                assert!(matches!(result, Err(Error::ManifestLimit)))
            }
            "base" => assert!(matches!(result, Err(Error::IncompatibleBase))),
            "foreign-keys" => assert!(matches!(result, Err(Error::ForeignKeysDisabled))),
            _ => assert!(matches!(result, Err(Error::PartialOrUnknownSchema))),
        }
        assert!(
            install(&mut db, identity(), limits()).await.is_err(),
            "{mode}"
        );
        assert_eq!(owned_schema(&mut db).await, schema, "{mode}");
        assert_eq!(legacy(&mut db).await, before, "{mode}");
    }
    let (_dir, mut db) = fresh().await;
    for capacity_bytes in [0, u64::MAX] {
        assert!(matches!(
            install(&mut db, identity(), SchemaLimits { capacity_bytes }).await,
            Err(Error::InvalidLimits)
        ));
    }
    assert!(matches!(
        install(
            &mut db,
            InstallIdentity {
                database_uuid: Uuid::nil(),
                installed_at_us: 1
            },
            limits()
        )
        .await,
        Err(Error::InvalidIdentity)
    ));
    assert!(owned_schema(&mut db).await.is_empty());
}

#[tokio::test]
async fn provider_ledger_epochs_detect_update_restore_delete_reinsert_and_rollback() {
    let (_dir, mut db) = fresh().await;
    q(&mut db, "PRAGMA recursive_triggers=OFF").await;
    install(&mut db, identity(), limits()).await.unwrap();
    q(&mut db, "INSERT INTO assistant_provider_entity_epochs(entity_id,present) VALUES('watched',0),('renamed',0)").await;
    let insert = "INSERT INTO entities(id,kind,revision,payload,created_at,updated_at) VALUES('watched','opaque',1,'{\"keep\":\"  original  \"}','2020-01-01','2020-01-01')";
    q(&mut db, insert).await;
    assert_eq!(epoch(&mut db, "watched").await, (1, 1));
    q(
        &mut db,
        "UPDATE entities SET payload='{\"changed\":true}' WHERE id='watched'",
    )
    .await;
    q(
        &mut db,
        "UPDATE entities SET payload='{\"keep\":\"  original  \"}' WHERE id='watched'",
    )
    .await;
    assert_eq!(epoch(&mut db, "watched").await, (3, 1));
    let mut tx = db.begin().await.unwrap();
    q(&mut tx, "DELETE FROM entities WHERE id='watched'").await;
    assert_eq!(epoch(&mut tx, "watched").await, (4, 0));
    tx.rollback().await.unwrap();
    assert_eq!(epoch(&mut db, "watched").await, (3, 1));
    q(
        &mut db,
        "UPDATE entities SET id='renamed' WHERE id='watched'",
    )
    .await;
    assert_eq!(epoch(&mut db, "watched").await, (4, 0));
    assert_eq!(epoch(&mut db, "renamed").await, (1, 1));
    q(&mut db, "DELETE FROM entities WHERE id='renamed'").await;
    assert_eq!(epoch(&mut db, "renamed").await, (2, 0));
    q(&mut db, insert).await;
    assert_eq!(epoch(&mut db, "watched").await, (5, 1));
    q(
        &mut db,
        "UPDATE entities SET revision=revision WHERE id='watched'",
    )
    .await;
    assert_eq!(epoch(&mut db, "watched").await, (6, 1));
    q(
        &mut db,
        "UPDATE entities SET revision=revision WHERE id='fixture-chat_sessions'",
    )
    .await;
    assert_eq!(
        scalar(
            &mut db,
            "SELECT count(*) FROM assistant_provider_entity_epochs"
        )
        .await,
        2
    );
    for sql in [
        "DELETE FROM assistant_provider_entity_epochs WHERE entity_id='watched'",
        "UPDATE assistant_provider_entity_epochs SET mutation_epoch=0 WHERE entity_id='watched'",
        "INSERT OR REPLACE INTO assistant_provider_entity_epochs(entity_id,present) VALUES('watched',1)",
    ] {
        assert!(sqlx::query(sql).execute(&mut db).await.is_err());
    }
    // Reach the signed boundary without billions of updates, then restore the exact guard.
    let guard = trigger_sql(&mut db, "assistant_provider_epochs_increment").await;
    q(&mut db, "DROP TRIGGER assistant_provider_epochs_increment").await;
    q(&mut db, "UPDATE assistant_provider_entity_epochs SET mutation_epoch=9223372036854775807 WHERE entity_id='watched'").await;
    q(&mut db, &guard).await;
    let before: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='watched'")
        .fetch_one(&mut db)
        .await
        .unwrap();
    assert!(
        sqlx::query("UPDATE entities SET payload='{}' WHERE id='watched'")
            .execute(&mut db)
            .await
            .is_err()
    );
    assert_eq!(epoch(&mut db, "watched").await, (i64::MAX, 1));
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT payload FROM entities WHERE id='watched'")
            .fetch_one(&mut db)
            .await
            .unwrap(),
        before
    );
    // REPLACE may suppress victim DELETE triggers. Track the actual AFTER rowid,
    // including two displaced identities and SQLite's negative-rowid boundary.
    q(&mut db, "INSERT INTO entities(rowid,id,kind,revision,payload,created_at,updated_at) VALUES(1000,'insert-victim','opaque',1,'{}','2020-01-01','2020-01-01')").await;
    watch_existing(&mut db, "insert-victim").await;
    q(&mut db, "INSERT OR REPLACE INTO entities(rowid,id,kind,revision,payload,created_at,updated_at) VALUES(1000,'insert-replacement','opaque',1,'{}','2020-01-01','2020-01-01')").await;
    assert_eq!(epoch(&mut db, "insert-victim").await, (1, 0));
    assert_eq!(locator(&mut db, "insert-victim").await, None);
    q(&mut db, "INSERT INTO entities(rowid,id,kind,revision,payload,created_at,updated_at) VALUES(1001,'moving','opaque',1,'{}','2020-01-01','2020-01-01'),(1002,'update-victim','opaque',1,'{}','2020-01-01','2020-01-01')").await;
    watch_existing(&mut db, "moving").await;
    watch_existing(&mut db, "update-victim").await;
    q(
        &mut db,
        "UPDATE OR REPLACE entities SET rowid=1002 WHERE id='moving'",
    )
    .await;
    assert_eq!(epoch(&mut db, "update-victim").await, (1, 0));
    assert_eq!(locator(&mut db, "update-victim").await, None);
    assert_eq!(epoch(&mut db, "moving").await, (1, 1));
    assert_eq!(locator(&mut db, "moving").await, Some(1002));
    q(&mut db, "INSERT INTO entities(rowid,id,kind,revision,payload,created_at,updated_at) VALUES(1003,'logical-victim','opaque',1,'{}','2020-01-01','2020-01-01'),(1004,'physical-victim','opaque',1,'{}','2020-01-01','2020-01-01'),(1005,'rename-source','opaque',1,'{}','2020-01-01','2020-01-01')").await;
    for id in ["logical-victim", "physical-victim", "rename-source"] {
        watch_existing(&mut db, id).await;
    }
    q(
        &mut db,
        "UPDATE OR REPLACE entities SET id='logical-victim',rowid=1004 WHERE id='rename-source'",
    )
    .await;
    assert_eq!(epoch(&mut db, "logical-victim").await, (1, 1));
    assert_eq!(locator(&mut db, "logical-victim").await, Some(1004));
    for id in ["physical-victim", "rename-source"] {
        assert_eq!(epoch(&mut db, id).await, (1, 0));
        assert_eq!(locator(&mut db, id).await, None);
    }
    q(&mut db, "INSERT INTO entities(rowid,id,kind,revision,payload,created_at,updated_at) VALUES(-1,'negative-victim','opaque',1,'{}','2020-01-01','2020-01-01')").await;
    watch_existing(&mut db, "negative-victim").await;
    q(&mut db, "INSERT INTO entities(id,kind,revision,payload,created_at,updated_at) VALUES('automatic-rowid','opaque',1,'{}','2020-01-01','2020-01-01')").await;
    assert_eq!(epoch(&mut db, "negative-victim").await, (0, 1));
    assert_eq!(locator(&mut db, "negative-victim").await, Some(-1));
    q(&mut db, "INSERT OR REPLACE INTO entities(rowid,id,kind,revision,payload,created_at,updated_at) VALUES(-1,'negative-replacement','opaque',1,'{}','2020-01-01','2020-01-01')").await;
    assert_eq!(epoch(&mut db, "negative-victim").await, (1, 0));
    assert_eq!(locator(&mut db, "negative-victim").await, None);
    q(&mut db, "DROP TRIGGER assistant_provider_epochs_increment").await;
    q(&mut db, "UPDATE assistant_provider_entity_epochs SET mutation_epoch=9223372036854775807 WHERE entity_id='moving'").await;
    q(&mut db, &guard).await;
    assert!(sqlx::query("INSERT OR REPLACE INTO entities(rowid,id,kind,revision,payload,created_at,updated_at) VALUES(1002,'overflow-replacement','opaque',1,'{}','2020-01-01','2020-01-01')").execute(&mut db).await.is_err());
    assert_eq!(epoch(&mut db, "moving").await, (i64::MAX, 1));
    assert_eq!(locator(&mut db, "moving").await, Some(1002));
    assert_eq!(
        scalar(
            &mut db,
            "SELECT count(*) FROM entities WHERE rowid=1002 AND id='moving'"
        )
        .await,
        1
    );
    assert_eq!(
        scalar(
            &mut db,
            "SELECT count(*) FROM entities WHERE id='overflow-replacement'"
        )
        .await,
        0
    );
    let location_mismatches: i64 = sqlx::query_scalar("SELECT count(*) FROM assistant_provider_entity_epochs AS w LEFT JOIN entities AS e ON e.id=w.entity_id WHERE (w.present=1 AND (e.id IS NULL OR e.rowid != w.retained_rowid)) OR (w.present=0 AND w.retained_rowid IS NOT NULL)")
        .fetch_one(&mut db).await.unwrap();
    assert_eq!(location_mismatches, 0);
    assert!(matches!(
        inspect(&mut db, limits()).await.unwrap(),
        Status::Installed(_)
    ));
}

#[tokio::test]
async fn provider_ledger_constraints_preserve_immutable_evidence_and_budget_bounds() {
    let (_dir, mut db) = fresh().await;
    q(&mut db, "PRAGMA recursive_triggers=OFF").await;
    install(&mut db, identity(), limits()).await.unwrap();
    stream(&mut db, "turn").await;
    stream(&mut db, "other").await;
    attempt(&mut db, "turn", "attempt").await;
    attempt(&mut db, "other", "foreign-attempt").await;
    receipt(&mut db, "turn").await;
    event(&mut db, "turn").await;
    q(
        &mut db,
        "INSERT INTO assistant_provider_entity_epochs(entity_id,present) VALUES('watch',0)",
    )
    .await;
    watch_existing(&mut db, "fixture-chat_sessions").await;
    // A hidden rowid is a second replacement identity. Fresh logical keys must not let
    // REPLACE delete a victim while recursive DELETE triggers are disabled.
    for suffix in [
        "schema",
        "budget",
        "streams",
        "attempts",
        "receipts",
        "events",
        "entity_epochs",
    ] {
        let table = format!("assistant_provider_{suffix}");
        let columns: Vec<String> =
            sqlx::query_scalar("SELECT name FROM pragma_table_info(?) ORDER BY cid")
                .bind(&table)
                .fetch_all(&mut db)
                .await
                .unwrap();
        let projection = columns
            .iter()
            .map(|column| match (suffix, column.as_str()) {
                ("streams", "turn_id") => "'replacement-turn'".to_owned(),
                ("streams", "stream_id") => "'replacement-stream'".to_owned(),
                ("attempts", "attempt_id") => "'replacement-attempt'".to_owned(),
                ("attempts", "claim_id") => "'replacement-claim'".to_owned(),
                ("attempts", "ordinal")
                | ("receipts", "receipt_sequence")
                | ("events", "sequence") => "2".to_owned(),
                ("receipts", "command_key")
                | ("events", "event_key")
                | ("entity_epochs", "entity_id") => "'replacement-key'".to_owned(),
                _ => format!("\"{column}\""),
            })
            .collect::<Vec<_>>()
            .join(",");
        let snapshot_columns = columns
            .iter()
            .map(|column| {
                if column == "sse_bytes" {
                    "hex(sse_bytes)".to_owned()
                } else {
                    format!("\"{column}\"")
                }
            })
            .collect::<Vec<_>>()
            .join(",");
        let snapshot = format!("SELECT json_array({snapshot_columns}) FROM {table} ORDER BY 1");
        let before: Vec<String> = sqlx::query_scalar(&snapshot)
            .fetch_all(&mut db)
            .await
            .unwrap();
        let query = format!(
            "INSERT OR REPLACE INTO {table}(rowid,{}) SELECT 1,{projection} FROM {table} LIMIT 1",
            columns.join(",")
        );
        let error = sqlx::query(&query).execute(&mut db).await.unwrap_err();
        assert!(
            error.to_string().contains("no column named rowid"),
            "{suffix}: {error}"
        );
        let after: Vec<String> = sqlx::query_scalar(&snapshot)
            .fetch_all(&mut db)
            .await
            .unwrap();
        assert_eq!(before, after, "{suffix}");
    }
    let legacy_before = legacy(&mut db).await;
    for sql in [
        "UPDATE assistant_provider_schema SET installed_at_us=2",
        "DELETE FROM assistant_provider_schema",
        "INSERT OR REPLACE INTO assistant_provider_schema SELECT * FROM assistant_provider_schema",
        "DELETE FROM assistant_provider_budget",
        "INSERT OR REPLACE INTO assistant_provider_budget VALUES(1,0,0)",
        "UPDATE assistant_provider_streams SET stream_id='changed' WHERE turn_id='turn'",
        "INSERT OR REPLACE INTO assistant_provider_streams SELECT * FROM assistant_provider_streams WHERE turn_id='turn'",
        "DELETE FROM assistant_provider_streams WHERE turn_id='turn'",
        "UPDATE assistant_provider_attempts SET claim_id='changed' WHERE attempt_id='attempt'",
        "INSERT OR REPLACE INTO assistant_provider_attempts SELECT * FROM assistant_provider_attempts WHERE attempt_id='attempt'",
        "DELETE FROM assistant_provider_attempts WHERE attempt_id='attempt'",
        "UPDATE assistant_provider_receipts SET receipt_json='{}'",
        "INSERT OR REPLACE INTO assistant_provider_receipts SELECT * FROM assistant_provider_receipts",
        "DELETE FROM assistant_provider_receipts",
        "UPDATE assistant_provider_events SET event_json='{}'",
        "INSERT OR REPLACE INTO assistant_provider_events SELECT * FROM assistant_provider_events",
        "DELETE FROM assistant_provider_events",
        "UPDATE assistant_provider_budget SET used_bytes=-1",
        "UPDATE assistant_provider_budget SET used_bytes=9223372036854775807,reserved_bytes=1",
        "UPDATE assistant_provider_budget SET used_bytes=9223372036854775807+1",
        "UPDATE assistant_provider_streams SET last_sequence=9007199254740992 WHERE turn_id='turn'",
        "UPDATE assistant_provider_streams SET receipt_head_sequence=9223372036854775807+1 WHERE turn_id='turn'",
        "UPDATE assistant_provider_streams SET project_id=char(0) || 'hidden' WHERE turn_id='turn'",
        "INSERT INTO assistant_provider_entity_epochs(entity_id,present) VALUES('missing-locator',1)",
        "INSERT INTO assistant_provider_entity_epochs(entity_id,present,retained_rowid) VALUES('absent-locator',0,123)",
        "INSERT INTO assistant_provider_entity_epochs(entity_id,present,retained_rowid) SELECT 'duplicate-locator',1,retained_rowid FROM assistant_provider_entity_epochs WHERE entity_id='fixture-chat_sessions'",
    ] {
        assert!(sqlx::query(sql).execute(&mut db).await.is_err(), "{sql}");
    }
    assert_eq!(
        scalar(&mut db, "SELECT count(*) FROM assistant_provider_events").await,
        1
    );
    assert_eq!(
        scalar(&mut db, "SELECT count(*) FROM assistant_provider_receipts").await,
        1
    );
    // New rows (not duplicate guards) exercise validation and same-Turn attempt binding.
    let event_insert = "INSERT INTO assistant_provider_events(turn_id,sequence,attempt_id,event_type,event_key,event_json,sse_bytes,sse_bytes_len,content_sha256,previous_sha256,event_sha256,committed_at_us) VALUES('turn',2,?,'delta','new',?,?,?, ?,?, ?,1)";
    for (attempt, json, blob, len, hash) in [
        (
            Some("foreign-attempt"),
            "{}".to_owned(),
            vec![b'x'],
            1,
            HASH.to_owned(),
        ),
        (None, "not json".to_owned(), vec![b'x'], 1, HASH.to_owned()),
        (
            None,
            "{}\0ignored".to_owned(),
            vec![b'x'],
            1,
            HASH.to_owned(),
        ),
        (None, "{}".to_owned(), vec![b'x'], 2, HASH.to_owned()),
        (None, "{}".to_owned(), vec![b'x'], 1, "g".repeat(64)),
        (
            None,
            "{}".to_owned(),
            vec![b'x'],
            1,
            format!("{}\0{}", "0".repeat(32), "0".repeat(31)),
        ),
        (
            None,
            format!("\"{}\"", "x".repeat(1024 * 1024)),
            vec![b'x'],
            1,
            HASH.to_owned(),
        ),
        (
            None,
            "{}".to_owned(),
            vec![b'x'; 1024 * 1024 + 1],
            1024 * 1024 + 1,
            HASH.to_owned(),
        ),
    ] {
        assert!(
            sqlx::query(event_insert)
                .bind(attempt)
                .bind(json)
                .bind(blob)
                .bind(len)
                .bind(hash)
                .bind(HASH)
                .bind(HASH)
                .execute(&mut db)
                .await
                .is_err()
        );
    }
    q(
        &mut db,
        &format!(
            "UPDATE assistant_provider_budget SET used_bytes={},reserved_bytes=1",
            CAPACITY
        ),
    )
    .await;
    assert!(matches!(
        inspect(&mut db, limits()).await,
        Err(Error::CapacityExceeded)
    ));
    assert!(matches!(
        inspect(
            &mut db,
            SchemaLimits {
                capacity_bytes: CAPACITY + 1
            }
        )
        .await
        .unwrap(),
        Status::Installed(_)
    ));
    q(
        &mut db,
        "UPDATE assistant_provider_budget SET used_bytes=0,reserved_bytes=0",
    )
    .await;
    q(
        &mut db,
        "UPDATE assistant_provider_streams SET state='deleted' WHERE turn_id='turn'",
    )
    .await;
    assert!(
        sqlx::query("UPDATE assistant_provider_streams SET state='active' WHERE turn_id='turn'")
            .execute(&mut db)
            .await
            .is_err()
    );
    q(
        &mut db,
        "DELETE FROM assistant_provider_events WHERE turn_id='turn'",
    )
    .await;
    q(
        &mut db,
        "DELETE FROM assistant_provider_receipts WHERE turn_id='turn'",
    )
    .await;
    q(
        &mut db,
        "DELETE FROM assistant_provider_attempts WHERE turn_id='turn'",
    )
    .await;
    assert!(sqlx::query("INSERT INTO assistant_provider_attempts SELECT 'new',turn_id,2,NULL,owner_id,'new',request_sha256,route_sha256,phase FROM assistant_provider_attempts WHERE attempt_id='foreign-attempt'").execute(&mut db).await.is_ok());
    let late = sqlx::query("INSERT INTO assistant_provider_receipts(turn_id,receipt_sequence,kind,command_key,command_sha256,receipt_json,previous_sha256,receipt_sha256,committed_at_us) VALUES('turn',2,'admitted','late',?,'{}',?,?,1)").bind(HASH).bind(HASH).bind(HASH).execute(&mut db).await;
    assert!(late.is_err());
    assert_eq!(scalar(&mut db, "SELECT count(*) FROM assistant_provider_streams WHERE turn_id='turn' AND state='deleted'").await, 1);
    assert_eq!(legacy(&mut db).await, legacy_before);
    assert!(matches!(
        inspect(&mut db, limits()).await.unwrap(),
        Status::Installed(_)
    ));
}
