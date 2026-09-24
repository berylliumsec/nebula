#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    records::{AssistantKind as Kind, StoredAssistantRecord},
};
use nebula_assistant_storage::entities::{Config, Error, Mutation, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};

fn record(kind: Kind, id: &str, changes: Value) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "status-project".into();
    if p.get("metadata").is_some() {
        p["metadata"] = json!({});
    }
    if kind != Kind::Session {
        p["session_id"] = "status".into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn hook(id: &str, changes: Value) -> Value {
    let mut p = json!({"id":id,"revision":1,"created_at":"2026-09-23T12:00:00Z","updated_at":"2026-09-23T12:00:00Z",
        "engagement_id":"status-project","chat_session_id":"missing-parent","chat_turn_id":"hook-turn",
        "hook_id":"retained","hook_snapshot":{},"event_name":"chat.turn.completed","started_at":"2026-09-23T12:00:00Z"});
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredDependency::decode(
        DependencyKind::NativeHookExecution,
        &serde_json::to_vec(&p).unwrap(),
    )
    .unwrap()
    .payload()
    .clone()
}
async fn insert_hook(raw: &mut SqliteConnection, p: &Value) {
    let time = |field: &str| {
        DateTime::parse_from_rfc3339(p[field].as_str().unwrap())
            .unwrap()
            .with_timezone(&Utc)
            .format("%Y-%m-%d %H:%M:%S%.6f")
            .to_string()
    };
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,'native_hook_executions',?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
        .bind(serde_json::to_string(p).unwrap()).bind(p["chat_session_id"].as_str()).bind(time("created_at")).bind(time("updated_at"))
        .execute(raw).await.unwrap();
}
fn ids(records: &[StoredAssistantRecord]) -> Vec<&str> {
    records
        .iter()
        .map(|r| r.payload()["id"].as_str().unwrap())
        .collect()
}

#[tokio::test]
async fn status_snapshots_preserve_activity_queue_and_hook_read_contracts() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![
        Mutation::Create(record(Kind::Session,"status",json!({}))),
        Mutation::Create(record(Kind::Session,"a-visible",json!({"metadata":{"temporary_assistant":0,"subagent_id":""}}))),
        Mutation::Create(record(Kind::Session,"temporary",json!({"metadata":{"temporary_assistant":true}}))),
        Mutation::Create(record(Kind::Session,"hidden-string",json!({"metadata":{"temporary_assistant":"false"}}))),
        Mutation::Create(record(Kind::Session,"foreign",json!({"engagement_id":"other"}))),
        Mutation::Create(record(Kind::Turn,"a-hidden",json!({"session_id":"temporary","status":"waiting_approval"}))),
        Mutation::Create(record(Kind::Turn,"b-orphan",json!({"session_id":"missing","status":"interrupted","request_snapshot":{"recovery":{"automatic_retry_pending":true}}}))),
        Mutation::Create(record(Kind::Turn,"c-callback",json!({"status":"waiting_callback"}))),
        Mutation::Create(record(Kind::Turn,"d-finalizing",json!({"status":"finalizing"}))),
        Mutation::Create(record(Kind::Turn,"excluded-complete",json!({"status":"complete"}))),
        Mutation::Create(record(Kind::Turn,"excluded-project",json!({"engagement_id":"other"}))),
        Mutation::Create(record(Kind::Turn,"hook-turn",json!({"session_id":"missing-parent"}))),
        Mutation::Create(record(Kind::Message,"chat-queue-a-visible",json!({}))),
    ]).await.unwrap();
    let mut raw = support::raw(&path).await;
    let missing = store.queue_snapshot("status").await.unwrap();
    assert!(missing.queue.is_none());
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM entities WHERE id='chat-queue-status'")
            .fetch_one(&mut raw)
            .await
            .unwrap(),
        0
    );
    let saved = record(
        Kind::Queue,
        "chat-queue-status",
        json!({"engagement_id":"legacy-project","session_id":"different-session","paused":true,"items":[{"status":"needs_review","opaque":{"retained":true}},{"status":"complete"}]}),
    );
    store
        .apply(vec![Mutation::Create(saved.clone())])
        .await
        .unwrap();
    for (id, changes) in [
        (
            "a-other-turn",
            json!({"chat_turn_id":"different-turn","started_at":"2026-09-24T12:00:00Z"}),
        ),
        (
            "z-matching",
            json!({"engagement_id":"foreign-project","owner_kind":"mission","started_at":"2026-09-22T12:00:00"}),
        ),
        ("b-other-session", json!({"chat_session_id":"other"})),
    ] {
        insert_hook(&mut raw, &hook(id, changes)).await;
    }
    let before: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut raw)
        .await
        .unwrap();
    let activity = store.activity_snapshot("status-project").await.unwrap();
    assert_eq!(ids(&activity.sessions.unwrap()), ["a-visible", "status"]);
    assert_eq!(
        ids(&activity.unfinished_turns),
        [
            "a-hidden",
            "b-orphan",
            "c-callback",
            "d-finalizing",
            "hook-turn"
        ]
    );
    assert_eq!(
        store.queue_snapshot("status").await.unwrap().queue.unwrap(),
        saved
    );
    assert!(
        store
            .queue_snapshot("a-visible")
            .await
            .unwrap()
            .queue
            .is_none()
    );
    assert!(matches!(
        store.queue_snapshot("missing").await,
        Err(Error::NotFound)
    ));
    let hooks = store.turn_hooks_snapshot("hook-turn").await.unwrap();
    assert_eq!(
        hooks
            .hooks
            .iter()
            .map(|h| h.payload()["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["a-other-turn", "z-matching"]
    );
    assert_eq!(hooks.hooks[1].payload()["engagement_id"], "foreign-project");
    assert!(matches!(
        store.turn_hooks_snapshot("status").await,
        Err(Error::NotFound)
    ));
    assert!(
        store
            .activity_snapshot("absent")
            .await
            .unwrap()
            .sessions
            .unwrap()
            .is_empty()
    );
    let after: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut raw)
        .await
        .unwrap();
    assert_eq!(before, after);
    // Unrelated turn records in the selected session are validated first.
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.status','invalid') WHERE id='a-other-turn'").execute(&mut raw).await.unwrap();
    assert!(matches!(
        store.turn_hooks_snapshot("hook-turn").await,
        Err(Error::Record(_))
    ));
    // A session read failure is deferred until after service pending validation.
    sqlx::query("UPDATE entities SET revision=999 WHERE id='status'")
        .execute(&mut raw)
        .await
        .unwrap();
    let activity = store.activity_snapshot("status-project").await.unwrap();
    assert_eq!(activity.unfinished_turns.len(), 5);
    assert!(matches!(activity.sessions, Err(Error::CorruptEnvelope)));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    for (index, project) in [String::new(), "β".repeat(201)].into_iter().enumerate() {
        let session = format!("legacy-{index}");
        store
            .apply(vec![
                Mutation::Create(record(
                    Kind::Session,
                    &session,
                    json!({"engagement_id":project}),
                )),
                Mutation::Create(record(
                    Kind::Turn,
                    &format!("legacy-turn-{index}"),
                    json!({"engagement_id":project,"session_id":session}),
                )),
            ])
            .await
            .unwrap();
        let activity = store.activity_snapshot(&project).await.unwrap();
        assert_eq!(ids(&activity.sessions.unwrap()), [session.as_str()]);
        assert_eq!(activity.unfinished_turns.len(), 1);
    }
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn status_snapshot_row_limits_are_shared_and_release_readers() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(record(
            Kind::Session,
            "status",
            json!({}),
        ))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    let template = record(Kind::Turn, "template", json!({}));
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<9999) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT 'bulk-'||i,'chat_turns','status-project',2,json_set(?,'$.id','bulk-'||i),'status','2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n")
        .bind(serde_json::to_string(template.payload()).unwrap()).execute(&mut raw).await.unwrap();
    let activity = store.activity_snapshot("status-project").await.unwrap();
    assert_eq!(activity.unfinished_turns.len(), 10_000);
    assert!(matches!(activity.sessions, Err(Error::ReadLimit)));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    sqlx::query("DELETE FROM entities WHERE id='bulk-0'")
        .execute(&mut raw)
        .await
        .unwrap();
    let activity = store.activity_snapshot("status-project").await.unwrap();
    assert_eq!(activity.unfinished_turns.len(), 9_999);
    assert_eq!(activity.sessions.unwrap().len(), 1);
    assert_eq!(sqlx::query_scalar::<_,i64>("SELECT count(*) FROM entities WHERE kind='chat_turns' AND engagement_id='status-project'").fetch_one(&mut raw).await.unwrap(),9_999);
    // Both successful collection paths must read beyond a legacy 1,000-row
    // page. Fixture insertion is one bounded transaction, never an executor.
    let session_template = record(
        Kind::Session,
        "template",
        json!({"engagement_id":"paged-project"}),
    );
    let hook_template = hook(
        "template",
        json!({"chat_session_id":"status","chat_turn_id":"bulk-1"}),
    );
    let mut tx = raw.begin().await.unwrap();
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<1000) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('paged-session-%04d',i),'chat_sessions','paged-project',2,json_set(?,'$.id',printf('paged-session-%04d',i)),NULL,'2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n")
        .bind(serde_json::to_string(session_template.payload()).unwrap()).execute(&mut *tx).await.unwrap();
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<1000) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('paged-hook-%04d',i),'native_hook_executions','status-project',1,json_set(?,'$.id',printf('paged-hook-%04d',i)),'status','2026-09-23 12:00:00.000000','2026-09-23 12:00:00.000000' FROM n")
        .bind(serde_json::to_string(&hook_template).unwrap()).execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    let activity = store.activity_snapshot("paged-project").await.unwrap();
    assert!(activity.unfinished_turns.is_empty());
    let sessions = activity.sessions.unwrap();
    assert_eq!(sessions.len(), 1_001);
    assert_eq!(
        sessions.first().unwrap().payload()["id"],
        "paged-session-0000"
    );
    assert_eq!(
        sessions.last().unwrap().payload()["id"],
        "paged-session-1000"
    );
    let hooks = store.turn_hooks_snapshot("bulk-1").await.unwrap();
    assert_eq!(hooks.turn.payload()["id"], "bulk-1");
    assert_eq!(hooks.hooks.len(), 1_001);
    assert_eq!(
        hooks.hooks.first().unwrap().payload()["id"],
        "paged-hook-0000"
    );
    assert_eq!(
        hooks.hooks.last().unwrap().payload()["id"],
        "paged-hook-1000"
    );
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn status_snapshot_byte_limits_cover_roots_and_dependencies() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    for record in [
        record(
            Kind::Session,
            "status",
            json!({"metadata":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
        record(
            Kind::Queue,
            "chat-queue-status",
            json!({"items":[{"opaque":"x".repeat(8*1024*1024)}]}),
        ),
        record(
            Kind::Turn,
            "hook-turn",
            json!({"session_id":"missing-parent","request_snapshot":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
    ] {
        store.apply(vec![Mutation::Create(record)]).await.unwrap();
    }
    let mut raw = support::raw(&path).await;
    insert_hook(
        &mut raw,
        &hook(
            "large-hook",
            json!({"hook_snapshot":{"opaque":"x".repeat(8*1024*1024)}}),
        ),
    )
    .await;
    assert!(matches!(
        store
            .activity_snapshot("status-project")
            .await
            .unwrap()
            .sessions,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert!(matches!(
        store.queue_snapshot("status").await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert!(matches!(
        store.turn_hooks_snapshot("hook-turn").await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert!(
        store
            .activity_snapshot("absent")
            .await
            .unwrap()
            .sessions
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM entities WHERE engagement_id='status-project'"
        )
        .fetch_one(&mut raw)
        .await
        .unwrap(),
        4
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}
