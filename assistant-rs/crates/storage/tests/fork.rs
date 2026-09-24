#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    model_validation::CreatedEntityDefaults,
    records::{AssistantKind as Kind, MAX_RECORD_BYTES, StoredAssistantRecord},
};
use nebula_assistant_storage::entities::{
    Config, Error, ForkHarness, ForkRecord, SqliteAssistantStore, StateClock,
};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::sync::{Arc, Mutex};
use std::time::Duration;

fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2020-01-01T00:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn defaults(harness: bool) -> CreatedEntityDefaults {
    CreatedEntityDefaults {
        id: None,
        created_at: now(),
        updated_at: now(),
        last_activity_at: harness.then(now),
    }
}
fn base(id: &str, fields: Value) -> Value {
    let mut p = json!({"id":id,"revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z"});
    p.as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    p
}
fn session(id: &str) -> Value {
    base(
        id,
        json!({"engagement_id":"project","title":"Fixture","provider_profile_id":"provider","model":"fixture"}),
    )
}
fn message(id: &str, session: &str) -> Value {
    base(
        id,
        json!({"engagement_id":"project","session_id":session,"sequence":1,"role":"assistant","content":"Retained"}),
    )
}
async fn insert(db: &mut SqliteConnection, kind: &str, id: &str, session: Option<&str>, raw: &str) {
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,?,'project',1,?,?,'2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')")
        .bind(id).bind(kind).bind(raw).bind(session).execute(db).await.unwrap();
}
async fn payload(db: &mut SqliteConnection, id: &str) -> Option<String> {
    sqlx::query_scalar("SELECT payload FROM entities WHERE id=?")
        .bind(id)
        .fetch_optional(db)
        .await
        .unwrap()
}
fn fork_session(id: &str, title: &str) -> ForkRecord {
    let raw=json!({"id":id,"engagement_id":"project","title":title,"provider_profile_id":"provider","model":"fixture"}).to_string();
    let record = StoredAssistantRecord::decode_fork_created(
        Kind::Session,
        raw.as_bytes(),
        &defaults(false),
        &[],
    )
    .unwrap();
    ForkRecord::new(record, &raw).unwrap()
}
async fn search(db: &mut SqliteConnection, id: &str) {
    sqlx::query("INSERT INTO search_documents(id,project_id,resource_kind,resource_id,revision,label,description,breadcrumb,content,updated_at) VALUES(?,'project','harness_sessions',?,1,'stale','stale','stale','stale','2020-01-01 00:00:00.000000')")
        .bind(id).bind(id).execute(db).await.unwrap();
}

