#[path = "../../storage/tests/support/mod.rs"]
mod support;

use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_services::{
    AssistantRecords, Error,
    context::{CursorWrite, DecisionWrite, cursor_id},
};
use nebula_assistant_storage::entities::{
    Config, Error as StorageError, Mutation, Precondition, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::Connection;
use std::{path::Path, time::Duration};

fn oracle() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-context.json")).unwrap()
}
fn write(body: Value) -> DecisionWrite {
    serde_json::from_value(body).unwrap()
}
fn cursor(body: Value) -> CursorWrite {
    serde_json::from_value(body).unwrap()
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
    raw.close().await.unwrap();
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    let initial = oracle()["initial"]
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
    (store.clone(), AssistantRecords::new(store))
}
fn normalize(mut value: Value) -> Value {
    match &mut value {
        Value::Object(fields) => {
            for (key, item) in fields {
                if key == "created_at" || key == "updated_at" {
                    *item = "<server-time>".into()
                } else {
                    *item = normalize(item.take());
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                *item = normalize(item.take());
            }
        }
        _ => {}
    }
    value
}
fn status(error: &Error) -> u16 {
    match error {
        Error::Conflict(_) | Error::Storage(StorageError::Conflict) => 409,
        Error::NotFound(_) | Error::Storage(StorageError::NotFound) => 404,
        Error::Invalid(_) => 422,
        other => panic!("unexpected service error: {other:?}"),
    }
}

#[tokio::test]
async fn python_service_oracle_matches_saved_context_and_cursor_transitions() {
    let temp = tempfile::tempdir().unwrap();
    let (store, service) = setup(&temp.path().join("nebula.db")).await;
    let fixture = oracle();
    let actions = fixture["actions"].as_array().unwrap();
    assert_eq!(actions.len(), 27);
    for (index, action) in actions.iter().enumerate() {
        let session = action["session"].as_str().unwrap();
        let outcome: Result<Value, Error> = match action["method"].as_str().unwrap() {
            "write" => service
                .write_decision(
                    session,
                    action["id"].as_str().unwrap(),
                    write(action["body"].clone()),
                )
                .await
                .map(StoredAssistantRecord::into_payload),
            "read" => service.decisions(session).await.map(|items| {
                Value::Array(
                    items
                        .into_iter()
                        .map(StoredAssistantRecord::into_payload)
                        .collect(),
                )
            }),
            "snapshot" => service
                .decision_snapshot(session, action["project"].as_str())
                .await
                .map(Value::Array),
            "cursor" => service
                .advance_cursor(
                    session,
                    cursor(action["body"].clone()),
                    action["authenticated_device"].as_str(),
                )
                .await
                .map(StoredAssistantRecord::into_payload),
            other => panic!("unknown action {other}"),
        };
        let result = match outcome {
            Ok(value) => json!({"status":200,"value":normalize(value)}),
            Err(error) => json!({"status":status(&error)}),
        };
        assert_eq!(result, action["expected"], "action {index}: {action}");
    }
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn promotion_collision_rolls_back_and_edits_retain_the_original_history() {
    let temp = tempfile::tempdir().unwrap();
    let (store, service) = setup(&temp.path().join("nebula.db")).await;
    let first=service.write_decision("session","one",write(json!({"expected_revision":0,"text":"First","kind":"question","source_message_id":"source","source_selection":"Keep β exact"}))).await.unwrap();
    let edited = service
        .write_decision(
            "session",
            "one",
            write(json!({"expected_revision":1,"text":"Second"})),
        )
        .await
        .unwrap();
    let history = &edited.payload()["history"][0];
    assert_eq!(history["text"], "First");
    assert_eq!(history["kind"], "question");
    assert_eq!(history["revision"], 1);
    assert_eq!(
        chrono::DateTime::parse_from_rfc3339(history["updated_at"].as_str().unwrap()).unwrap(),
        chrono::DateTime::parse_from_rfc3339(first.payload()["updated_at"].as_str().unwrap())
            .unwrap()
    );
    service
        .write_decision(
            "session",
            "project-one",
            write(json!({"expected_revision":0,"text":"Existing identity"})),
        )
        .await
        .unwrap();
    assert!(matches!(
        service
            .write_decision(
                "session",
                "one",
                write(json!({"expected_revision":2,"action":"promote"}))
            )
            .await,
        Err(Error::Storage(StorageError::Conflict))
    ));
    assert_eq!(store.get(Kind::Decision, "one").await.unwrap(), edited);
    store.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_edits_and_cursor_updates_have_one_revision_winner() {
    let temp = tempfile::tempdir().unwrap();
    let (store, service) = setup(&temp.path().join("nebula.db")).await;
    service
        .write_decision(
            "session",
            "one",
            write(json!({"expected_revision":0,"text":"Start"})),
        )
        .await
        .unwrap();
    service.advance_cursor("session",cursor(json!({"expected_revision":0,"device_id":"phone","through_at":"2020-01-01T00:00:00Z"})),None).await.unwrap();
    for is_cursor in [false, true] {
        let mut tasks = tokio::task::JoinSet::new();
        for index in 0..24 {
            let service = service.clone();
            tasks.spawn(async move {
                if is_cursor {service.advance_cursor("session",cursor(json!({"expected_revision":1,"device_id":"phone","through_at":format!("2020-01-02T00:00:{index:02}Z")})),None).await}
                else {service.write_decision("session","one",write(json!({"expected_revision":1,"text":format!("winner-{index}")}))).await}
            });
        }
        let mut wins = 0;
        while let Some(outcome) = tasks.join_next().await {
            match outcome.unwrap() {
                Ok(_) => wins += 1,
                Err(error) => assert_eq!(status(&error), 409),
            }
        }
        assert_eq!(wins, 1);
    }
    assert_eq!(
        store.get(Kind::Decision, "one").await.unwrap().payload()["history"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn transaction_guards_reject_changed_or_deleted_sessions_without_partial_writes() {
    let temp = tempfile::tempdir().unwrap();
    let (store, _service) = setup(&temp.path().join("nebula.db")).await;
    store
        .apply(vec![support::patch(
            Kind::Session,
            "session",
            1,
            json!({"title":"Changed"}),
        )])
        .await
        .unwrap();
    for revision in [1, 2] {
        if revision == 2 {
            store
                .apply(vec![Mutation::Delete {
                    kind: Kind::Session,
                    id: "session".into(),
                    expected_revision: 2,
                }])
                .await
                .unwrap();
        }
        let outcome = store
            .apply_guarded(
                vec![Precondition {
                    kind: Kind::Session,
                    id: "session".into(),
                    revision,
                }],
                vec![Mutation::Create(support::record(
                    Kind::Bookmark,
                    "not-created",
                ))],
            )
            .await;
        assert!(matches!(outcome, Err(StorageError::Conflict)));
        assert!(matches!(
            store.get(Kind::Bookmark, "not-created").await,
            Err(StorageError::NotFound)
        ));
    }
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn saved_context_limits_count_unicode_characters_and_filter_before_limiting() {
    let temp = tempfile::tempdir().unwrap();
    let (store, service) = setup(&temp.path().join("nebula.db")).await;
    for start in (0..1100).step_by(64) {
        let mut mutations = Vec::new();
        for index in start..(start + 64).min(1100) {
            let mut payload = support::payload(Kind::Decision);
            payload["id"] = format!("unrelated-{index}").into();
            payload["engagement_id"] = "other".into();
            payload["scope"] = "project".into();
            mutations.push(Mutation::Create(
                StoredAssistantRecord::decode(
                    Kind::Decision,
                    &serde_json::to_vec(&payload).unwrap(),
                )
                .unwrap(),
            ));
        }
        store.apply(mutations).await.unwrap();
    }
    for index in 0..10 {
        service
            .write_decision(
                "session",
                &format!("large-{index}"),
                write(json!({"expected_revision":0,"text":"🦀".repeat(4000)})),
            )
            .await
            .unwrap();
    }
    assert_eq!(
        service
            .decision_snapshot("session", Some("project"))
            .await
            .unwrap()
            .len(),
        10
    );
    service
        .write_decision(
            "session",
            "extra",
            write(json!({"expected_revision":0,"text":"x"})),
        )
        .await
        .unwrap();
    assert!(matches!(
        service.decision_snapshot("session", Some("project")).await,
        Err(Error::Conflict(_))
    ));
    for index in 0..10 {
        service
            .write_decision(
                "session",
                &format!("large-{index}"),
                write(json!({"expected_revision":1,"action":"remove"})),
            )
            .await
            .unwrap();
    }
    for index in 0..100 {
        service
            .write_decision(
                "session",
                &format!("small-{index}"),
                write(json!({"expected_revision":0,"text":"x"})),
            )
            .await
            .unwrap();
    }
    assert!(matches!(
        service.decision_snapshot("session", Some("project")).await,
        Err(Error::Conflict(_))
    ));
    assert!(
        service
            .decision_snapshot("session", None)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(matches!(
        service
            .write_decision(
                "session",
                "too-large",
                write(json!({"expected_revision":0,"text":"🦀".repeat(4001)}))
            )
            .await,
        Err(Error::Invalid(_))
    ));
    // This fixture retains a large inactive history. The complete-list method
    // must fail explicitly, while the active snapshot keeps its own semantics.
    let path = temp.path().join("nebula.db");
    let mut connection = support::raw(&path).await;
    let mut historical = support::payload(Kind::Decision);
    historical["status"] = "removed".into();
    let encoded = serde_json::to_string(&historical).unwrap();
    sqlx::query("WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<10001) INSERT INTO entities (id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT 'history-'||i,'chat_decisions','project',2,json_set(?, '$.id','history-'||i),'session','2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n")
        .bind(encoded).execute(&mut connection).await.unwrap();
    assert!(matches!(
        service.decisions("session").await,
        Err(Error::Storage(StorageError::ReadLimit))
    ));
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn paired_device_cursor_survives_reopen_without_resolving_pending_turns() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, service) = setup(&path).await;
    let mut turn = support::payload(Kind::Turn);
    turn["id"] = "pending-turn".into();
    turn["session_id"] = "session".into();
    turn["status"] = "waiting_approval".into();
    turn["approval_id"] = "still-needs-approval".into();
    store
        .apply(vec![Mutation::Create(
            StoredAssistantRecord::decode(Kind::Turn, &serde_json::to_vec(&turn).unwrap()).unwrap(),
        )])
        .await
        .unwrap();
    assert!(
        service
            .read_cursor("session", "untrusted", Some("paired-device"))
            .await
            .unwrap()
            .is_none()
    );
    let cursor=service.advance_cursor("session",cursor(json!({"expected_revision":0,"device_id":"untrusted","through_at":"2020-01-01T00:00:00Z"})),Some("paired-device")).await.unwrap();
    assert_eq!(
        cursor.payload()["id"],
        cursor_id("session", "paired-device")
    );
    assert_eq!(cursor.payload()["device_id"], "paired-device");
    assert!(
        service
            .read_cursor("session", "untrusted", None)
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        store
            .get(Kind::Turn, "pending-turn")
            .await
            .unwrap()
            .payload(),
        &turn
    );
    store.shutdown().await.unwrap();
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::new(store.clone());
    assert_eq!(
        service
            .read_cursor("session", "ignored", Some("paired-device"))
            .await
            .unwrap()
            .unwrap(),
        cursor
    );
    assert_eq!(
        store
            .get(Kind::Turn, "pending-turn")
            .await
            .unwrap()
            .payload(),
        &turn
    );
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn source_changed_after_validation_cannot_be_saved() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, service) = setup(&path).await;
    let mut connection = support::raw(&path).await;
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let pending = tokio::spawn(async move {
        service.write_decision("session","raced",write(json!({"expected_revision":0,"text":"Saved excerpt","source_message_id":"source","source_selection":"Keep β exact"}))).await
    });
    tokio::time::timeout(Duration::from_secs(3), async {
        while store.admission().available_bytes == Config::default().queued_bytes {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    sqlx::query("UPDATE entities SET revision=2, payload=json_set(payload, '$.revision',2,'$.content','Changed while queued') WHERE id='source'").execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    assert!(matches!(
        pending.await.unwrap(),
        Err(Error::Storage(StorageError::Conflict))
    ));
    assert!(matches!(
        store.get(Kind::Decision, "raced").await,
        Err(StorageError::NotFound)
    ));
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}
