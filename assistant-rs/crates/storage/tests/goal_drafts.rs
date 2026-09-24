#[allow(dead_code)]
mod support;

use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{
    Config, Error, Mutation, SqliteAssistantStore, StateClock,
};
use serde_json::{Map, Value, json, value::RawValue};
use sqlx::Connection;
use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

fn config() -> Map<String, Value> {
    json!({"objective":" revised objective ","completion_criteria":[" done "],"plan":[],"token_budget":null,"time_budget_seconds":null,"step_budget":null,"child_budget":null}).as_object().unwrap().clone()
}
fn clock(calls: Arc<AtomicUsize>, stamp: &'static str) -> Arc<StateClock> {
    Arc::new(move || {
        calls.fetch_add(1, Ordering::SeqCst);
        DateTime::parse_from_rfc3339(stamp)
            .unwrap()
            .with_timezone(&Utc)
    })
}
async fn rows(raw: &mut sqlx::SqliteConnection) -> Vec<(String, i64, String, String)> {
    sqlx::query_as("SELECT id,revision,payload,updated_at FROM entities ORDER BY id")
        .fetch_all(raw)
        .await
        .unwrap()
}
async fn search(raw: &mut sqlx::SqliteConnection) -> Vec<(String, i64, String)> {
    sqlx::query_as("SELECT id,revision,label FROM search_documents ORDER BY id")
        .fetch_all(raw)
        .await
        .unwrap()
}
fn goal() -> StoredAssistantRecord {
    let mut p = support::payload(Kind::Goal);
    p["id"] = "config-goal".into();
    p["status"] = "running".into();
    p["execution_owner_id"] = "owner".into();
    p["execution_claim_id"] = "claim".into();
    p["execution_claimed_at"] = "2026-09-23T13:00:00+01:00".into();
    p["active_since"] = "2026-09-23T13:00:00+01:00".into();
    p["elapsed_seconds"] = 123.25.into();
    StoredAssistantRecord::decode(Kind::Goal, &serde_json::to_vec(&p).unwrap()).unwrap()
}