#[tokio::test]
async fn fork_reads_validate_complete_scoped_history_and_shared_limits() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    insert(
        &mut db,
        "chat_sessions",
        "source",
        None,
        &session("source").to_string(),
    )
    .await;
    let original = payload(&mut db, "source").await.unwrap();
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        store.fork_session("source").await.unwrap().record.payload()["title"],
        "Fixture"
    );
    let mut malformed = session("wrapped");
    malformed["title"] = 0.into();
    insert(
        &mut db,
        "chat_sessions",
        "wrapped",
        None,
        &malformed.to_string(),
    )
    .await;
    assert!(matches!(
        store.fork_session("wrapped").await,
        Err(Error::WrappedRecord(_))
    ));

    let mut invalid = message("retracted", "source");
    invalid["sequence"] = 0.into();
    invalid["metadata"] = json!({"retracted_at":"old"});
    insert(
        &mut db,
        "chat_messages",
        "retracted",
        Some("source"),
        &invalid.to_string(),
    )
    .await;
    assert!(
        matches!(
            store.fork_messages("source").await,
            Err(Error::RetainedModelValidation(_))
        ),
        "retracted rows hydrate before service filtering"
    );
    sqlx::query("DELETE FROM entities WHERE id='retracted'")
        .execute(&mut db)
        .await
        .unwrap();
    invalid["id"] = "unrelated".into();
    invalid["session_id"] = "other".into();
    insert(
        &mut db,
        "chat_messages",
        "unrelated",
        Some("other"),
        &invalid.to_string(),
    )
    .await;
    assert!(store.fork_messages("source").await.unwrap().is_empty());
    let decision = base(
        "ignored-decision",
        json!({"engagement_id":"project","session_id":"other","scope":"project","status":"removed","text":""}),
    );
    insert(
        &mut db,
        "chat_decisions",
        "ignored-decision",
        Some("other"),
        &decision.to_string(),
    )
    .await;
    assert!(
        matches!(
            store.fork_decisions("source", "project").await,
            Err(Error::RetainedModelValidation(_))
        ),
        "inactive project rows hydrate before clone eligibility filtering"
    );
    assert!(
        store
            .fork_decisions("source", "different-project")
            .await
            .unwrap()
            .is_empty()
    );

    let mut large = message("large-invalid", "large");
    large["created_at"] = "2020-01-01T00:00:00".into();
    large["metadata"] = json!({"opaque":"x".repeat(9*1024*1024)});
    insert(
        &mut db,
        "chat_messages",
        "large-invalid",
        Some("large"),
        &large.to_string(),
    )
    .await;
    assert!(
        matches!(store.fork_messages("large").await, Err(Error::ReadLimit)),
        "raw row plus shared report owner exceed16MiB"
    );
    sqlx::query("DELETE FROM entities WHERE id='large-invalid'")
        .execute(&mut db)
        .await
        .unwrap();
    large = message("large-valid", "large");
    large["metadata"] = json!({"opaque":"x".repeat(6*1024*1024)});
    insert(
        &mut db,
        "chat_messages",
        "large-valid",
        Some("large"),
        &large.to_string(),
    )
    .await;
    assert!(
        matches!(store.fork_messages("large").await, Err(Error::ReadLimit)),
        "raw, hydrated payload and ordered output share one budget"
    );
    sqlx::query("DELETE FROM entities WHERE id='large-valid'")
        .execute(&mut db)
        .await
        .unwrap();
    large = message("raw-limit", "large");
    large["metadata"] = json!({"opaque":"x".repeat(MAX_RECORD_BYTES)});
    insert(
        &mut db,
        "chat_messages",
        "raw-limit",
        Some("large"),
        &large.to_string(),
    )
    .await;
    assert!(matches!(
        store.fork_messages("large").await,
        Err(Error::ReadLimit)
    ));
    drop(large);

    let template = message("bulk", "bulk").to_string();
    sqlx::query("WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<1001) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('bulk-%05d',i),'chat_messages','project',1,json_set(?,'$.id',printf('bulk-%05d',i),'$.sequence',i),'bulk','2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000' FROM n")
        .bind(&template).execute(&mut db).await.unwrap();
    let complete = store.fork_messages("bulk").await.unwrap();
    assert_eq!(complete.len(), 1001);
    assert_eq!(
        complete.last().unwrap().record.payload()["id"],
        "bulk-01001"
    );
    drop(complete);
    sqlx::query("WITH RECURSIVE n(i) AS(SELECT 1002 UNION ALL SELECT i+1 FROM n WHERE i<10001) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('bulk-%05d',i),'chat_messages','project',1,json_set(?,'$.id',printf('bulk-%05d',i),'$.sequence',i),'bulk','2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000' FROM n")
        .bind(&template).execute(&mut db).await.unwrap();
    assert!(matches!(
        store.fork_messages("bulk").await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert_eq!(
        payload(&mut db, "source").await.as_deref(),
        Some(original.as_str())
    );
    store.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn fork_commits_preserve_order_reject_raw_bypasses_and_cleanup_only_vendor() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let raw = r#"{"id":"branch","engagement_id":"project","title":"Branch","provider_profile_id":"provider","model":"fixture","metadata":{" a ":{"z":0},"b":{"z":1,"a":2},"a":{"y":3,"x":4},"private":"do-not-print"}}"#;
    let record = StoredAssistantRecord::decode_fork_created(
        Kind::Session,
        raw.as_bytes(),
        &defaults(false),
        &[],
    )
    .unwrap();
    let retained = ForkRecord::new(record, raw).unwrap();
    assert!(!format!("{retained:?}").contains("do-not-print"));
    assert!(
        retained.raw_payload.contains(
            r#""metadata":{"a":{"y":3,"x":4},"b":{"z":1,"a":2},"private":"do-not-print"}"#
        )
    );
    let expected = retained.raw_payload.clone();
    store.create_fork_record(retained).await.unwrap();
    assert_eq!(
        payload(&mut db, "branch").await.as_deref(),
        Some(expected.as_str())
    );
    let doc: Value =
        sqlx::query_scalar::<_, String>("SELECT label FROM search_documents WHERE id='branch'")
            .fetch_one(&mut db)
            .await
            .unwrap()
            .into();
    assert_eq!(doc, "Branch");
    assert!(
        matches!(store.create_fork_record(fork_session("branch","Overwrite")).await,Err(Error::AlreadyExists(id)) if id=="branch")
    );
    assert_eq!(
        payload(&mut db, "branch").await.as_deref(),
        Some(expected.as_str())
    );
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT label FROM search_documents WHERE id='branch'")
            .fetch_one(&mut db)
            .await
            .unwrap(),
        "Branch"
    );

    let mut mismatch = fork_session("mismatch", "Safe");
    mismatch.raw_payload = r#"{"id":"unvalidated"}"#.into();
    assert!(matches!(
        store.create_fork_record(mismatch).await,
        Err(Error::CorruptEnvelope)
    ));
    assert!(payload(&mut db, "mismatch").await.is_none());
    let unsupported = support::record(Kind::Turn, "not-a-fork");
    let raw = unsupported.payload().to_string();
    assert!(matches!(
        store
            .create_fork_record(ForkRecord {
                record: unsupported,
                raw_payload: raw
            })
            .await,
        Err(Error::InvalidBounds)
    ));
    let profile = StoredDependency::decode(
        DependencyKind::McpServerProfile,
        &base(
            "not-a-harness",
            json!({"name":"Fixture","transport":"streamable_http","url":"https://mcp.invalid/rpc"}),
        )
        .to_string()
        .into_bytes(),
    )
    .unwrap();
    let raw = profile.payload().to_string();
    assert!(matches!(
        store
            .create_fork_harness(ForkHarness {
                record: profile,
                raw_payload: raw
            })
            .await,
        Err(Error::InvalidBounds)
    ));
    assert!(payload(&mut db, "not-a-fork").await.is_none());
    assert!(payload(&mut db, "not-a-harness").await.is_none());

    search(&mut db, "vendor").await;
    let raw = r#"{"id":"vendor","engagement_id":"project","harness_profile_id":"retained","model":"fixture","metadata":{"private":"do-not-print"}}"#;
    let record =
        StoredDependency::decode_harness_session_created(raw.as_bytes(), &defaults(true)).unwrap();
    let vendor = ForkHarness::new(record, raw).unwrap();
    assert!(!format!("{vendor:?}").contains("do-not-print"));
    store.create_fork_harness(vendor).await.unwrap();
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM search_documents WHERE id='vendor'")
            .fetch_one(&mut db)
            .await
            .unwrap(),
        0,
        "create removes unsupported-kind stale search rows"
    );
    search(&mut db, "vendor").await;
    let raw = r#"{"id":"partial-chat","engagement_id":"project","title":"Partial","backend":"harness","harness_profile_id":"retained","harness_session_id":"vendor","model":"fixture"}"#;
    let record = StoredAssistantRecord::decode_fork_created(
        Kind::Session,
        raw.as_bytes(),
        &defaults(false),
        &[],
    )
    .unwrap();
    store
        .create_fork_record(ForkRecord::new(record, raw).unwrap())
        .await
        .unwrap();
    let partial = payload(&mut db, "partial-chat").await.unwrap();
    let provenance = base(
        "retained-provenance",
        json!({
            "engagement_id":"project","workspace_root":"/fixture/never-opened",
            "scope_kind":"turn","scope_id":"fixture-turn","actor_id":"fixture",
            "owner_kind":"harness","owner_id":"vendor","chat_session_id":"partial-chat",
            "chat_turn_id":null,"status":"active","supported":true,"unsupported_reason":null,
            "baseline":{},"current":{},"mutations":[],"attribution":{},"confidence":"exact",
            "concurrent_scope_ids":[],"started_at":"2020-01-01T00:00:00Z","completed_at":null
        }),
    )
    .to_string();
    insert(
        &mut db,
        "workspace_provenance_observations",
        "retained-provenance",
        None,
        &provenance,
    )
    .await;
    store.cleanup_fork_harness("vendor").await.unwrap();
    assert!(payload(&mut db, "vendor").await.is_none());
    assert_eq!(
        payload(&mut db, "partial-chat").await.as_deref(),
        Some(partial.as_str()),
        "source cleanup preserves separately committed chat prefix"
    );
    assert_eq!(
        payload(&mut db, "retained-provenance").await.as_deref(),
        Some(provenance.as_str()),
        "cleanup never cascades into retained attribution receipts"
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM search_documents WHERE id='vendor'")
            .fetch_one(&mut db)
            .await
            .unwrap(),
        1,
        "NebulaStore.delete is entity-only"
    );
    assert!(matches!(
        store.cleanup_fork_harness("vendor").await,
        Err(Error::NotFound)
    ));
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        reopened.fork_session("branch").await.unwrap().raw_payload,
        expected
    );
    assert_eq!(
        payload(&mut db, "partial-chat").await.as_deref(),
        Some(partial.as_str())
    );
    assert_eq!(
        payload(&mut db, "retained-provenance").await.as_deref(),
        Some(provenance.as_str())
    );
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn fork_harness_cancelled_hydration_keeps_admission_without_reader_leases() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    let raw = base(
        "lazy-vendor",
        json!({"engagement_id":"project","harness_profile_id":"retained","model":"fixture"}),
    )
    .to_string();
    insert(&mut db, "harness_sessions", "lazy-vendor", None, &raw).await;
    insert(
        &mut db,
        "chat_sessions",
        "independent",
        None,
        &session("independent").to_string(),
    )
    .await;
    let config = Config {
        readers: 1,
        read_capacity: 1,
        ..Config::default()
    };
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    let (release, wait) = std::sync::mpsc::channel();
    let wait = Arc::new(Mutex::new(wait));
    let (entered, mut began) = tokio::sync::mpsc::unbounded_channel();
    let clock: Arc<StateClock> = Arc::new(move || {
        entered.send(()).unwrap();
        wait.lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .expect("release blocked fixture clock");
        now()
    });
    let task_store = store.clone();
    let pending = tokio::spawn(async move { task_store.fork_harness("lazy-vendor", clock).await });
    tokio::time::timeout(Duration::from_secs(2), began.recv())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        store.admission().available_reads,
        1,
        "trusted clock runs after releasing read permit"
    );
    assert!(
        tokio::time::timeout(Duration::from_secs(2), store.fork_session("independent"))
            .await
            .unwrap()
            .is_ok()
    );
    pending.abort();
    assert!(pending.await.unwrap_err().is_cancelled());
    assert!(
        matches!(
            store.fork_harness("lazy-vendor", Arc::new(now)).await,
            Err(Error::DependencyUnavailable)
        ),
        "aborted waiter cannot release a still-running blocking slot"
    );
    release.send(()).unwrap();
    let hydrated = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            match store.fork_harness("lazy-vendor", Arc::new(now)).await {
                Err(Error::DependencyUnavailable) => tokio::task::yield_now().await,
                result => break result,
            }
        }
    })
    .await
    .unwrap()
    .unwrap();
    assert_eq!(
        hydrated.record.payload()["last_activity_at"],
        "2020-01-01T00:00:00Z"
    );
    assert_eq!(
        payload(&mut db, "lazy-vendor").await.as_deref(),
        Some(raw.as_str()),
        "read factories never persist observations"
    );
    assert_eq!(store.admission().available_reads, 1);
    store.shutdown().await.unwrap();
    db.close().await.unwrap();
}
