#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use chrono::{DateTime, SecondsFormat, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_services::{AssistantRecords, context::CursorWrite};
use nebula_assistant_storage::entities::{Config, Mutation, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::Connection;
use std::{collections::BTreeSet, path::Path};

fn oracle() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-catchup.json")).unwrap()
}
fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn after_expiry() -> DateTime<Utc> {
    now() + chrono::Duration::seconds(2)
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
    let mut raw = support::raw(path).await;
    sqlx::query("DELETE FROM entities")
        .execute(&mut raw)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut raw)
        .await
        .unwrap();
    let fixture = oracle();
    for row in fixture["projects"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fixture["dependency_records"].as_array().unwrap())
    {
        let p = &row["payload"];
        sqlx::query("INSERT INTO entities (id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
            .bind(p["id"].as_str().unwrap()).bind(row["kind"].as_str().unwrap())
            .bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64().unwrap())
            .bind(serde_json::to_string(p).unwrap()).bind(p["chat_session_id"].as_str())
            .bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
            .execute(&mut raw).await.unwrap();
    }
    raw.close().await.unwrap();
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    for batch in fixture["initial_records"].as_array().unwrap().chunks(64) {
        let records = batch
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
        store.apply(records).await.unwrap();
    }
    (store.clone(), AssistantRecords::with_clock(store, now))
}

