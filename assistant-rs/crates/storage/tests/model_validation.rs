#[allow(dead_code)]
mod support;

use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, RecordError, StoredAssistantRecord};
use nebula_assistant_storage::entities::{
    Config, Error, Mutation, SqliteAssistantStore, StateClock,
};
use serde_json::{Map, Value, json};
use sqlx::Connection;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

fn record(kind: Kind, id: &str, session: &str) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    if kind == Kind::Schedule {
        p["session_id"] = session.into();
    }
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn changes(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}
fn report(error: Error) -> Value {
    match error {
        Error::RetainedModelValidation(report) => serde_json::to_value(report).unwrap(),
        error => panic!("expected direct model validation, received {error:?}"),
    }
}
async fn entity_rows(raw: &mut sqlx::SqliteConnection) -> Vec<(String, i64, String, String)> {
    sqlx::query_as("SELECT id,revision,payload,updated_at FROM entities ORDER BY id")
        .fetch_all(raw)
        .await
        .unwrap()
}
async fn search_rows(raw: &mut sqlx::SqliteConnection) -> Vec<(String, i64, String)> {
    sqlx::query_as("SELECT id,revision,label FROM search_documents ORDER BY id")
        .fetch_all(raw)
        .await
        .unwrap()
}

#[tokio::test]
async fn schedule_validation_preserves_direct_wrapped_root_and_candidate_order() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let config = Config::default();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    store
        .apply(vec![
            Mutation::Create(record(Kind::Session, "validation", "")),
            Mutation::Create(record(Kind::Schedule, "a-schedule", "validation")),
            Mutation::Create(record(Kind::Schedule, "z-schedule", "validation")),
            Mutation::Create(record(Kind::Schedule, "unrelated", "other-session")),
        ])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.next_run_at','2030-01-01T12:00:00') WHERE id='unrelated'")
        .execute(&mut raw).await.unwrap();
    assert_eq!(
        store
            .session_plans_snapshot(Kind::Schedule, "validation")
            .await
            .unwrap()
            .records
            .len(),
        2
    );
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.interval_seconds',1) WHERE id='a-schedule'")
        .execute(&mut raw).await.unwrap();
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.next_run_at','2030-01-01T12:00:00') WHERE id='z-schedule'")
        .execute(&mut raw).await.unwrap();
    let first = report(
        store
            .session_plans_snapshot(Kind::Schedule, "validation")
            .await
            .unwrap_err(),
    );
    assert_eq!(
        first,
        json!([{"type":"greater_than_equal","loc":["interval_seconds"],"msg":"Input should be greater than or equal to 3600","input":1,"ctx":{"ge":3600}}])
    );
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.interval_seconds',3600) WHERE id='a-schedule'")
        .execute(&mut raw).await.unwrap();
    let before = entity_rows(&mut raw).await;
    let error = store
        .session_plans_snapshot(Kind::Schedule, "validation")
        .await
        .unwrap_err();
    assert_eq!(
        report(error),
        json!([{"type":"value_error","loc":["next_run_at"],"msg":"Value error, schedule times must include a timezone","input":"2030-01-01T12:00:00","ctx":{"error":{}}}])
    );
    assert!(matches!(
        store.get(Kind::Schedule, "z-schedule").await,
        Err(Error::Record(_))
    ));
    assert!(matches!(
        store
            .session_plans_snapshot(Kind::Schedule, "missing")
            .await,
        Err(Error::NotFound)
    ));
    assert_eq!(entity_rows(&mut raw).await, before);
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.provider_profile_id',null) WHERE id='validation'")
        .execute(&mut raw).await.unwrap();
    let before = entity_rows(&mut raw).await;
    assert!(
        matches!(
            store
                .session_plans_snapshot(Kind::Schedule, "validation")
                .await,
            Err(Error::WrappedRecord(_))
        ),
        "wrapped root validation precedes direct candidates"
    );
    assert_eq!(entity_rows(&mut raw).await, before);
    assert_eq!(store.admission().available_reads, config.read_capacity);
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn schedule_validation_keeps_integrity_limits_and_report_debug_separate() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let config = Config::default();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    store
        .apply(vec![
            Mutation::Create(record(Kind::Session, "validation", "")),
            Mutation::Create(record(Kind::Schedule, "schedule", "validation")),
        ])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    let original: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='schedule'")
        .fetch_one(&mut raw)
        .await
        .unwrap();
    sqlx::query("UPDATE entities SET revision=99 WHERE id='schedule'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        store
            .session_plans_snapshot(Kind::Schedule, "validation")
            .await,
        Err(Error::CorruptEnvelope)
    ));
    sqlx::query("UPDATE entities SET revision=2,payload='{' WHERE id='schedule'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        store
            .session_plans_snapshot(Kind::Schedule, "validation")
            .await,
        Err(Error::Record(RecordError::Json))
    ));
    sqlx::query("UPDATE entities SET payload=json_set(?,'$.provider_profile_id',123,'$.next_run_at','2030-01-01T12:00:00','$.private_marker','PRIVATE-VALIDATION-CONTENT') WHERE id='schedule'")
        .bind(&original).execute(&mut raw).await.unwrap();
    let error = store
        .session_plans_snapshot(Kind::Schedule, "validation")
        .await
        .unwrap_err();
    assert!(!format!("{error:?} {error}").contains("PRIVATE-VALIDATION-CONTENT"));
    let errors = report(error);
    assert_eq!(
        errors
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["loc"].clone())
            .collect::<Vec<_>>(),
        vec![
            json!(["provider_profile_id"]),
            json!(["next_run_at"]),
            json!(["private_marker"])
        ]
    );
    assert_eq!(
        errors[2]["input"], "PRIVATE-VALIDATION-CONTENT",
        "wire errors retain source input despite redacted diagnostics"
    );
    // A report shares its entire retained input. Charge it alongside the
    // already-read rows instead of allowing a second unbudgeted large owner.
    sqlx::query("UPDATE entities SET payload=json_set(?,'$.model',?,'$.next_run_at','2030-01-01T12:00:00') WHERE id='schedule'")
        .bind(&original).bind("x".repeat(9*1024*1024)).execute(&mut raw).await.unwrap();
    let before = entity_rows(&mut raw).await;
    assert!(matches!(
        store
            .session_plans_snapshot(Kind::Schedule, "validation")
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(entity_rows(&mut raw).await, before);
    sqlx::query("UPDATE entities SET payload=? WHERE id='schedule'")
        .bind(&original)
        .execute(&mut raw)
        .await
        .unwrap();
    assert_eq!(
        store
            .session_plans_snapshot(Kind::Schedule, "validation")
            .await
            .unwrap()
            .records
            .len(),
        1
    );
    assert_eq!(store.admission().available_reads, config.read_capacity);
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn schedule_writer_validates_after_revision_and_rolls_back_entity_and_search() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let config = Config::default();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    store
        .apply(vec![Mutation::Create(record(
            Kind::Schedule,
            "schedule",
            "validation",
        ))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    sqlx::query("INSERT INTO search_documents(id,project_id,resource_kind,resource_id,revision,label,description,breadcrumb,content,updated_at) VALUES ('schedule','project','conversation','schedule',2,'retain-on-failure','','Workbench','','2026-09-23 12:01:00.000000')")
        .execute(&mut raw).await.unwrap();
    let original: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='schedule'")
        .fetch_one(&mut raw)
        .await
        .unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let observed = calls.clone();
    let early: Arc<StateClock> = Arc::new(move || {
        observed.fetch_add(1, Ordering::SeqCst);
        DateTime::parse_from_rfc3339("2020-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    });
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.next_run_at','2030-01-01T12:00:00') WHERE id='schedule'")
        .execute(&mut raw).await.unwrap();
    let before = entity_rows(&mut raw).await;
    let before_search = search_rows(&mut raw).await;
    assert!(matches!(
        store
            .patch_schedule("schedule", "1".into(), Map::new(), early.clone())
            .await,
        Err(Error::SettingsRevisionConflict { found: 2, .. })
    ));
    assert!(matches!(
        store
            .patch_schedule("schedule", "2".into(), Map::new(), early.clone())
            .await,
        Err(Error::Record(_))
    ));
    assert_eq!(
        calls.load(Ordering::SeqCst),
        0,
        "revision and current hydration precede writer clock"
    );
    assert_eq!(entity_rows(&mut raw).await, before);
    assert_eq!(search_rows(&mut raw).await, before_search);
    sqlx::query("UPDATE entities SET payload=? WHERE id='schedule'")
        .bind(&original)
        .execute(&mut raw)
        .await
        .unwrap();
    let before = entity_rows(&mut raw).await;
    let errors = report(
        store
            .patch_schedule(
                "schedule",
                "2".into(),
                changes(json!({"enabled":false})),
                early,
            )
            .await
            .unwrap_err(),
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(errors[0]["loc"], json!([]));
    assert_eq!(
        errors[0]["msg"],
        "Value error, updated_at cannot be earlier than created_at"
    );
    assert_eq!(errors[0]["ctx"], json!({"error":{}}));
    assert_eq!(
        errors[0]["input"]["created_at"],
        "2026-09-23T12:00:00+00:00"
    );
    assert_eq!(
        errors[0]["input"]["updated_at"],
        "2020-01-01T00:00:00+00:00"
    );
    assert_eq!(
        errors[0]["input"]["next_run_at"],
        "2026-09-23T12:01:00+00:00"
    );
    assert_eq!(errors[0]["input"]["revision"], 3);
    assert_eq!(entity_rows(&mut raw).await, before);
    assert_eq!(search_rows(&mut raw).await, before_search);
    let future: Arc<StateClock> = Arc::new(|| {
        DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    });
    let errors = report(
        store
            .patch_schedule(
                "schedule",
                "2".into(),
                changes(json!({"paused_by":"bad"})),
                future.clone(),
            )
            .await
            .unwrap_err(),
    );
    assert_eq!(errors[0]["loc"], json!(["paused_by"]));
    assert_eq!(errors[0]["input"], "bad");
    assert_eq!(entity_rows(&mut raw).await, before);
    assert_eq!(search_rows(&mut raw).await, before_search);
    let saved = store
        .patch_schedule(
            "schedule",
            "2".into(),
            changes(json!({"enabled":false})),
            future,
        )
        .await
        .unwrap();
    assert_eq!(saved.payload()["revision"], 3);
    assert!(
        !search_rows(&mut raw)
            .await
            .iter()
            .any(|(id, _, _)| id == "schedule")
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    assert_eq!(
        store
            .get(Kind::Schedule, "schedule")
            .await
            .unwrap()
            .payload(),
        saved.payload()
    );
    assert_eq!(store.admission().available_reads, config.read_capacity);
    assert_eq!(store.admission().available_bytes, config.queued_bytes);
    store.shutdown().await.unwrap();
}