#[tokio::test]
async fn goal_config_preserves_claims_opaque_fragments_and_reopens() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![Mutation::Create(goal())]).await.unwrap();
    let mut raw = support::raw(&path).await;
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.metadata',json(?),'$.completion_evidence',json(?),'$.skill_snapshots',json(?)) WHERE id='config-goal'")
        .bind(r#"{"z":{"last":1,"first":2},"a":"private-fixture"}"#)
        .bind(r#"[{"z":true,"a":"evidence"}]"#)
        .bind(r#"[{"z":false,"a":"skill"}]"#).execute(&mut raw).await.unwrap();
    let before: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='config-goal'")
        .fetch_one(&mut raw)
        .await
        .unwrap();
    let p: Value = serde_json::from_str(&before).unwrap();
    let before_search = search(&mut raw).await;
    let calls = Arc::new(AtomicUsize::new(0));
    let saved = store
        .patch_goal_config(
            "config-goal",
            p["revision"].to_string(),
            config(),
            clock(calls.clone(), "2030-01-01T12:00:00Z"),
        )
        .await
        .unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(saved.payload()["objective"], "revised objective");
    assert_eq!(saved.payload()["completion_criteria"], json!(["done"]));
    for (key, value) in p.as_object().unwrap() {
        if !config().contains_key(key) && !["revision", "updated_at"].contains(&key.as_str()) {
            assert_eq!(&saved.payload()[key], value, "untouched {key}");
        }
    }
    let after: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='config-goal'")
        .fetch_one(&mut raw)
        .await
        .unwrap();
    let before_fragments: HashMap<String, &RawValue> = serde_json::from_str(&before).unwrap();
    let after_fragments: HashMap<String, &RawValue> = serde_json::from_str(&after).unwrap();
    for key in ["metadata", "completion_evidence", "skill_snapshots"] {
        assert_eq!(
            before_fragments[key].get(),
            after_fragments[key].get(),
            "opaque order {key}"
        );
    }
    assert_eq!(search(&mut raw).await, before_search);
    // Historical typed keys may be normalized while opaque values retain their
    // order. Collisions keep the first normalized key position and last value.
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.metadata',json(?),'$.completion_evidence',json(?),'$.skill_snapshots',json(?)) WHERE id='config-goal'")
        .bind(r#"{" z ":{"last":1,"first":2},"a":"old","a ":"new","z":{"last":3,"first":4}}"#)
        .bind(r#"[{" z ":1,"a":2,"z":3}]"#)
        .bind(r#"[{" a ":1,"z":2,"a":3}]"#).execute(&mut raw).await.unwrap();
    let saved = store
        .patch_goal_config(
            "config-goal",
            saved.payload()["revision"].to_string(),
            config(),
            clock(calls.clone(), "2030-01-01T12:00:01Z"),
        )
        .await
        .unwrap();
    let normalized: String =
        sqlx::query_scalar("SELECT payload FROM entities WHERE id='config-goal'")
            .fetch_one(&mut raw)
            .await
            .unwrap();
    let fragments: HashMap<String, &RawValue> = serde_json::from_str(&normalized).unwrap();
    assert_eq!(
        fragments["metadata"].get(),
        r#"{"z":{"last":3,"first":4},"a":"new"}"#
    );
    assert_eq!(fragments["completion_evidence"].get(), r#"[{"z":3,"a":2}]"#);
    assert_eq!(fragments["skill_snapshots"].get(), r#"[{"a":3,"z":2}]"#);
    assert_eq!(search(&mut raw).await, before_search);
    let final_rows = rows(&mut raw).await;
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        reopened.get(Kind::Goal, "config-goal").await.unwrap(),
        saved
    );
    assert_eq!(rows(&mut raw).await, final_rows);
    reopened.shutdown().await.unwrap();
    raw.close().await.unwrap();
}

#[tokio::test]
async fn goal_config_checks_revision_and_hydration_before_writer_clock() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![Mutation::Create(goal())]).await.unwrap();
    let mut raw = support::raw(&path).await;
    let original = goal().payload().clone();
    let revision = original["revision"].to_string();
    let calls = Arc::new(AtomicUsize::new(0));
    let early = clock(calls.clone(), "2020-01-01T00:00:00Z");
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.execution_claim_id',null) WHERE id='config-goal'").execute(&mut raw).await.unwrap();
    let before = rows(&mut raw).await;
    let before_search = search(&mut raw).await;
    assert!(matches!(
        store
            .patch_goal_config("config-goal", "999".into(), config(), early.clone())
            .await,
        Err(Error::SettingsRevisionConflict { .. })
    ));
    assert!(matches!(
        store
            .patch_goal_config("config-goal", revision.clone(), config(), early.clone())
            .await,
        Err(Error::Record(_))
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert_eq!(rows(&mut raw).await, before);
    assert_eq!(search(&mut raw).await, before_search);
    sqlx::query("UPDATE entities SET payload=? WHERE id='config-goal'")
        .bind(serde_json::to_string(&original).unwrap())
        .execute(&mut raw)
        .await
        .unwrap();
    let before = rows(&mut raw).await;
    let Err(Error::RetainedModelValidation(report)) = store
        .patch_goal_config("config-goal", revision, config(), early)
        .await
    else {
        panic!("expected model-after failure")
    };
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let errors = serde_json::to_value(report).unwrap();
    assert_eq!(errors[0]["loc"], json!([]));
    assert_eq!(
        errors[0]["msg"],
        "Value error, updated_at cannot be earlier than created_at"
    );
    assert_eq!(rows(&mut raw).await, before);
    assert_eq!(search(&mut raw).await, before_search);
    assert_eq!(
        store.admission().available_bytes,
        Config::default().queued_bytes
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn goal_config_rejects_lifecycle_fields_and_concurrent_stale_updates() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![Mutation::Create(goal())]).await.unwrap();
    let revision = goal().payload()["revision"].to_string();
    let calls = Arc::new(AtomicUsize::new(0));
    let now = clock(calls.clone(), "2030-01-01T12:00:00Z");
    for key in [
        "status",
        "usage",
        "elapsed_seconds",
        "execution_claim_id",
        "metadata",
    ] {
        let mut changes = config();
        changes.insert(key.into(), Value::Null);
        assert!(matches!(
            store
                .patch_goal_config("config-goal", revision.clone(), changes, now.clone())
                .await,
            Err(Error::ProtectedField)
        ));
    }
    assert!(matches!(
        store
            .patch_goal_config("config-goal", revision.clone(), Map::new(), now.clone())
            .await,
        Err(Error::ProtectedField)
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    let (a, b) = tokio::join!(
        store.patch_goal_config("config-goal", revision.clone(), config(), now.clone()),
        store.patch_goal_config("config-goal", revision, config(), now),
    );
    assert_eq!(a.is_ok() as usize + b.is_ok() as usize, 1);
    assert!(matches!(
        a.err().or_else(|| b.err()).unwrap(),
        Error::SettingsRevisionConflict { .. }
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    store.shutdown().await.unwrap();
}
