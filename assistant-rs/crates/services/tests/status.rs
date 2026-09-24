#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_services::{AssistantRecords, Error};
use nebula_assistant_storage::entities::{Config, Mutation, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::Connection;
use std::path::Path;

fn oracle() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-status.json")).unwrap()
}
fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn sql_time(value: &Value) -> String {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}
async fn setup(path: &Path) -> (SqliteAssistantStore, AssistantRecords) {
    support::database(path).await;
    let fixture = oracle();
    let mut raw = support::raw(path).await;
    sqlx::query("DELETE FROM entities")
        .execute(&mut raw)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut raw)
        .await
        .unwrap();
    for row in fixture["projects"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fixture["initial_records"].as_array().unwrap())
        .chain(fixture["dependency_records"].as_array().unwrap())
    {
        let p = &row["payload"];
        let session = if row["kind"] == "native_hook_executions" {
            p["chat_session_id"].as_str()
        } else if row["kind"] == "chat_sessions" || row["kind"] == "engagements" {
            None
        } else {
            p["session_id"].as_str()
        };
        sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
            .bind(p["id"].as_str()).bind(row["kind"].as_str()).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
            .bind(serde_json::to_string(p).unwrap()).bind(session).bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
            .execute(&mut raw).await.unwrap();
    }
    raw.close().await.unwrap();
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    (store.clone(), AssistantRecords::new(store))
}
async fn projection(services: &AssistantRecords, step: &Value) -> Result<Value, Error> {
    match step["kind"].as_str().unwrap() {
        "activity" => {
            services
                .session_activity(step["project_id"].as_str().unwrap())
                .await
        }
        "queue" => {
            services
                .saved_queue(step["session_id"].as_str().unwrap(), now())
                .await
        }
        "hooks" => services.turn_hooks(step["turn_id"].as_str().unwrap()).await,
        other => panic!("Unknown service projection {other}"),
    }
}

#[tokio::test]
async fn python_status_oracle_matches_retained_projections_and_errors() {
    let temp = tempfile::tempdir().unwrap();
    let (store, services) = setup(&temp.path().join("nebula.db")).await;
    let fixture = oracle();
    let mut compared = 0;
    for case in fixture["cases"].as_array().unwrap() {
        if !case["service"].is_object() {
            continue;
        }
        let outcome = projection(&services, &case["service"]).await;
        let expected = &case["expected"];
        match outcome {
            Ok(value) => {
                assert_eq!(expected["status"], 200, "{}", case["name"]);
                assert_eq!(value, expected["body"], "{}", case["name"]);
                if case["service"]["kind"] == "hooks" {
                    let serialized = value.to_string();
                    assert!(!serialized.contains("Fixture output must not appear"));
                    assert!(!serialized.contains("Fixture stderr must not appear"));
                    assert!(!serialized.contains("hook_snapshot"));
                }
            }
            Err(Error::HistoryConflict(detail)) => {
                assert_eq!(expected["status"], 409, "{}", case["name"]);
                assert_eq!(expected["body"]["code"], "chat.chat_history_conflict");
                assert_eq!(expected["body"]["detail"], detail);
            }
            Err(Error::LegacyUnhandled) => {
                assert_eq!(expected["status"], 500, "{}", case["name"]);
                assert_eq!(expected["body"]["code"], "api.unhandled_exception");
            }
            Err(Error::ModelValidation(errors)) => {
                assert_eq!(expected["status"], 422, "{}", case["name"]);
                assert_eq!(expected["body"]["code"], "api.model_validation");
                assert_eq!(expected["body"]["detail"], json!(errors));
            }
            Err(error @ Error::EntityNotFound { .. }) => {
                assert_eq!(expected["status"], 404, "{}", case["name"]);
                assert_eq!(expected["body"]["code"], "chat.not_found_error");
                assert_eq!(expected["body"]["detail"], error.to_string());
            }
            Err(error) => panic!("{}: unexpected {error:?}", case["name"]),
        }
        compared += 1;
    }
    assert!(
        compared >= 30,
        "The oracle must exercise successful and failed projections"
    );
    store.shutdown().await.unwrap();
}

