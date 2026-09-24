use axum::{
    body::{Body, to_bytes},
    http::Request,
};
use chrono::{DateTime, Utc};
use nebula_assistant_domain::session_state::ConnectionState;
use nebula_assistant_storage::entities::{Config, SqliteAssistantStore};
use nebula_assistant_transport::{Authentication, HttpConfig, router};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection, sqlite::SqliteConnectOptions};
use std::{
    collections::BTreeMap,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicI64, AtomicUsize, Ordering},
    },
};
use tower::ServiceExt;

// Only this exact test uses this clock. The router reads it through a trusted
// configuration function, never through an HTTP request field.
static CLOCK_MICROS: AtomicI64 = AtomicI64::new(0);
fn now() -> DateTime<Utc> {
    DateTime::from_timestamp_micros(CLOCK_MICROS.load(Ordering::SeqCst)).unwrap()
}
fn set_clock(value: &Value) {
    CLOCK_MICROS.store(
        DateTime::parse_from_rfc3339(value.as_str().unwrap())
            .unwrap()
            .timestamp_micros(),
        Ordering::SeqCst,
    );
}
fn sql_time(value: &Value) -> String {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}
async fn raw(path: &Path) -> SqliteConnection {
    SqliteConnection::connect_with(
        &SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true)
            .foreign_keys(true),
    )
    .await
    .unwrap()
}
async fn put_record(db: &mut SqliteConnection, row: &Value) {
    let p = &row["payload"];
    let kind = row["kind"].as_str().unwrap();
    let session = if kind == "chat_turns" {
        p["session_id"].as_str()
    } else {
        p["chat_session_id"].as_str()
    };
    sqlx::query("INSERT INTO entities (id,kind,engagement_id,revision,chat_session_id,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,engagement_id=excluded.engagement_id,revision=excluded.revision,chat_session_id=excluded.chat_session_id,payload=excluded.payload,created_at=excluded.created_at,updated_at=excluded.updated_at")
        .bind(p["id"].as_str()).bind(kind).bind(p["engagement_id"].as_str())
        .bind(p["revision"].as_i64()).bind(session).bind(serde_json::to_string(p).unwrap())
        .bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
        .execute(db).await.unwrap();
}
async fn put_event(db: &mut SqliteConnection, p: &Value) {
    sqlx::query("INSERT INTO operation_events (id,operation_id,operation_kind,engagement_id,sequence,event_type,payload,actor_id,occurred_at,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(p["operation_id"].as_str()).bind(p["operation_kind"].as_str())
        .bind(p["engagement_id"].as_str()).bind(p["sequence"].as_i64()).bind(p["event_type"].as_str())
        .bind(serde_json::to_string(&p["payload"]).unwrap()).bind(p["actor_id"].as_str())
        .bind(sql_time(&p["occurred_at"])).bind(p["idempotency_key"].as_str())
        .execute(db).await.unwrap();
}
async fn watermarks(db: &mut SqliteConnection) -> Value {
    let rows: Vec<(String, i64, String)> = sqlx::query_as(
        "SELECT session_id,revision,digest FROM session_projections ORDER BY session_id",
    )
    .fetch_all(db)
    .await
    .unwrap();
    rows.into_iter()
        .map(|(session_id, revision, digest)| json!({"session_id":session_id,"revision":revision,"digest":digest}))
        .collect()
}
async fn retained(db: &mut SqliteConnection) -> (Vec<String>, Vec<String>) {
    let entities = sqlx::query_scalar("SELECT json_object('id',id,'kind',kind,'engagement_id',engagement_id,'revision',revision,'automation_run_id',automation_run_id,'automation_session_id',automation_session_id,'automation_status',automation_status,'automation_expires_at',automation_expires_at,'chat_session_id',chat_session_id,'payload',payload,'created_at',created_at,'updated_at',updated_at) FROM entities ORDER BY id")
        .fetch_all(&mut *db).await.unwrap();
    let events = sqlx::query_scalar("SELECT json_object('id',id,'operation_id',operation_id,'operation_kind',operation_kind,'engagement_id',engagement_id,'sequence',sequence,'event_type',event_type,'payload',payload,'actor_id',actor_id,'occurred_at',occurred_at,'idempotency_key',idempotency_key) FROM operation_events ORDER BY id")
        .fetch_all(db).await.unwrap();
    (entities, events)
}
fn normalize(value: Value) -> Value {
    match value {
        Value::Object(fields) => Value::Object(
            fields
                .into_iter()
                .map(|(key, v)| {
                    let v = if ["request_id", "error_id"].contains(&key.as_str()) {
                        Value::from("<generated>")
                    } else {
                        normalize(v)
                    };
                    (key, v)
                })
                .collect(),
        ),
        Value::Array(values) => values.into_iter().map(normalize).collect(),
        value => value,
    }
}
fn connection(value: &str) -> ConnectionState {
    match value {
        "connected" => ConnectionState::Connected,
        "disconnected" => ConnectionState::Disconnected,
        "unknown" => ConnectionState::Unknown,
        _ => panic!("invalid fixture connection"),
    }
}

#[tokio::test]
async fn python_state_http_oracle_preserves_watermarks_pending_actions_and_passive_connections() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-state.json")).unwrap();
    assert!(fixture.get("capture_pending").is_none());
    assert_eq!(fixture["cases"].as_array().unwrap().len(), 81);
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = raw(&path).await;
    let schema: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-storage.json")).unwrap();
    for sql in schema["schema_sql"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fixture["schema_sql"].as_array().unwrap())
    {
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
            put_record(&mut db, row).await;
        }
    }
    for row in fixture["initial_operation_events"].as_array().unwrap() {
        put_event(&mut db, row).await;
    }
    for row in fixture["initial_watermarks"].as_array().unwrap() {
        sqlx::query("INSERT INTO session_projections(session_id,revision,digest) VALUES(?,?,?)")
            .bind(row["session_id"].as_str())
            .bind(row["revision"].as_i64())
            .bind(row["digest"].as_str())
            .execute(&mut db)
            .await
            .unwrap();
    }
    let mut store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    set_clock(&fixture["clock"]);
    // The source's initial Database.session() annotates errors as storage, but
    // its writer re-projection uses a plain Session and keeps the chat feature.
    // Introduce the same valid-model/invalid-comparison expiry after admission,
    // while the isolated writer waits, to exercise that distinct response path.
    let original = fixture["dependency_records"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["payload"]["id"] == "sequence-approval")
        .unwrap();
    let before_fault = retained(&mut db).await;
    let before_watermarks = watermarks(&mut db).await;
    let mut locked = db.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let mut options = HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
    options.clock = now;
    let queued = tokio::spawn(
        router(store.clone(), options).unwrap().oneshot(
            Request::builder()
                .uri("/api/v1/chat/sessions/sequence/state")
                .header("host", "nebula.test:9443")
                .header("authorization", "Bearer fixture-core")
                .header("x-nebula-operation-id", "fixture-operation")
                .body(Body::empty())
                .unwrap(),
        ),
    );
    tokio::time::timeout(std::time::Duration::from_secs(10), async {
        while store.admission().available_bytes == Config::default().queued_bytes {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("session-state request never reached writer admission");
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.expires_at','2030-01-01T12:00:01') WHERE id='sequence-approval'")
        .execute(&mut *locked).await.unwrap();
    locked.commit().await.unwrap();
    let injected = retained(&mut db).await;
    let response = queued.await.unwrap().unwrap();
    let status = response.status().as_u16();
    assert_eq!(
        json!(
            response
                .headers()
                .get("cache-control")
                .map(|v| v.to_str().unwrap())
        ),
        fixture["write_phase_error"]["expected_cache_control"]
    );
    let body: Value = serde_json::from_slice(
        &to_bytes(response.into_body(), 16 * 1024 * 1024)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        json!({"status":status,"body":normalize(body)}),
        fixture["write_phase_error"]["expected"]
    );
    assert_eq!(injected, retained(&mut db).await);
    assert_eq!(before_watermarks, watermarks(&mut db).await);
    put_record(&mut db, original).await;
    assert_eq!(before_fault, retained(&mut db).await);
    let mut enabled = fixture["runtime_enabled"].as_bool().unwrap();
    let mut connections: BTreeMap<String, String> = fixture["connections"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(id, state)| (id.clone(), state.as_str().unwrap().into()))
        .collect();
    let calls = Arc::new(AtomicUsize::new(0));
    for case in fixture["cases"].as_array().unwrap() {
        for action in case["pre_actions"].as_array().into_iter().flatten() {
            match action["action"].as_str().unwrap() {
                "set_clock" => set_clock(&action["now"]),
                "set_runtime" => enabled = action["enabled"].as_bool().unwrap(),
                "set_connection" => {
                    connections.insert(
                        action["harness_session_id"].as_str().unwrap().into(),
                        action["state"].as_str().unwrap().into(),
                    );
                }
                "upsert_record" => put_record(&mut db, &action["record"]).await,
                "delete_record" => {
                    sqlx::query("DELETE FROM entities WHERE id=?")
                        .bind(action["id"].as_str())
                        .execute(&mut db)
                        .await
                        .unwrap();
                }
                "append_operation_event" => put_event(&mut db, &action["record"]).await,
                "reopen" => {
                    store.shutdown().await.unwrap();
                    store = SqliteAssistantStore::open(&path, Config::default())
                        .await
                        .unwrap();
                }
                _ => panic!("unknown fixture action {action}"),
            }
        }
        let mut options =
            HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
        options.clock = now;
        if enabled {
            let observations = connections.clone();
            let calls = calls.clone();
            options.harness_connection = Some(Arc::new(move |id| {
                calls.fetch_add(1, Ordering::SeqCst);
                connection(
                    observations
                        .get(id)
                        .map(String::as_str)
                        .unwrap_or("disconnected"),
                )
            }));
        }
        let mut request = Request::builder()
            .method(case["method"].as_str().unwrap())
            .uri(case["path"].as_str().unwrap())
            .header("host", "nebula.test:9443")
            .header("x-nebula-operation-id", "fixture-operation");
        if let Some(headers) = case["auth"]["headers"].as_object() {
            for (key, value) in headers {
                request = request.header(key, value.as_str().unwrap());
            }
        } else {
            request = request.header("authorization", "Bearer fixture-core");
        }
        let before = retained(&mut db).await;
        let observed_before = calls.load(Ordering::SeqCst);
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
                    .map(|v| v.to_str().unwrap())
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
        if status == 401 {
            assert_eq!(calls.load(Ordering::SeqCst), observed_before);
        }
        assert_eq!(
            before,
            retained(&mut db).await,
            "retained state changed: {}",
            case["name"]
        );
        assert_eq!(
            watermarks(&mut db).await,
            case["expected_watermarks"],
            "{}",
            case["name"]
        );
    }
    store.shutdown().await.unwrap();
    let before_reopen = retained(&mut db).await;
    let cache_before_reopen = watermarks(&mut db).await;
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(before_reopen, retained(&mut db).await);
    assert_eq!(cache_before_reopen, watermarks(&mut db).await);
    let rows: Vec<(String, String, String)> =
        sqlx::query_as("SELECT id,kind,payload FROM entities ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap();
    let actual: BTreeMap<_, _> = rows
        .into_iter()
        .map(|(id, kind, payload)| {
            (
                id,
                json!({"kind":kind,"payload":serde_json::from_str::<Value>(&payload).unwrap()}),
            )
        })
        .collect();
    let expected: BTreeMap<_, _> = ["projects", "final_records", "final_dependencies"]
        .into_iter()
        .flat_map(|field| fixture[field].as_array().unwrap())
        .map(|row| {
            (
                row["payload"]["id"].as_str().unwrap().to_owned(),
                row.clone(),
            )
        })
        .collect();
    assert_eq!(actual, expected);
    let actual_events: Vec<Value> = retained(&mut db)
        .await
        .1
        .into_iter()
        .map(|row| serde_json::from_str(&row).unwrap())
        .collect();
    let expected_events: Vec<Value> = fixture["final_operation_events"]
        .as_array()
        .unwrap()
        .iter()
        .cloned()
        .map(|mut row| {
            row["payload"] = serde_json::to_string(&row["payload"]).unwrap().into();
            row["occurred_at"] = sql_time(&row["occurred_at"]).into();
            row
        })
        .collect();
    assert_eq!(actual_events, expected_events);
    assert_eq!(watermarks(&mut db).await, fixture["final_watermarks"]);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}
