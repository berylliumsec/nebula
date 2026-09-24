#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    records::{AssistantKind as Kind, StoredAssistantRecord},
};
use nebula_assistant_storage::entities::{
    Config, Error, Mutation, SqliteAssistantStore, StateClock,
};
use serde_json::{Map, Value, json};
use sqlx::Connection;
use std::{
    sync::{
        Arc,
        atomic::{AtomicI64, Ordering},
    },
    time::Duration,
};

fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:05Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn clock() -> Arc<StateClock> {
    Arc::new(now)
}
fn changes(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}
fn record(kind: Kind, id: &str) -> StoredAssistantRecord {
    support::record(kind, id)
}
fn profile(id: &str, enabled: bool) -> StoredDependency {
    let p = json!({"id":id,"revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z","name":"fixture","transport":"streamable_http","url":"https://mcp.invalid/rpc","enabled":enabled});
    StoredDependency::decode(
        DependencyKind::McpServerProfile,
        &serde_json::to_vec(&p).unwrap(),
    )
    .unwrap()
}
async fn insert_profile(raw: &mut sqlx::SqliteConnection, profile: &StoredDependency) {
    let p = profile.payload();
    sqlx::query("INSERT INTO entities(id,kind,revision,payload,created_at,updated_at) VALUES (?,'mcp_servers',1,?,'2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')")
        .bind(p["id"].as_str()).bind(serde_json::to_string(p).unwrap()).execute(raw).await.unwrap();
}
async fn row(raw: &mut sqlx::SqliteConnection, id: &str) -> (i64, String) {
    sqlx::query_as("SELECT revision,payload FROM entities WHERE id=?")
        .bind(id)
        .fetch_one(raw)
        .await
        .unwrap()
}

