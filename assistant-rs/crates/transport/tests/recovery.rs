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
    sync::atomic::{AtomicI64, Ordering},
};
use tower::ServiceExt;

static CLOCK: AtomicI64 = AtomicI64::new(0);
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
    let session = if kind == "chat_turns" {
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
async fn projections(db: &mut SqliteConnection) -> (Vec<String>, Vec<String>) {
    let watermarks = sqlx::query_scalar("SELECT json_object('session_id',session_id,'revision',revision,'digest',digest) FROM session_projections ORDER BY session_id")
        .fetch_all(&mut *db).await.unwrap();
    let search = sqlx::query_scalar("SELECT json_object('id',id,'project_id',project_id,'resource_kind',resource_kind,'resource_id',resource_id,'revision',revision,'label',label,'description',description,'breadcrumb',breadcrumb,'content',content,'updated_at',updated_at) FROM search_documents ORDER BY id")
        .fetch_all(db).await.unwrap();
    (watermarks, search)
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

#[tokio::test]
async fn python_recovery_http_oracle_preserves_ordered_commits_receipts_and_uncertainty() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-recovery.json")).unwrap();
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
    let schema: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-storage.json")).unwrap();
    for sql in schema["schema_sql"].as_array().unwrap() {
        sqlx::query(sql.as_str().unwrap())
            .execute(&mut db)
            .await
            .unwrap();
    }
    sqlx::query(
        "INSERT INTO schema_versions(version,applied_at) VALUES(5,'2020-01-01 00:00:00.000000')",
    )
    .execute(&mut db)
    .await
    .unwrap();
    sqlx::query("INSERT INTO alembic_version VALUES('0016_chat_session_lookup')")
        .execute(&mut db)
        .await
        .unwrap();
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
    CLOCK.store(
        DateTime::parse_from_rfc3339(fixture["clock"].as_str().unwrap())
            .unwrap()
            .timestamp_micros(),
        Ordering::SeqCst,
    );
    let mut store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        if case["action"] == "reopen" {
            let before = entities(&mut db).await;
            store.shutdown().await.unwrap();
            store = SqliteAssistantStore::open(&path, Config::default())
                .await
                .unwrap();
            assert_eq!(before, entities(&mut db).await);
        }
        let before = entities(&mut db).await;
        assert_eq!(
            snapshot_digest(&before),
            case["before_snapshot_sha256"],
            "before snapshot: {}",
            case["name"]
        );
        let before_projections = projections(&mut db).await;
        let mut options =
            HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
        options.clock = now;
        let mut request = Request::builder()
            .method(case["method"].as_str().unwrap_or("GET"))
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
        let response = router(store.clone(), options)
            .unwrap()
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = response.status().as_u16();
        assert_eq!(
            json!(
                response
                    .headers()
                    .get("cache-control")
                    .map(|h| h.to_str().unwrap())
            ),
            case["expected_cache_control"],
            "{}",
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
            "{}",
            case["name"]
        );
        let after = entities(&mut db).await;
        assert_eq!(
            snapshot_digest(&after),
            case["after_snapshot_sha256"],
            "after snapshot: {}",
            case["name"]
        );
        assert_eq!(
            before.keys().collect::<Vec<_>>(),
            after.keys().collect::<Vec<_>>(),
            "{}",
            case["name"]
        );
        let changed: BTreeSet<_> = before
            .keys()
            .filter(|id| before[*id] != after[*id])
            .cloned()
            .collect();
        let expected: BTreeSet<_> = case["expected_changes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["before"]["payload"]["id"].as_str().unwrap().to_owned())
            .collect();
        assert_eq!(changed, expected, "changed identities: {}", case["name"]);
        for change in case["expected_changes"].as_array().unwrap() {
            let id = change["before"]["payload"]["id"].as_str().unwrap();
            assert_eq!(
                record(&before[id]),
                change["before"],
                "before {id}: {}",
                case["name"]
            );
            assert_eq!(
                record(&after[id]),
                change["after"],
                "after {id}: {}",
                case["name"]
            );
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
                    before[id][field], after[id][field],
                    "envelope {field} {id}: {}",
                    case["name"]
                );
            }
            assert_eq!(
                after[id]["revision"],
                change["after"]["payload"]["revision"]
            );
            assert_eq!(
                after[id]["updated_at"],
                sql_time(&change["after"]["payload"]["updated_at"])
            );
        }
        assert_eq!(
            before_projections,
            projections(&mut db).await,
            "projections: {}",
            case["name"]
        );
    }
    store.shutdown().await.unwrap();
    let saved = entities(&mut db).await;
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(saved, entities(&mut db).await);
    let actual: BTreeMap<_, _> = saved
        .into_iter()
        .map(|(id, row)| (id, record(&row)))
        .collect();
    let expected: BTreeMap<_, _> = ["projects", "final_records", "final_dependencies"]
        .into_iter()
        .flat_map(|field| fixture[field].as_array().unwrap())
        .map(|row| {
            (
                row["payload"]["id"].as_str().unwrap().to_owned(),
                json!({"kind":row["kind"],"payload":row["payload"]}),
            )
        })
        .collect();
    assert_eq!(actual, expected);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}
