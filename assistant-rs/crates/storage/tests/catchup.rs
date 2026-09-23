#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{Config, Error, Mutation, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::Connection;

fn record(kind: Kind, id: &str, changes: Value) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "snapshot-project".into();
    if kind != Kind::Session {
        p["session_id"] = "snapshot".into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn at(value: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value)
        .unwrap()
        .with_timezone(&Utc)
}

#[tokio::test]
async fn catchup_snapshot_preserves_distinct_history_and_pending_scopes_without_writes() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let turn = record(
        Kind::Turn,
        "turn-owned",
        json!({"final_message_id":"answer","harness_turn_id":"harness"}),
    );
    let fallback = record(
        Kind::Turn,
        "turn-foreign",
        json!({"engagement_id":"other","final_message_id":"wrong-session-answer"}),
    );
    store.apply(vec![
        Mutation::Create(record(Kind::Session,"snapshot",json!({"metadata":{}}))),
        Mutation::Create(record(Kind::Message,"prompt",json!({"role":"user","sequence":1,"content":"Read the saved result"}))),
        Mutation::Create(record(Kind::Message,"answer",json!({"role":"assistant","sequence":2,"content":"Retained result","created_at":"2026-09-23T12:01:00Z"}))),
        Mutation::Create(record(Kind::Message,"retracted",json!({"role":"assistant","sequence":3,"metadata":{"retracted_at":"retained"},"created_at":"2026-09-23T12:01:00Z"}))),
        Mutation::Create(record(Kind::Message,"wrong-session-answer",json!({"session_id":"other","role":"assistant","sequence":4}))),
        Mutation::Create(turn.clone()), Mutation::Create(fallback.clone()),
    ]).await.unwrap();
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-catchup.json")).unwrap();
    let mut raw = support::raw(&path).await;
    for (kind, id) in [
        ("approvals", "approval"),
        ("harness_turns", "harness"),
        ("harness_interactions", "question"),
    ] {
        let mut p = fixture["dependency_records"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["kind"] == kind)
            .unwrap()["payload"]
            .clone();
        p["id"] = id.into();
        p["engagement_id"] = "snapshot-project".into();
        p["chat_session_id"] = "snapshot".into();
        if kind == "approvals" {
            p["chat_turn_id"] = "turn-owned".into();
        } else {
            p["origin"] = "chat".into();
            p["run_id"] = Value::Null;
        }
        if kind == "harness_turns" {
            p["chat_turn_id"] = "turn-owned".into();
        }
        if kind == "harness_interactions" {
            p["harness_turn_id"] = "harness".into();
        }
        let sqltime = |v: &Value| {
            at(v.as_str().unwrap())
                .format("%Y-%m-%d %H:%M:%S%.6f")
                .to_string()
        };
        sqlx::query("INSERT INTO entities (id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,'snapshot-project',?,?,'snapshot',?,?)")
            .bind(id).bind(kind).bind(p["revision"].as_i64()).bind(serde_json::to_string(&p).unwrap())
            .bind(sqltime(&p["created_at"])).bind(sqltime(&p["updated_at"]))
            .execute(&mut raw).await.unwrap();
    }
    let before: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut raw)
        .await
        .unwrap();
    let snapshot = store
        .catchup_snapshot(
            "snapshot",
            "snapshot-project",
            Some(at("2026-09-23T11:00:00Z").fixed_offset()),
            at("2026-09-23T13:00:00Z"),
        )
        .await
        .unwrap();
    assert_eq!(snapshot.turns.len(), 2);
    assert_eq!(snapshot.pending.turns.len(), 1);
    assert_eq!(snapshot.pending.approvals.len(), 1);
    assert_eq!(snapshot.pending.questions.len(), 1);
    assert_eq!(snapshot.pending.harnesses.len(), 1);
    assert_eq!(
        snapshot.sources["turn-owned"].as_ref().unwrap().payload()["id"],
        "answer"
    );
    assert_eq!(
        snapshot.sources["turn-foreign"].as_ref().unwrap().payload()["id"],
        "prompt"
    );
    assert_eq!(snapshot.messages.len(), 2);
    assert!(snapshot.messages.iter().any(|m| m.is_replaced_message()));
    assert_eq!(snapshot.prompts.len(), 1);
    assert_eq!(snapshot.prompts["answer"], "Read the saved result");
    // Legacy SQLite binds cursor wall-clock fields, while failure/pending
    // comparisons in services continue to compare the represented instant.
    for (cursor, count) in [
        ("2026-09-23T13:00:00+02:00", 0),
        ("2026-09-23T11:00:00-02:00", 2),
    ] {
        let offset = store
            .catchup_snapshot(
                "snapshot",
                "snapshot-project",
                Some(DateTime::parse_from_rfc3339(cursor).unwrap()),
                at("2026-09-23T14:00:00Z"),
            )
            .await
            .unwrap();
        assert_eq!(offset.messages.len(), count, "{cursor}");
    }
    assert_eq!(
        store
            .source_message("snapshot", &fallback)
            .await
            .unwrap()
            .unwrap()
            .payload()["id"],
        "prompt"
    );
    let after: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut raw)
        .await
        .unwrap();
    assert_eq!(before, after);
    sqlx::query("UPDATE entities SET revision=999 WHERE id='approval'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        store
            .catchup_snapshot(
                "snapshot",
                "snapshot-project",
                None,
                at("2026-09-23T13:00:00Z")
            )
            .await,
        Err(Error::CorruptEnvelope)
    ));
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn catchup_snapshot_budget_is_shared_across_components_and_releases_its_reader() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(record(
            Kind::Session,
            "snapshot",
            json!({"metadata":{}}),
        ))])
        .await
        .unwrap();
    // One 9 MiB turn appears in both history and pending ownership. The total
    // retained snapshot must fail, although each component alone fits.
    store
        .apply(vec![Mutation::Create(record(
            Kind::Turn,
            "large",
            json!({"final_message_id":null,"request_snapshot":{"opaque":"x".repeat(9*1024*1024)}}),
        ))])
        .await
        .unwrap();
    assert!(matches!(
        store
            .catchup_snapshot(
                "snapshot",
                "snapshot-project",
                None,
                at("2026-09-23T13:00:00Z")
            )
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert!(store.get(Kind::Session, "snapshot").await.is_ok());
    store.shutdown().await.unwrap();
}