async fn retained_rows(
    path: &Path,
) -> Vec<(
    String,
    String,
    Option<String>,
    i64,
    String,
    Option<String>,
    String,
    String,
)> {
    let mut raw = support::raw(path).await;
    let rows = sqlx::query_as("SELECT id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at FROM entities ORDER BY id")
        .fetch_all(&mut raw).await.unwrap();
    raw.close().await.unwrap();
    rows
}

#[tokio::test]
async fn status_reads_preserve_rows_and_ephemeral_queue_absence_after_reopen() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, services) = setup(&path).await;
    let before = retained_rows(&path).await;
    assert!(!before.iter().any(|row| row.0 == "chat-queue-idle"));
    let mut raw = support::raw(&path).await;
    sqlx::query("INSERT INTO session_projections(session_id,revision,digest) VALUES ('idle',53,'status-retained')")
        .execute(&mut raw).await.unwrap();
    raw.close().await.unwrap();
    let fixture = oracle();
    for case in fixture["cases"].as_array().unwrap() {
        if case["service"].is_object() {
            let _ = projection(&services, &case["service"]).await;
        }
    }
    let initial = services.saved_queue("idle", now()).await.unwrap();
    assert_eq!(initial["revision"], 0);
    assert_eq!(initial["created_at"], "2030-01-01T12:00:00Z");
    assert_eq!(initial["updated_at"], initial["created_at"]);
    assert_eq!(retained_rows(&path).await, before);
    store.shutdown().await.unwrap();
    drop(services);
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let services = AssistantRecords::new(reopened.clone());
    let later = services
        .saved_queue("idle", now() + chrono::Duration::minutes(1))
        .await
        .unwrap();
    assert_eq!(later["revision"], 0);
    assert_eq!(later["created_at"], "2030-01-01T12:01:00Z");
    assert_eq!(later["updated_at"], later["created_at"]);
    for kind in ["activity", "queue", "hooks"] {
        let case = fixture["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|case| case["service"]["kind"] == kind && case["expected"]["status"] == 200)
            .unwrap();
        assert_eq!(
            projection(&services, &case["service"]).await.unwrap(),
            case["expected"]["body"]
        );
    }
    assert_eq!(retained_rows(&path).await, before);
    let mut raw = support::raw(&path).await;
    let watermark: (i64, String) =
        sqlx::query_as("SELECT revision,digest FROM session_projections WHERE session_id='idle'")
            .fetch_one(&mut raw)
            .await
            .unwrap();
    assert_eq!(watermark, (53, "status-retained".into()));
    raw.close().await.unwrap();
    reopened.shutdown().await.unwrap();
}

fn record(kind: Kind, id: &str, fields: Value) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "precedence".into();
    p.as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}

#[tokio::test]
async fn activity_conflicts_precede_corrupt_visible_sessions() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![
            Mutation::Create(record(
                Kind::Session,
                "malformed-visible",
                json!({"metadata":{}}),
            )),
            Mutation::Create(record(
                Kind::Turn,
                "duplicate-a",
                json!({"session_id":"absent-session","status":"routing"}),
            )),
            Mutation::Create(record(
                Kind::Turn,
                "duplicate-b",
                json!({"session_id":"absent-session","status":"routing"}),
            )),
        ])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    sqlx::query(
        "UPDATE entities SET payload=json_set(payload,'$.title',42) WHERE id='malformed-visible'",
    )
    .execute(&mut raw)
    .await
    .unwrap();
    raw.close().await.unwrap();
    let services = AssistantRecords::new(store.clone());
    assert!(
        matches!(
            services.session_activity("precedence").await,
            Err(Error::HistoryConflict(
                "chat session has multiple active turns"
            ))
        ),
        "Pending-turn conflicts precede visible-session decoding errors"
    );
    let mut raw = support::raw(&path).await;
    sqlx::query("DELETE FROM entities WHERE id='duplicate-b'")
        .execute(&mut raw)
        .await
        .unwrap();
    raw.close().await.unwrap();
    assert!(
        matches!(
            services.session_activity("precedence").await,
            Err(Error::LegacyUnhandled)
        ),
        "Without a pending conflict the deferred session decode error must remain visible"
    );
    store.shutdown().await.unwrap();
}
