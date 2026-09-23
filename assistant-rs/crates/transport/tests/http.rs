use axum::{
    Router,
    body::{Body, to_bytes},
    http::{HeaderValue, Request},
};
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    auth::PairedDevice,
    records::{AssistantKind as Kind, StoredAssistantRecord},
};
use nebula_assistant_storage::entities::{Config, Mutation, SqliteAssistantStore};
use nebula_assistant_transport::{Authentication, HttpConfig, router};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection, sqlite::SqliteConnectOptions};
use std::{path::Path, time::Duration};
use tower::ServiceExt;

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-auth.json")).unwrap()
}
fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T00:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn config() -> HttpConfig {
    let mut c = HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
    c.clock = now;
    c
}
async fn raw(path: &Path) -> SqliteConnection {
    SqliteConnection::connect_with(
        &SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true),
    )
    .await
    .unwrap()
}
fn sql_time(value: &Value) -> String {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}
async fn setup(path: &Path) -> SqliteAssistantStore {
    let ddl: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-storage.json")).unwrap();
    let mut connection = raw(path).await;
    for sql in ddl["schema_sql"].as_array().unwrap() {
        sqlx::query(sql.as_str().unwrap())
            .execute(&mut connection)
            .await
            .unwrap();
    }
    sqlx::query(
        "INSERT INTO schema_versions (version,applied_at) VALUES (5,'2020-01-01 00:00:00.000000')",
    )
    .execute(&mut connection)
    .await
    .unwrap();
    sqlx::query("INSERT INTO alembic_version VALUES ('0016_chat_session_lookup')")
        .execute(&mut connection)
        .await
        .unwrap();
    connection.close().await.unwrap();
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(
            StoredAssistantRecord::decode(
                Kind::Session,
                &serde_json::to_vec(&fixture()["session"]).unwrap(),
            )
            .unwrap(),
        )])
        .await
        .unwrap();
    set_device(path, &fixture()["device"]).await;
    store
}
async fn set_device(path: &Path, device: &Value) {
    let mut connection = raw(path).await;
    sqlx::query("INSERT INTO entities (id,kind,revision,payload,created_at,updated_at) VALUES (?,'paired_device_sessions',?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,created_at=excluded.created_at,updated_at=excluded.updated_at")
        .bind(device["id"].as_str()).bind(device["revision"].as_i64()).bind(serde_json::to_string(device).unwrap()).bind(sql_time(&device["created_at"])).bind(sql_time(&device["updated_at"])).execute(&mut connection).await.unwrap();
    sqlx::query("DELETE FROM entities WHERE kind='chat_read_cursors'")
        .execute(&mut connection)
        .await
        .unwrap();
    connection.close().await.unwrap();
}
fn normalize(mut value: Value) -> Value {
    match &mut value {
        Value::Object(fields) => {
            for (key, v) in fields {
                if ["created_at", "updated_at", "request_id", "error_id"].contains(&key.as_str()) {
                    *v = "<generated>".into()
                } else {
                    *v = normalize(v.take())
                }
            }
        }
        Value::Array(values) => {
            for v in values {
                *v = normalize(v.take())
            }
        }
        _ => {}
    }
    value
}
fn request(method: &str, path: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method(method)
        .uri(path)
        .header("host", "nebula.test:9443")
        .header("authorization", "Bearer fixture-core")
        .header("content-type", "application/json")
        .body(if body.is_null() {
            Body::empty()
        } else {
            Body::from(serde_json::to_vec(&body).unwrap())
        })
        .unwrap()
}
async fn value(response: axum::response::Response) -> Value {
    serde_json::from_slice(
        &to_bytes(response.into_body(), 20 * 1024 * 1024)
            .await
            .unwrap(),
    )
    .unwrap()
}
async fn paired(app: Router) -> axum::response::Response {
    let request = Request::builder()
        .uri("/api/v1/chat/sessions/session/decisions")
        .header("host", "nebula.test:9443")
        .header("cookie", "nebula_device=fixture-device")
        .body(Body::empty())
        .unwrap();
    app.oneshot(request).await.unwrap()
}