fn fixed_timestamps(fixture: &Value) -> BTreeSet<String> {
    let mut stamps = BTreeSet::new();
    for field in ["projects", "initial_records", "dependency_records"] {
        for row in fixture[field].as_array().unwrap() {
            for field in ["created_at", "updated_at"] {
                let value = row["payload"][field].as_str().unwrap();
                stamps.insert(value.into());
                let parsed = DateTime::parse_from_rfc3339(value).unwrap();
                stamps.insert(parsed.to_rfc3339_opts(
                    if parsed.timestamp_subsec_micros() == 0 {
                        SecondsFormat::Secs
                    } else {
                        SecondsFormat::Micros
                    },
                    false,
                ));
                for z in [false, true] {
                    stamps.insert(parsed.with_timezone(&Utc).to_rfc3339_opts(
                        if parsed.timestamp_subsec_micros() == 0 {
                            SecondsFormat::Secs
                        } else {
                            SecondsFormat::Micros
                        },
                        z,
                    ));
                }
            }
        }
    }
    stamps
}
fn normalize(mut value: Value, stamps: &BTreeSet<String>) -> Value {
    match &mut value {
        Value::Object(fields) => {
            for (key, item) in fields {
                if ["request_id", "error_id"].contains(&key.as_str())
                    || (["created_at", "updated_at"].contains(&key.as_str())
                        && item.as_str().is_none_or(|text| !stamps.contains(text)))
                {
                    *item = "<generated>".into();
                } else {
                    *item = normalize(item.take(), stamps);
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                *item = normalize(item.take(), stamps);
            }
        }
        _ => {}
    }
    value
}

#[tokio::test]
async fn python_catchup_oracle_matches_projection_and_cursor_transitions() {
    let temp = tempfile::tempdir().unwrap();
    let (store, services) = setup(&temp.path().join("nebula.db")).await;
    let writes = AssistantRecords::new(store.clone());
    let fixture = oracle();
    let stamps = fixed_timestamps(&fixture);
    let mut compared = 0;
    for case in fixture["cases"].as_array().unwrap() {
        if case["expected"]["status"] != 200 || !case["service"].is_object() {
            continue;
        }
        let step = &case["service"];
        let session = step["session_id"].as_str().unwrap();
        let outcome = match step["kind"].as_str().unwrap() {
            "catch_up" => services
                .catch_up(
                    session,
                    step["device_id"].as_str().unwrap(),
                    step["authenticated_device"].as_str(),
                )
                .await
                .unwrap(),
            "turn_summary" => services
                .turn_summary(session, step["turn_id"].as_str().unwrap())
                .await
                .unwrap(),
            "advance_cursor" => writes
                .advance_cursor_at(
                    session,
                    serde_json::from_value::<CursorWrite>(case["body"].clone()).unwrap(),
                    step["authenticated_device"].as_str(),
                    now(),
                )
                .await
                .unwrap()
                .into_payload(),
            other => panic!("unknown service step {other}"),
        };
        assert_eq!(
            normalize(outcome, &stamps),
            case["expected"]["body"],
            "{}",
            case["name"]
        );
        compared += 1;
    }
    assert!(
        compared >= 20,
        "the oracle must exercise the real projections and transitions"
    );
    store.shutdown().await.unwrap();
}

async fn unchanged_records(path: &Path) -> Vec<(String, String, i64, String, String, String)> {
    let mut raw = support::raw(path).await;
    let rows = sqlx::query_as("SELECT id,kind,revision,payload,created_at,updated_at FROM entities WHERE kind != 'chat_read_cursors' ORDER BY id")
        .fetch_all(&mut raw).await.unwrap();
    raw.close().await.unwrap();
    rows
}

#[tokio::test]
async fn clock_expiry_and_secret_notices_are_pure_reads_without_watermarks() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, services) = setup(&path).await;
    let initial = unchanged_records(&path).await;
    let mut raw = support::raw(&path).await;
    sqlx::query("INSERT INTO session_projections (session_id,revision,digest) VALUES ('pending',47,'retained-projection')")
        .execute(&mut raw).await.unwrap();
    raw.close().await.unwrap();
    let before = services
        .catch_up("pending", "uninitialized", None)
        .await
        .unwrap();
    assert_eq!(before["initialized"], false);
    assert_eq!(before["items"], json!([]));
    let notices = before["pending"].as_array().unwrap();
    assert_eq!(
        notices
            .iter()
            .find(|notice| notice["id"] == "question-secret")
            .unwrap()["text"],
        "A secret answer is required"
    );
    assert!(
        !serde_json::to_string(&before)
            .unwrap()
            .contains("PRIVATE-PROMPT-NOT-FOR-DISPLAY")
    );
    assert!(
        notices
            .iter()
            .any(|notice| notice["id"] == "approval-future")
    );
    for id in ["approval-expired", "approval-equal"] {
        assert!(!notices.iter().any(|notice| notice["id"] == id));
    }
    let later = AssistantRecords::with_clock(store.clone(), after_expiry)
        .catch_up("pending", "uninitialized", None)
        .await
        .unwrap();
    assert!(
        !later["pending"]
            .as_array()
            .unwrap()
            .iter()
            .any(|notice| notice["id"] == "approval-future")
    );
    assert_eq!(unchanged_records(&path).await, initial);
    let mut raw = support::raw(&path).await;
    let projections: Vec<(String, i64, String)> = sqlx::query_as(
        "SELECT session_id,revision,digest FROM session_projections ORDER BY session_id",
    )
    .fetch_all(&mut raw)
    .await
    .unwrap();
    assert_eq!(
        projections,
        vec![("pending".into(), 47, "retained-projection".into())]
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn acknowledging_catchup_keeps_pending_actions_and_other_device_history() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, services) = setup(&path).await;
    let writes = AssistantRecords::new(store.clone());
    let initial = unchanged_records(&path).await;
    for (device, authenticated) in [("spoofed", Some("paired")), ("other-device", None)] {
        writes
            .advance_cursor_at(
                "pending",
                CursorWrite {
                    expected_revision: 0.into(),
                    device_id: device.into(),
                    through_at: "2019-01-01T00:00:00Z".into(),
                },
                authenticated,
                now(),
            )
            .await
            .unwrap();
    }
    let before = services
        .catch_up("pending", "spoofed", Some("paired"))
        .await
        .unwrap();
    assert!(!before["items"].as_array().unwrap().is_empty());
    let original_cursor = services
        .read_cursor("pending", "spoofed", Some("paired"))
        .await
        .unwrap()
        .unwrap();
    let updated_cursor = writes
        .advance_cursor_at(
            "pending",
            CursorWrite {
                expected_revision: 1.into(),
                device_id: "other-device".into(),
                through_at: before["through_at"].as_str().unwrap().into(),
            },
            Some("paired"),
            now(),
        )
        .await
        .unwrap();
    assert_eq!(
        updated_cursor.payload()["created_at"],
        original_cursor.payload()["created_at"]
    );
    assert!(
        DateTime::parse_from_rfc3339(updated_cursor.payload()["updated_at"].as_str().unwrap())
            .unwrap()
            >= DateTime::parse_from_rfc3339(
                original_cursor.payload()["updated_at"].as_str().unwrap()
            )
            .unwrap()
    );
    let after = services
        .catch_up("pending", "spoofed", Some("paired"))
        .await
        .unwrap();
    assert_eq!(after["revision"], 2);
    assert_eq!(after["items"], json!([]));
    assert_eq!(after["pending"], before["pending"]);
    assert!(!after["pending"].as_array().unwrap().is_empty());
    let other = services
        .catch_up("pending", "other-device", None)
        .await
        .unwrap();
    assert_eq!(other["revision"], 1);
    assert_eq!(other["items"], before["items"]);
    assert_eq!(unchanged_records(&path).await, initial);
    store.shutdown().await.unwrap();
    drop(services);
    drop(writes);
    drop(store);
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let services = AssistantRecords::with_clock(reopened.clone(), now);
    assert_eq!(
        services
            .catch_up("pending", "ignored", Some("paired"))
            .await
            .unwrap(),
        after
    );
    assert_eq!(
        services
            .catch_up("pending", "other-device", None)
            .await
            .unwrap(),
        other
    );
    reopened.shutdown().await.unwrap();
}
