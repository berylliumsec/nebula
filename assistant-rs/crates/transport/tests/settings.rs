use axum::{
    body::{Body, to_bytes},
    http::Request,
};
use chrono::{DateTime, Utc};
use nebula_assistant_storage::entities::{Config, SqliteAssistantStore};
use nebula_assistant_transport::{Authentication, HttpConfig, router};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection, sqlite::SqliteConnectOptions};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Mutex,
        atomic::{AtomicI64, AtomicUsize, Ordering},
    },
};
use tower::ServiceExt;

static CLOCK: AtomicI64 = AtomicI64::new(0);
static GENERATED_ID: Mutex<String> = Mutex::new(String::new());
static UUID_CALLS: AtomicUsize = AtomicUsize::new(0);
fn generated_id() -> String {
    UUID_CALLS.fetch_add(1, Ordering::SeqCst);
    GENERATED_ID.lock().unwrap().clone()
}
fn now() -> DateTime<Utc> {
    DateTime::from_timestamp_micros(CLOCK.load(Ordering::SeqCst)).unwrap()
}
fn sql_time(value: &Value) -> String {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}
async fn put(db: &mut SqliteConnection, row: &Value) {
    let p = &row["payload"];
    let kind = row["kind"].as_str().unwrap();
    let session = if [
        "chat_turns",
        "chat_schedules",
        "chat_messages",
        "chat_goals",
        "chat_queues",
        "chat_read_cursors",
        "chat_decisions",
        "chat_bookmarks",
    ]
    .contains(&kind)
    {
        p["session_id"].as_str()
    } else {
        p["chat_session_id"].as_str()
    };
    let raw = row
        .get("raw_payload")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| serde_json::to_string(p).unwrap());
    assert_eq!(serde_json::from_str::<Value>(&raw).unwrap(), *p);
    sqlx::query("INSERT INTO entities (id,kind,engagement_id,revision,chat_session_id,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(kind).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
        .bind(session).bind(raw).bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
        .execute(db).await.unwrap();
}
async fn entities(db: &mut SqliteConnection) -> BTreeMap<String, Value> {
    let rows: Vec<String> = sqlx::query_scalar("SELECT json_object('id',id,'kind',kind,'engagement_id',engagement_id,'revision',revision,'automation_run_id',automation_run_id,'automation_session_id',automation_session_id,'automation_status',automation_status,'automation_expires_at',automation_expires_at,'chat_session_id',chat_session_id,'payload',payload,'created_at',created_at,'updated_at',updated_at) FROM entities ORDER BY id")
        .fetch_all(db).await.unwrap();
    rows.into_iter()
        .map(|raw| {
            let row: Value = serde_json::from_str(&raw).unwrap();
            (row["id"].as_str().unwrap().to_owned(), row)
        })
        .collect()
}
fn record(row: &Value) -> Value {
    json!({"kind":row["kind"],"payload":serde_json::from_str::<Value>(row["payload"].as_str().unwrap()).unwrap()})
}
fn snapshot_digest(rows: &BTreeMap<String, Value>) -> String {
    nebula_assistant_domain::session_state::digest(&Value::Array(
        rows.values().map(record).collect(),
    ))
    .unwrap()
}
fn normalize(value: Value) -> Value {
    match value {
        Value::Object(fields) => fields
            .into_iter()
            .map(|(key, value)| {
                let value = if ["request_id", "error_id"].contains(&key.as_str()) {
                    json!("<generated>")
                } else {
                    normalize(value)
                };
                (key, value)
            })
            .collect(),
        Value::Array(values) => values.into_iter().map(normalize).collect(),
        value => value,
    }
}