#[tokio::test]
async fn python_authentication_oracle_matches_http_status_headers_and_bodies() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    let app = router(store.clone(), config()).unwrap();
    let oracle = fixture();
    assert_eq!(oracle["cases"].as_array().unwrap().len(), 33);
    for case in oracle["cases"].as_array().unwrap() {
        set_device(&path, &case["device_before"]).await;
        let mut req = Request::builder()
            .method(case["method"].as_str().unwrap())
            .uri(case["path"].as_str().unwrap())
            .header("host", "nebula.test:9443")
            .header("content-type", "application/json")
            .body(if case["body"].is_null() {
                Body::empty()
            } else {
                Body::from(serde_json::to_vec(&case["body"]).unwrap())
            })
            .unwrap();
        for (name, value) in case["headers"].as_object().unwrap() {
            req.headers_mut().insert(
                name.parse::<axum::http::HeaderName>().unwrap(),
                HeaderValue::from_str(value.as_str().unwrap()).unwrap(),
            );
        }
        for (name, hex) in case["raw_headers"].as_object().unwrap() {
            let bytes: Vec<_> = hex
                .as_str()
                .unwrap()
                .as_bytes()
                .chunks_exact(2)
                .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
                .collect();
            req.headers_mut().insert(
                name.parse::<axum::http::HeaderName>().unwrap(),
                HeaderValue::from_bytes(&bytes).unwrap(),
            );
        }
        let response = app.clone().oneshot(req).await.unwrap();
        let status = response.status().as_u16();
        let www = response
            .headers()
            .get("www-authenticate")
            .map(|s| s.to_str().unwrap().to_owned());
        let request_id = response.headers()["x-request-id"]
            .to_str()
            .unwrap()
            .to_owned();
        let body = value(response).await;
        if status >= 400 {
            assert_eq!(body["request_id"], request_id);
        }
        assert_eq!(
            json!({"status":status,"www_authenticate":www,"body":normalize(body)}),
            case["expected"],
            "{}",
            case["name"]
        );
        let current = store
            .paired_device(oracle["device"]["token_sha256"].as_str().unwrap())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            normalize(current.payload().clone()),
            case["device_after"],
            "device after {}",
            case["name"]
        );
    }
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn device_lookup_passes_thousand_rows_and_revocation_is_immediate() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    let app = router(store.clone(), config()).unwrap();
    let mut connection = raw(&path).await;
    sqlx::query("WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<1001) INSERT INTO entities (id,kind,revision,payload,created_at,updated_at) SELECT 'older-'||i,'paired_device_sessions',1,json_set(?,'$.id','older-'||i,'$.token_sha256',printf('%064x',i)),?,? FROM n")
        .bind(serde_json::to_string(&fixture()["device"]).unwrap()).bind(sql_time(&fixture()["device"]["created_at"])).bind(sql_time(&fixture()["device"]["updated_at"])).execute(&mut connection).await.unwrap();
    assert_eq!(paired(app.clone()).await.status(), 200);
    sqlx::query("UPDATE entities SET revision=2,payload=json_set(payload,'$.revision',2,'$.revoked_at','2030-01-01T00:00:00Z') WHERE id='paired'").execute(&mut connection).await.unwrap();
    assert_eq!(paired(app).await.status(), 401);
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn idle_refresh_handles_contention_without_reverting_revocation() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    let mut stale = fixture()["device"].clone();
    stale["last_used_at"] = "2029-12-31T23:50:00Z".into();
    set_device(&path, &stale).await;
    let mut options = config();
    options.response_budget_bytes = 512 * 1024 * 1024;
    let app = router(store.clone(), options).unwrap();
    let mut tasks = tokio::task::JoinSet::new();
    for _ in 0..16 {
        let app = app.clone();
        tasks.spawn(async move {
            let response = paired(app).await;
            assert_eq!(response.status(), 200);
            value(response).await;
        });
    }
    while let Some(result) = tasks.join_next().await {
        result.unwrap();
    }
    let device = store
        .paired_device(stale["token_sha256"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(device.revision(), 2);
    assert_eq!(device.payload()["metadata"], stale["metadata"]);
    set_device(&path, &stale).await;
    let mut connection = raw(&path).await;
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let waiting = tokio::spawn(async move { paired(app).await });
    tokio::time::timeout(Duration::from_secs(3), async {
        while store.admission().available_bytes == Config::default().queued_bytes {
            tokio::time::sleep(Duration::from_millis(1)).await
        }
    })
    .await
    .unwrap();
    sqlx::query("UPDATE entities SET revision=2,payload=json_set(payload,'$.revision',2,'$.revoked_at','2030-01-01T00:00:00Z') WHERE id='paired'").execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    assert_eq!(waiting.await.unwrap().status(), 401);
    let revoked = store
        .paired_device(stale["token_sha256"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(revoked.payload()["last_used_at"], stale["last_used_at"]);
    assert_eq!(revoked.revision(), 2);
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn http_context_and_read_cursor_mutations_survive_reopen() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    let app = router(store.clone(), config()).unwrap();
    let path_decision = "/api/v1/chat/sessions/session/decisions/decision";
    for (revision, text) in [(0, "Keep Unicode 🦀"), (1, "Keep revised context")] {
        let response = app
            .clone()
            .oneshot(request(
                "PUT",
                path_decision,
                json!({"expected_revision":revision,"text":text}),
            ))
            .await
            .unwrap();
        assert_eq!(response.status(), 200);
        let payload = value(response).await;
        assert_eq!(payload["revision"], revision + 1);
        assert_eq!(payload["text"], text);
    }
    let response=app.clone().oneshot(request("PUT","/api/v1/chat/sessions/session/read-cursor",json!({"expected_revision":0,"device_id":"desktop","through_at":"2020-01-01T00:00:00Z"}))).await.unwrap();
    assert_eq!(response.status(), 200);
    let cursor = value(response).await;
    store.shutdown().await.unwrap();
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let app = router(store.clone(), config()).unwrap();
    let response = app
        .oneshot(request(
            "GET",
            "/api/v1/chat/sessions/session/decisions",
            Value::Null,
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let rows = value(response).await;
    assert_eq!(rows[0]["revision"], 2);
    assert_eq!(rows[0]["history"][0]["text"], "Keep Unicode 🦀");
    assert_eq!(
        store
            .get(Kind::ReadCursor, cursor["id"].as_str().unwrap())
            .await
            .unwrap()
            .payload(),
        &cursor
    );
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn request_body_response_and_deadline_limits_release_capacity() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    for bound_requests in [true, false] {
        let mut options = config();
        if bound_requests {
            options.concurrent_requests = 1
        } else {
            options.response_budget_bytes = 16 * 1024 * 1024
        };
        options.body_bytes = 128;
        options.request_timeout = Duration::from_millis(100);
        let app = router(store.clone(), options).unwrap();
        let held = app
            .clone()
            .oneshot(request(
                "GET",
                "/api/v1/chat/sessions/session/decisions",
                Value::Null,
            ))
            .await
            .unwrap();
        assert_eq!(held.status(), 200);
        let full = app
            .clone()
            .oneshot(request(
                "GET",
                "/api/v1/chat/sessions/session/decisions",
                Value::Null,
            ))
            .await
            .unwrap();
        assert_eq!(full.status(), 503);
        assert_eq!(full.headers()["retry-after"], "1");
        drop(full);
        drop(held);
        let too_large = app
            .clone()
            .oneshot(request(
                "PUT",
                "/api/v1/chat/sessions/session/decisions/oversized",
                json!({"expected_revision":0,"text":"x".repeat(200)}),
            ))
            .await
            .unwrap();
        assert_eq!(too_large.status(), 413);
        drop(too_large);
        let mut slow = request(
            "PUT",
            "/api/v1/chat/sessions/session/decisions/slow",
            Value::Null,
        );
        *slow.body_mut() = Body::from_stream(futures_util::stream::pending::<
            Result<Vec<u8>, std::io::Error>,
        >());
        let timed = app.clone().oneshot(slow).await.unwrap();
        assert_eq!(timed.status(), 504);
        drop(timed);
        let recovered = app
            .oneshot(request(
                "GET",
                "/api/v1/chat/sessions/session/decisions",
                Value::Null,
            ))
            .await
            .unwrap();
        assert_eq!(recovered.status(), 200);
        assert_eq!(value(recovered).await, json!([]));
    }
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn corrupt_device_records_fail_closed_without_exposing_credentials() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    let app = router(store.clone(), config()).unwrap();
    let good = PairedDevice::decode(&serde_json::to_vec(&fixture()["device"]).unwrap()).unwrap();
    assert_eq!(format!("{good:?}"), "PairedDevice { redacted }");
    let mut connection = raw(&path).await;
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.csrf_sha256','secret-sentinel-invalid') WHERE id='paired'").execute(&mut connection).await.unwrap();
    let response = paired(app).await;
    assert_eq!(response.status(), 503);
    let body = value(response).await.to_string();
    assert!(!body.contains("secret-sentinel"));
    assert!(!body.contains("fixture-device"));
    assert!(!body.contains("fixture-core"));
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn isolated_tcp_listener_serves_authenticated_assistant_routes() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    let app = router(store.clone(), config()).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let (stop, stopped) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async {
                let _ = stopped.await;
            })
            .await
            .unwrap()
    });
    let output=tokio::time::timeout(Duration::from_secs(3),async {
        let mut client=tokio::net::TcpStream::connect(address).await.unwrap();
        client.write_all(b"GET /api/v1/chat/sessions/session/decisions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer fixture-core\r\nConnection: close\r\n\r\n").await.unwrap();let mut out=Vec::new();client.take(65536).read_to_end(&mut out).await.unwrap();String::from_utf8(out).unwrap()
    }).await.unwrap();
    assert!(output.starts_with("HTTP/1.1 200 OK\r\n"));
    assert!(output.contains("[]"));
    stop.send(()).unwrap();
    server.await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn python_http_input_and_error_oracle_preserves_final_records() {
    use nebula_assistant_storage::entities::ListQuery;
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    store
        .apply(vec![Mutation::Delete {
            kind: Kind::Session,
            id: "session".into(),
            expected_revision: 1,
        }])
        .await
        .unwrap();
    let oracle: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-http.json")).unwrap();
    let oracle = expand_http_fixture(oracle);
    assert_eq!(oracle["cases"].as_array().unwrap().len(), 120);
    let initial = oracle["initial"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            Mutation::Create(
                StoredAssistantRecord::decode(
                    Kind::try_from(row["kind"].as_str().unwrap()).unwrap(),
                    &serde_json::to_vec(&row["payload"]).unwrap(),
                )
                .unwrap(),
            )
        })
        .collect();
    store.apply(initial).await.unwrap();
    let app = router(store.clone(), config()).unwrap();
    for case in oracle["cases"].as_array().unwrap() {
        let mut input = request(
            case["method"].as_str().unwrap(),
            case["path"].as_str().unwrap(),
            case["body"].clone(),
        );
        if let Some(raw) = case["raw_body"].as_str() {
            *input.body_mut() = Body::from(raw.to_owned());
        }
        input.headers_mut().insert(
            "x-nebula-operation-id",
            HeaderValue::from_static("fixture-operation"),
        );
        let response = app.clone().oneshot(input).await.unwrap();
        let status = response.status().as_u16();
        let actual = normalize(value(response).await);
        // Keep failure output bounded when checking long rejected input fields.
        if actual != case["expected"]["body"]
            || u64::from(status) != case["expected"]["status"].as_u64().unwrap()
        {
            panic!(
                "{}: status {status} expected {}; actual {} expected {}",
                case["name"],
                case["expected"]["status"],
                actual.to_string().chars().take(2200).collect::<String>(),
                case["expected"]["body"]
                    .to_string()
                    .chars()
                    .take(2200)
                    .collect::<String>()
            );
        }
    }
    let mut final_records = std::collections::BTreeMap::new();
    for kind in [Kind::Decision, Kind::ReadCursor] {
        let rows = store
            .list(ListQuery {
                limit: 1000,
                ..ListQuery::new(kind)
            })
            .await
            .unwrap();
        for row in rows.records {
            let payload = normalize(row.into_payload());
            final_records.insert(
                payload["id"].as_str().unwrap().to_owned(),
                json!({"kind":kind.as_str(),"payload":payload}),
            );
        }
    }
    let expected: std::collections::BTreeMap<_, _> = oracle["final"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            (
                row["payload"]["id"].as_str().unwrap().to_owned(),
                row.clone(),
            )
        })
        .collect();
    assert_eq!(final_records, expected);
    store.shutdown().await.unwrap();
}

fn expand_http_fixture(mut value: Value) -> Value {
    match &mut value {
        Value::Object(fields) if fields.len() == 1 && fields.contains_key("$repeat_string") => {
            let repeated = &fields["$repeat_string"];
            let text = repeated[0].as_str().unwrap();
            let count = repeated[1].as_u64().unwrap();
            assert_eq!(text.chars().count(), 1);
            assert!(count <= 200001);
            text.repeat(count as usize).into()
        }
        Value::Object(fields) => {
            for item in fields.values_mut() {
                *item = expand_http_fixture(item.take());
            }
            value
        }
        Value::Array(items) => {
            for item in items {
                *item = expand_http_fixture(item.take());
            }
            value
        }
        _ => value,
    }
}

#[tokio::test]
async fn validation_error_amplification_cannot_exceed_response_budget() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = setup(&path).await;
    let mut options = config();
    options.body_bytes = 16 * 1024 * 1024;
    options.response_budget_bytes = 16 * 1024 * 1024;
    let app = router(store.clone(), options).unwrap();
    let response = app
        .clone()
        .oneshot(request(
            "PUT",
            "/api/v1/chat/sessions/session/read-cursor",
            json!({"extra":"x".repeat(6*1024*1024)}),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), 413);
    let bytes = to_bytes(response.into_body(), 4096).await.unwrap();
    assert!(!bytes.is_empty());
    let response = app
        .oneshot(request(
            "PUT",
            "/api/v1/chat/sessions/session/read-cursor",
            json!({"expected_revision":0,"device_id":"phone","through_at":"2020-01-01T00:00:00Z"}),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    value(response).await;
    store.shutdown().await.unwrap();
}