#[tokio::test]
async fn settings_reads_preserve_mcp_request_order_deferred_errors_and_scope() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(record(Kind::Session, "settings"))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    for (id, enabled) in [
        ("disabled", false),
        ("enabled", true),
        ("bad", true),
        ("unselected", true),
    ] {
        insert_profile(&mut raw, &profile(id, enabled)).await;
    }
    sqlx::query("UPDATE entities SET revision=999 WHERE id IN ('bad','unselected')")
        .execute(&mut raw)
        .await
        .unwrap();
    let before: Vec<(String, i64, String)> =
        sqlx::query_as("SELECT id,revision,payload FROM entities ORDER BY id")
            .fetch_all(&mut raw)
            .await
            .unwrap();
    let ids = [
        "disabled".into(),
        "bad".into(),
        "enabled".into(),
        "missing".into(),
        "settings".into(),
        "".into(),
        "x".repeat(201),
        "enabled".into(),
    ];
    let rows = store.mcp_profiles_snapshot(&ids).await.unwrap();
    assert_eq!(
        rows.iter().map(|row| &row.id).collect::<Vec<_>>(),
        ids.iter().collect::<Vec<_>>()
    );
    assert_eq!(
        rows[0].record.as_ref().unwrap().as_ref().unwrap().payload()["enabled"],
        false
    );
    assert!(matches!(rows[1].record, Err(Error::CorruptEnvelope)));
    assert_eq!(
        rows[2].record.as_ref().unwrap().as_ref().unwrap().payload()["enabled"],
        true
    );
    assert!(
        rows[3..7]
            .iter()
            .all(|row| row.record.as_ref().unwrap().is_none())
    );
    assert!(rows[7].record.as_ref().unwrap().is_some());
    assert!(store.mcp_profiles_snapshot(&[]).await.unwrap().is_empty());
    assert!(matches!(
        store
            .mcp_profiles_snapshot(&vec!["enabled".into(); 65])
            .await,
        Err(Error::InvalidBounds)
    ));
    assert!(matches!(
        store.settings_session("missing").await,
        Err(Error::NotFound)
    ));
    let after: Vec<(String, i64, String)> =
        sqlx::query_as("SELECT id,revision,payload FROM entities ORDER BY id")
            .fetch_all(&mut raw)
            .await
            .unwrap();
    assert_eq!(after, before);
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn settings_patch_preserves_initial_metadata_order_and_fresh_revision_precedence() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(record(Kind::Session, "settings"))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.metadata',json(?)) WHERE id='settings'")
        .bind(r#"{"provider_subagent":{"provider_profile_id":{"z":1,"a":2},"model":"saved","max_active":{"y":1,"b":2}},"opaque":{"z":1,"a":2},"private":"do-not-print"}"#).execute(&mut raw).await.unwrap();
    let initial = store.settings_session("settings").await.unwrap();
    assert_eq!(initial.record.payload()["revision"], 2);
    assert!(!format!("{initial:?}").contains("do-not-print"));
    sqlx::query("UPDATE entities SET revision=3,payload=json_set(payload,'$.revision',3,'$.model','newer-model','$.metadata.opaque',json('{\"a\":2,\"z\":1}')) WHERE id='settings'").execute(&mut raw).await.unwrap();
    let saved = store
        .patch_session_settings(
            "settings",
            "3".into(),
            changes(json!({"title":"Updated","metadata":initial.record.payload()["metadata"]})),
            initial.raw_payload,
            clock(),
        )
        .await
        .unwrap();
    assert_eq!(saved.record.payload()["revision"], 4);
    assert_eq!(saved.record.payload()["model"], "newer-model");
    assert!(saved.raw_payload.contains(r#""opaque":{"z":1,"a":2}"#));
    assert!(
        saved
            .raw_payload
            .contains(r#""provider_profile_id":{"z":1,"a":2}"#)
    );
    let search: (String, String, i64) = sqlx::query_as(
        "SELECT label,description,revision FROM search_documents WHERE id='settings'",
    )
    .fetch_one(&mut raw)
    .await
    .unwrap();
    assert_eq!(search, ("Updated".into(), "newer-model".into(), 4));
    let mut metadata = saved.record.payload()["metadata"].clone();
    metadata["provider_subagent"] = json!({"provider_profile_id":"rendered","model":"saved","max_active":metadata["provider_subagent"]["max_active"]});
    let saved = store
        .patch_session_settings(
            "settings",
            "4".into(),
            changes(json!({"metadata":metadata})),
            saved.raw_payload,
            clock(),
        )
        .await
        .unwrap();
    assert!(saved.raw_payload.contains(r#""max_active":{"y":1,"b":2}"#));
    let before = row(&mut raw, "settings").await;
    assert!(matches!(
        store
            .patch_session_settings(
                "settings",
                "4".into(),
                changes(json!({"metadata":{}})),
                saved.raw_payload.clone(),
                clock()
            )
            .await,
        Err(Error::SettingsRevisionConflict { found: 5, .. })
    ));
    assert_eq!(row(&mut raw, "settings").await, before);
    sqlx::query("UPDATE entities SET payload='{}' WHERE id='settings'")
        .execute(&mut raw)
        .await
        .unwrap();
    let expected = "9".repeat(400);
    let error = store
        .patch_session_settings(
            "settings",
            expected.clone(),
            changes(json!({"metadata":{}})),
            saved.raw_payload.clone(),
            clock(),
        )
        .await
        .unwrap_err();
    assert!(
        matches!(error,Error::SettingsRevisionConflict{expected:observed,found:5} if observed==expected)
    );
    assert!(matches!(
        store
            .patch_session_settings(
                "settings",
                "5".into(),
                changes(json!({"metadata":{}})),
                saved.raw_payload,
                clock()
            )
            .await,
        Err(Error::Record(_))
    ));
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn schedule_configuration_patches_are_narrow_and_clear_stale_search_entries() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(record(
            Kind::Schedule,
            "settings-schedule",
        ))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    sqlx::query("INSERT INTO search_documents(id,project_id,resource_kind,resource_id,revision,label,description,breadcrumb,content,updated_at) VALUES ('settings-schedule','project','conversation','settings-schedule',2,'stale','','Workbench','','2026-09-23 12:01:00.000000')").execute(&mut raw).await.unwrap();
    let saved = store
        .patch_schedule("settings-schedule", "2".into(), Map::new(), clock())
        .await
        .unwrap();
    assert_eq!(saved.payload()["revision"], 3);
    assert_eq!(saved.payload()["updated_at"], "2030-01-01T12:00:05Z");
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM search_documents WHERE id='settings-schedule'")
            .fetch_one(&mut raw)
            .await
            .unwrap();
    assert_eq!(count, 0);
    let before = row(&mut raw, "settings-schedule").await;
    assert!(matches!(
        store
            .patch_schedule(
                "settings-schedule",
                "3".into(),
                changes(json!({"interval_seconds":7200})),
                clock()
            )
            .await,
        Err(Error::ProtectedField)
    ));
    assert!(matches!(
        store
            .patch_schedule("settings-schedule", "2".into(), Map::new(), clock())
            .await,
        Err(Error::SettingsRevisionConflict { found: 3, .. })
    ));
    assert!(matches!(
        store
            .patch_schedule("missing", "1".into(), Map::new(), clock())
            .await,
        Err(Error::NotFound)
    ));
    assert_eq!(row(&mut raw, "settings-schedule").await, before);
    let saved=store.patch_schedule("settings-schedule","3".into(),changes(json!({"enabled":true,"paused_by":null,"skip_reason":null,"next_run_at":"2030-01-01T15:00:00+02:00"})),clock()).await.unwrap();
    assert_eq!(saved.payload()["next_run_at"], "2030-01-01T13:00:00Z");
    assert!(matches!(
        store
            .patch_schedule(
                "settings-schedule",
                "4".into(),
                changes(json!({"paused_by":"invalid"})),
                clock()
            )
            .await,
        Err(Error::RetainedModelValidation(_))
    ));
    assert_eq!(row(&mut raw, "settings-schedule").await.0, 4);
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn settings_writes_resample_clock_drain_cancellation_and_enforce_aggregate_limits() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let config = Config::default();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    store
        .apply(vec![Mutation::Create(record(Kind::Session, "settings"))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    let initial = store.settings_session("settings").await.unwrap();
    let lock = raw.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let time = Arc::new(AtomicI64::new(now().timestamp()));
    let sampled = time.clone();
    let observed: Arc<StateClock> =
        Arc::new(move || DateTime::from_timestamp(sampled.load(Ordering::SeqCst), 0).unwrap());
    let pending = {
        let store = store.clone();
        tokio::spawn(async move {
            store
                .patch_session_settings(
                    "settings",
                    "2".into(),
                    changes(json!({"title":"After cancellation","metadata":{}})),
                    initial.raw_payload,
                    observed,
                )
                .await
        })
    };
    tokio::time::timeout(Duration::from_secs(2), async {
        while store.admission().available_bytes == config.queued_bytes {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    time.store(now().timestamp() + 30, Ordering::SeqCst);
    pending.abort();
    assert!(pending.await.unwrap_err().is_cancelled());
    lock.commit().await.unwrap();
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    let mut raw = support::raw(&path).await;
    let saved = store.settings_session("settings").await.unwrap();
    assert_eq!(saved.record.payload()["revision"], 3);
    assert_eq!(saved.record.payload()["updated_at"], "2030-01-01T12:00:35Z");
    let before = row(&mut raw, "settings").await;
    let huge = changes(json!({"metadata":{"large":"x".repeat(9*1024*1024)}}));
    assert!(matches!(
        store
            .patch_session_settings("settings", "3".into(), huge, saved.raw_payload, clock())
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(row(&mut raw, "settings").await, before);
    insert_profile(&mut raw, &profile("large", true)).await;
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.metadata.large',?) WHERE id IN ('settings','large')").bind("x".repeat(9*1024*1024)).execute(&mut raw).await.unwrap();
    assert!(matches!(
        store.settings_session("settings").await,
        Err(Error::ReadLimit)
    ));
    assert!(matches!(
        store
            .mcp_profiles_snapshot(&["large".into(), "large".into()])
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(store.admission().available_reads, config.read_capacity);
    assert_eq!(store.admission().available_bytes, config.queued_bytes);
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}