fn quoted(name: &str) -> String {
    assert!(!name.is_empty() && name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_'));
    format!("\"{name}\"")
}
async fn insert_table(db: &mut SqliteConnection, table: &str, rows: &[Value]) {
    assert!(
        [
            "search_documents",
            "operation_events",
            "run_events",
            "resource_relations",
            "session_projections"
        ]
        .contains(&table)
    );
    for row in rows {
        let fields = row.as_object().unwrap();
        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({})",
            quoted(table),
            fields
                .keys()
                .map(|k| quoted(k))
                .collect::<Vec<_>>()
                .join(","),
            vec!["?"; fields.len()].join(",")
        );
        let mut query = sqlx::query(&sql);
        for value in fields.values() {
            query = match value {
                Value::Null => query.bind(None::<String>),
                Value::String(s) => query.bind(s),
                Value::Bool(v) => query.bind(v),
                Value::Number(n) if n.as_i64().is_some() => query.bind(n.as_i64().unwrap()),
                Value::Number(n) => query.bind(n.as_f64().unwrap()),
                _ => query.bind(serde_json::to_string(value).unwrap()),
            };
        }
        query.execute(&mut *db).await.unwrap();
    }
}
async fn table_rows(db: &mut SqliteConnection, table: &str) -> Vec<Value> {
    let columns: Vec<String> =
        sqlx::query_scalar("SELECT name FROM pragma_table_info(?) ORDER BY cid")
            .bind(table)
            .fetch_all(&mut *db)
            .await
            .unwrap();
    assert!(!columns.is_empty());
    let sql = format!(
        "SELECT json_object({}) FROM {} ORDER BY {}",
        columns
            .iter()
            .map(|c| format!("'{c}',{}", quoted(c)))
            .collect::<Vec<_>>()
            .join(","),
        quoted(table),
        quoted(&columns[0])
    );
    let rows: Vec<Value> = sqlx::query_scalar::<_, String>(&sql)
        .fetch_all(db)
        .await
        .unwrap()
        .into_iter()
        .map(|s| serde_json::from_str(&s).unwrap())
        .collect();
    rows
}
fn by_id(rows: Vec<Value>) -> BTreeMap<String, Value> {
    rows.into_iter()
        .map(|row| (row["id"].as_str().unwrap().to_owned(), row))
        .collect()
}
async fn protected(db: &mut SqliteConnection) -> BTreeMap<String, Vec<Value>> {
    let mut result = BTreeMap::new();
    for table in [
        "operation_events",
        "run_events",
        "resource_relations",
        "session_projections",
    ] {
        result.insert(table.into(), table_rows(db, table).await);
    }
    result
}
async fn install_fault(db: &mut SqliteConnection, case: &Value) {
    let Some(fault) = case.get("fault").filter(|v| !v.is_null()) else {
        return;
    };
    let target = match fault["kind"].as_str().unwrap() {
        "unarchive_conflict" => {
            assert_eq!(fault["count"], 3);
            fault["session_id"].as_str().unwrap()
        }
        "schedule_conflict" => fault["schedule_id"].as_str().unwrap(),
        kind => panic!("unimplemented fixture fault {kind}"),
    };
    assert!(
        target
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    );
    sqlx::query("CREATE TABLE fixture_conflict (id INTEGER PRIMARY KEY); INSERT INTO fixture_conflict VALUES(1)").execute(&mut *db).await.unwrap();
    // A real UNIQUE failure rolls the current write back, preserving earlier
    // commits. The production writer maps it to Conflict; no runtime failpoint.
    sqlx::query(&format!("CREATE TRIGGER fixture_settings_conflict BEFORE UPDATE ON entities WHEN OLD.id='{target}' BEGIN INSERT INTO fixture_conflict VALUES(1); END"))
        .execute(db).await.unwrap();
}
async fn remove_fault(db: &mut SqliteConnection, case: &Value) {
    if case.get("fault").is_some_and(|v| !v.is_null()) {
        sqlx::query("DROP TRIGGER fixture_settings_conflict; DROP TABLE fixture_conflict")
            .execute(db)
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn python_settings_http_oracle_preserves_mutations_search_and_partial_commits() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-settings.json")).unwrap();
    assert!(fixture.get("capture_pending").is_none());
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = SqliteConnection::connect_with(
        &SqliteConnectOptions::new()
            .filename(&path)
            .create_if_missing(true)
            .foreign_keys(true),
    )
    .await
    .unwrap();
    let base: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-storage.json")).unwrap();
    let mut installed = BTreeSet::new();
    for sql in base["schema_sql"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fixture["schema_sql"].as_array().unwrap())
    {
        let sql = sql.as_str().unwrap();
        if !installed.insert(sql) {
            continue;
        }
        sqlx::query(sql).execute(&mut db).await.unwrap();
    }
    sqlx::query("INSERT INTO schema_versions(version,applied_at) VALUES(5,'2020-01-01 00:00:00.000000'); INSERT INTO alembic_version VALUES('0016_chat_session_lookup')").execute(&mut db).await.unwrap();
    for field in ["projects", "initial_records", "dependency_records"] {
        for row in fixture[field].as_array().unwrap() {
            let mut row = row.clone();
            let id = row["payload"]["id"].as_str().unwrap();
            if let Some(raw) = fixture["raw_payloads"].get(id) {
                row["raw_payload"] = raw.clone();
            }
            put(&mut db, &row).await;
        }
    }
    insert_table(
        &mut db,
        "search_documents",
        fixture["initial_search_documents"].as_array().unwrap(),
    )
    .await;
    for (table, rows) in fixture["initial_protected_tables"].as_object().unwrap() {
        insert_table(&mut db, table, rows.as_array().unwrap()).await;
    }
    let initial_protected = protected(&mut db).await;
    let mut store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        if case["action"] == "reopen" {
            store.shutdown().await.unwrap();
            store = SqliteAssistantStore::open(&path, Config::default())
                .await
                .unwrap();
        }
        CLOCK.store(
            DateTime::parse_from_rfc3339(
                case.get("writer_clock")
                    .or_else(|| case.get("clock"))
                    .unwrap_or(&fixture["clock"])
                    .as_str()
                    .unwrap(),
            )
            .unwrap()
            .timestamp_micros(),
            Ordering::SeqCst,
        );
        *GENERATED_ID.lock().unwrap() = case["generated_ids"]
            .get(0)
            .and_then(Value::as_str)
            .unwrap_or("00000000-0000-4000-8000-000000000000")
            .into();
        UUID_CALLS.store(0, Ordering::SeqCst);
        install_fault(&mut db, case).await;
        let before = entities(&mut db).await;
        assert_eq!(
            snapshot_digest(&before),
            case["before_snapshot_sha256"],
            "before snapshot: {}",
            case["name"]
        );
        let before_search = by_id(table_rows(&mut db, "search_documents").await);
        assert_eq!(
            nebula_assistant_domain::session_state::digest(&json!(
                before_search.values().collect::<Vec<_>>()
            ))
            .unwrap(),
            case["before_search_sha256"],
            "before search hash: {}",
            case["name"]
        );
        let mut config = HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
        config.clock = now;
        config.new_schedule_id = generated_id;
        let mut request = Request::builder()
            .method(case["method"].as_str().unwrap())
            .uri(case["path"].as_str().unwrap())
            .header("host", "nebula.test:9443")
            .header("x-nebula-operation-id", "fixture-operation");
        if let Some(headers) = case
            .get("auth")
            .and_then(|v| v.get("headers"))
            .and_then(Value::as_object)
        {
            for (key, value) in headers {
                request = request.header(key, value.as_str().unwrap());
            }
        } else {
            request = request.header("authorization", "Bearer fixture-core");
        }
        let body = if case["method"] == "GET" {
            Body::empty()
        } else {
            request = request.header("content-type", "application/json");
            Body::from(serde_json::to_vec(&case["body"]).unwrap())
        };
        let response = router(store.clone(), config)
            .unwrap()
            .oneshot(request.body(body).unwrap())
            .await
            .unwrap();
        let status = response.status().as_u16();
        if let Some(expected) = case.get("expected_uuid_calls") {
            assert_eq!(
                json!(UUID_CALLS.load(Ordering::SeqCst)),
                *expected,
                "UUID guard timing: {}",
                case["name"]
            );
        }
        assert_eq!(
            json!(
                response
                    .headers()
                    .get("cache-control")
                    .map(|h| h.to_str().unwrap())
            ),
            case["expected_cache_control"],
            "cache: {}",
            case["name"]
        );
        let body: Value = serde_json::from_slice(
            &to_bytes(response.into_body(), 16 * 1024 * 1024)
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            json!({"status":status,"body":normalize(body)}),
            case["expected"],
            "response: {}",
            case["name"]
        );
        let after = entities(&mut db).await;
        assert_eq!(
            snapshot_digest(&after),
            case["after_snapshot_sha256"],
            "after snapshot: {}",
            case["name"]
        );
        let ids: BTreeSet<_> = before.keys().chain(after.keys()).cloned().collect();
        let changed: BTreeSet<_> = ids
            .into_iter()
            .filter(|id| before.get(id) != after.get(id))
            .collect();
        let expected: BTreeSet<_> = case["expected_changes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| {
                c["after"]["payload"]["id"]
                    .as_str()
                    .or_else(|| c["before"]["payload"]["id"].as_str())
                    .unwrap()
                    .to_owned()
            })
            .collect();
        assert_eq!(changed, expected, "changed identities: {}", case["name"]);
        for change in case["expected_changes"].as_array().unwrap() {
            let id = change["after"]["payload"]["id"]
                .as_str()
                .or_else(|| change["before"]["payload"]["id"].as_str())
                .unwrap();
            assert_eq!(
                before.get(id).map(record).unwrap_or(Value::Null),
                change["before"],
                "before {id}: {}",
                case["name"]
            );
            assert_eq!(
                after.get(id).map(record).unwrap_or(Value::Null),
                change["after"],
                "after {id}: {}",
                case["name"]
            );
            if let Some(saved) = after.get(id) {
                assert_eq!(saved["revision"], change["after"]["payload"]["revision"]);
                assert_eq!(
                    saved["updated_at"],
                    sql_time(&change["after"]["payload"]["updated_at"])
                );
                assert_eq!(
                    saved["created_at"],
                    sql_time(&change["after"]["payload"]["created_at"])
                );
                if let Some(prior) = before.get(id) {
                    for field in [
                        "id",
                        "kind",
                        "engagement_id",
                        "chat_session_id",
                        "created_at",
                        "automation_run_id",
                        "automation_session_id",
                        "automation_status",
                        "automation_expires_at",
                    ] {
                        assert_eq!(
                            prior[field], saved[field],
                            "envelope {field} {id}: {}",
                            case["name"]
                        );
                    }
                }
            }
        }
        let after_search = by_id(table_rows(&mut db, "search_documents").await);
        assert_eq!(
            nebula_assistant_domain::session_state::digest(&json!(
                after_search.values().collect::<Vec<_>>()
            ))
            .unwrap(),
            case["after_search_sha256"],
            "after search hash: {}",
            case["name"]
        );
        let search_ids: BTreeSet<_> = before_search
            .keys()
            .chain(after_search.keys())
            .cloned()
            .collect();
        let search_changed: BTreeSet<_> = search_ids
            .into_iter()
            .filter(|id| before_search.get(id) != after_search.get(id))
            .collect();
        let search_expected: BTreeSet<_> = case["expected_search_changes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| {
                c["after"]["id"]
                    .as_str()
                    .or_else(|| c["before"]["id"].as_str())
                    .unwrap()
                    .to_owned()
            })
            .collect();
        assert_eq!(
            search_changed, search_expected,
            "search identities: {}",
            case["name"]
        );
        for change in case["expected_search_changes"].as_array().unwrap() {
            let id = change["after"]["id"]
                .as_str()
                .or_else(|| change["before"]["id"].as_str())
                .unwrap();
            assert_eq!(
                before_search.get(id).unwrap_or(&Value::Null),
                &change["before"],
                "search before {id}: {}",
                case["name"]
            );
            assert_eq!(
                after_search.get(id).unwrap_or(&Value::Null),
                &change["after"],
                "search after {id}: {}",
                case["name"]
            );
        }
        assert_eq!(
            protected(&mut db).await,
            initial_protected,
            "protected tables: {}",
            case["name"]
        );
        assert_eq!(
            nebula_assistant_domain::session_state::digest(&json!(initial_protected)).unwrap(),
            case["after_protected_sha256"],
            "protected hash: {}",
            case["name"]
        );
        remove_fault(&mut db, case).await;
    }
    store.shutdown().await.unwrap();
    let before_reopen = entities(&mut db).await;
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(before_reopen, entities(&mut db).await);
    let expected: BTreeMap<_, _> = ["projects", "final_records", "final_dependencies"]
        .into_iter()
        .flat_map(|k| fixture[k].as_array().unwrap())
        .map(|row| {
            (
                row["payload"]["id"].as_str().unwrap().to_owned(),
                json!({"kind":row["kind"],"payload":row["payload"]}),
            )
        })
        .collect();
    assert_eq!(
        before_reopen
            .into_iter()
            .map(|(id, row)| (id, record(&row)))
            .collect::<BTreeMap<_, _>>(),
        expected
    );
    assert_eq!(
        by_id(table_rows(&mut db, "search_documents").await),
        by_id(
            fixture["final_search_documents"]
                .as_array()
                .unwrap()
                .clone()
        )
    );
    assert_eq!(protected(&mut db).await, initial_protected);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}
