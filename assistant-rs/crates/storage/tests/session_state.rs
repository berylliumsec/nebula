#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    records::{AssistantKind as Kind, StoredAssistantRecord},
    session_state::ConnectionState,
};
use nebula_assistant_storage::entities::{
    Config, Error, Mutation, SqliteAssistantStore, StateObservations,
};
use serde_json::{Value, json};
use sqlx::Connection;
use std::{
    sync::{
        Arc,
        atomic::{AtomicI64, AtomicUsize, Ordering},
    },
    time::Duration,
};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-state.json")).unwrap()
}
fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn record(kind: Kind, id: &str, changes: Value) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "state-project".into();
    if kind == Kind::Session
        && (changes.get("harness_profile_id").is_some()
            || changes.get("harness_session_id").is_some())
    {
        p["backend"] = "harness".into();
        p["provider_profile_id"] = Value::Null;
        p["harness_profile_id"] = "missing-profile".into();
        p["harness_session_id"] = "transport".into();
    }
    if kind == Kind::Turn {
        p["session_id"] = "state".into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn dependency(kind: DependencyKind, id: &str, changes: Value) -> StoredDependency {
    let f = fixture();
    let mut p = f["dependency_records"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["kind"] == kind.as_str())
        .unwrap()["payload"]
        .clone();
    p["id"] = id.into();
    if kind != DependencyKind::HarnessProfile {
        p["engagement_id"] = "state-project".into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn sql_time(p: &Value) -> String {
    DateTime::parse_from_rfc3339(p.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}
async fn insert_dependency(raw: &mut sqlx::SqliteConnection, r: &StoredDependency) {
    let p = r.payload();
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(r.kind().as_str()).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
        .bind(serde_json::to_string(p).unwrap()).bind(p["chat_session_id"].as_str()).bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
        .execute(raw).await.unwrap();
}
async fn ledger(raw: &mut sqlx::SqliteConnection) {
    for sql in fixture()["schema_sql"].as_array().unwrap() {
        sqlx::query(sql.as_str().unwrap())
            .execute(&mut *raw)
            .await
            .unwrap();
    }
}
async fn event(raw: &mut sqlx::SqliteConnection, sequence: i64, kind: &str, event_type: &str) {
    sqlx::query("INSERT INTO operation_events(id,operation_id,operation_kind,engagement_id,sequence,event_type,payload,occurred_at) VALUES (?,'ledger-harness',?,'unrelated-project',?,?, '{}','2020-01-01 00:00:00.000000')")
        .bind(format!("event-{sequence}")).bind(kind).bind(sequence).bind(event_type).execute(raw).await.unwrap();
}
fn observations(clock: Arc<AtomicI64>, connection: Arc<AtomicUsize>) -> StateObservations {
    StateObservations {
        clock: Arc::new(move || DateTime::from_timestamp(clock.load(Ordering::SeqCst), 0).unwrap()),
        connection: Some(Arc::new(move |id| {
            assert_eq!(id, "transport");
            match connection.load(Ordering::SeqCst) {
                1 => ConnectionState::Connected,
                2 => ConnectionState::Disconnected,
                _ => ConnectionState::Unknown,
            }
        })),
    }
}
fn fixed() -> StateObservations {
    StateObservations {
        clock: Arc::new(now),
        connection: None,
    }
}
async fn retained(raw: &mut sqlx::SqliteConnection) -> Vec<(String, i64, String)> {
    sqlx::query_as("SELECT id,revision,payload FROM entities ORDER BY id")
        .fetch_all(raw)
        .await
        .unwrap()
}
async fn wait_admitted(store: &SqliteAssistantStore, config: Config) {
    tokio::time::timeout(Duration::from_secs(2), async {
        while store.admission().available_bytes == config.queued_bytes {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("changed state was not admitted");
}

#[tokio::test]
async fn state_watermarks_preserve_python_digests_expiry_first_progress_and_retained_state() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let f = fixture();
    let cached = &f["initial_watermarks"][0];
    let identity = cached["session_id"].as_str().unwrap();
    let python_session = f["initial_records"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["payload"]["id"] == identity)
        .unwrap();
    store
        .apply(vec![
            Mutation::Create(
                StoredAssistantRecord::decode(
                    Kind::Session,
                    &serde_json::to_vec(&python_session["payload"]).unwrap(),
                )
                .unwrap(),
            ),
            Mutation::Create(record(
                Kind::Session,
                "state",
                json!({"harness_session_id":"transport"}),
            )),
            Mutation::Create(record(
                Kind::Turn,
                "state-turn",
                json!({"approval_id":"pending"}),
            )),
        ])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    ledger(&mut raw).await;
    sqlx::query("INSERT INTO session_projections(session_id,revision,digest) VALUES (?,?,?)")
        .bind(identity)
        .bind(cached["revision"].as_i64())
        .bind(cached["digest"].as_str())
        .execute(&mut raw)
        .await
        .unwrap();
    insert_dependency(&mut raw,&dependency(DependencyKind::Approval,"pending",json!({"chat_session_id":"foreign-session","chat_turn_id":"state-turn","status":"pending","continuation":null,"expires_at":"2030-01-01T12:00:01Z"}))).await;
    let before = retained(&mut raw).await;
    let python = store.session_state(identity, fixed()).await.unwrap();
    assert_eq!(python["revision"], cached["revision"]);
    assert_eq!(
        sqlx::query_scalar::<_, String>(
            "SELECT digest FROM session_projections WHERE session_id=?"
        )
        .bind(identity)
        .fetch_one(&mut raw)
        .await
        .unwrap(),
        cached["digest"].as_str().unwrap()
    );
    let clock = Arc::new(AtomicI64::new(now().timestamp()));
    let connection = Arc::new(AtomicUsize::new(0));
    let observe = observations(clock.clone(), connection.clone());
    let first = store.session_state("state", observe.clone()).await.unwrap();
    assert_eq!(first["execution"], "waiting_approval");
    assert_eq!(first["revision"], 1);
    clock.store(now().timestamp() + 2, Ordering::SeqCst);
    let expired = store.session_state("state", observe.clone()).await.unwrap();
    assert_eq!(expired["execution"], "running");
    assert_eq!(expired["revision"], 2);
    clock.store(now().timestamp(), Ordering::SeqCst);
    assert_eq!(
        store.session_state("state", observe.clone()).await.unwrap()["revision"],
        3
    );
    assert_eq!(
        retained(&mut raw).await,
        before,
        "polling and clock rollback do not change records"
    );
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.status','approved','$.continuation',json(?)) WHERE id='pending'")
        .bind(serde_json::to_string(&json!({"harness_turn_id":"ledger-harness","status":"delivered","detail":null,"adapter_handoff":null,"adapter_status":"sent","adapter_detail":null,"progress_after_sequence":10,"updated_at":"2020-01-01T00:00:00Z"})).unwrap())
        .execute(&mut raw).await.unwrap();
    let before = retained(&mut raw).await;
    let decided = store.session_state("state", observe.clone()).await.unwrap();
    assert_eq!(decided["execution"], "continuing");
    assert_eq!(decided["revision"], 4);
    event(&mut raw, 9, "harness_turn", "harness.completed").await;
    event(&mut raw, 11, "harness_turn", "irrelevant.event").await;
    event(&mut raw, 12, "other", "harness.completed").await;
    assert_eq!(
        store.session_state("state", observe.clone()).await.unwrap()["revision"],
        4
    );
    event(&mut raw, 13, "harness_turn", "harness.message_delta").await;
    let progress = store.session_state("state", observe.clone()).await.unwrap();
    assert_eq!(progress["revision"], 5);
    assert_eq!(progress["execution"], "running");
    assert_eq!(progress["decisions"][0]["progress_sequence"], 13);
    event(&mut raw, 14, "harness_turn", "harness.tool_completed").await;
    assert_eq!(
        store.session_state("state", observe.clone()).await.unwrap(),
        progress
    );
    connection.store(1, Ordering::SeqCst);
    let connected = store.session_state("state", observe.clone()).await.unwrap();
    assert_eq!(connected["revision"], 6);
    connection.store(2, Ordering::SeqCst);
    let disconnected = store.session_state("state", observe.clone()).await.unwrap();
    assert_eq!(disconnected["revision"], 7);
    assert_eq!(retained(&mut raw).await, before);
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM operation_events")
            .fetch_one(&mut raw)
            .await
            .unwrap(),
        5
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        reopened.session_state("state", observe).await.unwrap(),
        disconnected
    );
    reopened
        .apply(vec![Mutation::Delete {
            kind: Kind::Session,
            id: "state".into(),
            expected_revision: 2,
        }])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM session_projections WHERE session_id='state'"
        )
        .fetch_one(&mut raw)
        .await
        .unwrap(),
        0
    );
    raw.close().await.unwrap();
    reopened.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn state_unchanged_reads_avoid_writer_and_queued_updates_reproject_and_drain() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let config = Config::default();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    store
        .apply(vec![
            Mutation::Create(record(
                Kind::Session,
                "state",
                json!({"harness_session_id":"transport"}),
            )),
            Mutation::Create(record(Kind::Turn, "state-turn", json!({}))),
        ])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    ledger(&mut raw).await;
    insert_dependency(&mut raw,&dependency(DependencyKind::Approval,"pending",json!({"chat_session_id":"state","chat_turn_id":"state-turn","status":"pending","continuation":null,"expires_at":"2030-01-01T12:00:01Z"}))).await;
    let clock = Arc::new(AtomicI64::new(now().timestamp()));
    let connection = Arc::new(AtomicUsize::new(0));
    let observe = observations(clock.clone(), connection.clone());
    let baseline = store.session_state("state", observe.clone()).await.unwrap();
    assert_eq!(baseline["revision"], 1);
    let mut lock = raw.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let unchanged = tokio::time::timeout(
        Duration::from_secs(2),
        store.session_state("state", observe.clone()),
    )
    .await
    .expect("unchanged state queued behind writer")
    .unwrap();
    assert_eq!(unchanged, baseline);
    assert_eq!(store.admission().available_bytes, config.queued_bytes);
    connection.store(1, Ordering::SeqCst);
    let queued = {
        let store = store.clone();
        let observe = observe.clone();
        tokio::spawn(async move { store.session_state("state", observe).await })
    };
    wait_admitted(&store, config).await;
    assert_eq!(
        store.admission().available_reads,
        config.read_capacity,
        "snapshot released before queueing"
    );
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.status','finalizing') WHERE id='state-turn'").execute(&mut *lock).await.unwrap();
    clock.store(now().timestamp() + 2, Ordering::SeqCst);
    connection.store(2, Ordering::SeqCst);
    lock.commit().await.unwrap();
    let current = queued.await.unwrap().unwrap();
    assert_eq!(current["revision"], 2);
    assert_eq!(current["execution"], "finalizing");
    assert_eq!(current["pending"], json!([]));
    assert_eq!(current["connection"], "disconnected");
    // Every concurrent poll observes one shared revision, including callers
    // whose initial read raced with another writer's identical assignment.
    let mut polls = Vec::new();
    connection.store(1, Ordering::SeqCst);
    for _ in 0..12 {
        let store = store.clone();
        let observe = observe.clone();
        polls.push(tokio::spawn(async move {
            store.session_state("state", observe).await
        }));
    }
    for poll in polls {
        assert_eq!(poll.await.unwrap().unwrap()["revision"], 3);
    }
    let lock = raw.begin_with("BEGIN IMMEDIATE").await.unwrap();
    connection.store(2, Ordering::SeqCst);
    let cancelled = {
        let store = store.clone();
        let observe = observe.clone();
        tokio::spawn(async move { store.session_state("state", observe).await })
    };
    wait_admitted(&store, config).await;
    cancelled.abort();
    assert!(cancelled.await.unwrap_err().is_cancelled());
    lock.commit().await.unwrap();
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, config).await.unwrap();
    assert_eq!(
        reopened
            .session_state("state", observe.clone())
            .await
            .unwrap()["revision"],
        4,
        "accepted cancelled request drains durably"
    );
    let mut raw = support::raw(&path).await;
    sqlx::query("PRAGMA foreign_keys=ON")
        .execute(&mut raw)
        .await
        .unwrap();
    let mut lock = raw.begin_with("BEGIN IMMEDIATE").await.unwrap();
    connection.store(1, Ordering::SeqCst);
    let deleted = {
        let store = reopened.clone();
        let observe = observe.clone();
        tokio::spawn(async move { store.session_state("state", observe).await })
    };
    wait_admitted(&reopened, config).await;
    sqlx::query("DELETE FROM entities WHERE id='state'")
        .execute(&mut *lock)
        .await
        .unwrap();
    lock.commit().await.unwrap();
    assert!(matches!(deleted.await.unwrap(), Err(Error::NotFound)));
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM session_projections WHERE session_id='state'"
        )
        .fetch_one(&mut raw)
        .await
        .unwrap(),
        0
    );
    raw.close().await.unwrap();
    reopened.shutdown().await.unwrap();
}

#[tokio::test]
async fn state_snapshot_preserves_scopes_validates_complete_collections_and_profile_authority() {
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
                "state",
                json!({"harness_profile_id":"profile"}),
            )),
            Mutation::Create(record(
                Kind::Turn,
                "unrelated-turn",
                json!({"engagement_id":"wrong-project"}),
            )),
        ])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    ledger(&mut raw).await;
    insert_dependency(
        &mut raw,
        &dependency(DependencyKind::HarnessProfile, "profile", json!({})),
    )
    .await;
    sqlx::query("UPDATE entities SET revision=999 WHERE id='unrelated-turn'")
        .execute(&mut raw)
        .await
        .unwrap();
    let turn = record(Kind::Turn, "template", json!({}));
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<1000) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('turn-%04d',i),'chat_turns','state-project',2,json_set(?,'$.id',printf('turn-%04d',i)),'state','2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n").bind(serde_json::to_string(turn.payload()).unwrap()).execute(&mut raw).await.unwrap();
    let approval = dependency(
        DependencyKind::Approval,
        "template",
        json!({"chat_session_id":"other","chat_turn_id":"turn-0000","status":"approved","continuation":{"harness_turn_id":"ledger-harness","status":"delivered","progress_after_sequence":10,"updated_at":"2020-01-01T00:00:00Z"}}),
    );
    let p = approval.payload();
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<1000) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('approval-%04d',i),'approvals','state-project',?,json_set(?,'$.id',printf('approval-%04d',i)),'other',?,? FROM n")
        .bind(p["revision"].as_i64()).bind(serde_json::to_string(p).unwrap()).bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"])).execute(&mut raw).await.unwrap();
    let projected = store.session_state("state", fixed()).await.unwrap();
    assert_eq!(projected["turn_id"], "turn-0000");
    assert_eq!(projected["decisions"].as_array().unwrap().len(), 1001);
    assert_eq!(projected["actions"], json!(["check_status", "stop"]));
    let before = retained(&mut raw).await;
    assert_eq!(
        store.session_state("state", fixed()).await.unwrap(),
        projected
    );
    assert_eq!(retained(&mut raw).await, before);
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.capabilities.interruption',json('false')) WHERE id='profile'").execute(&mut raw).await.unwrap();
    let no_stop = store.session_state("state", fixed()).await.unwrap();
    assert_eq!(no_stop["actions"], json!(["check_status"]));
    assert_eq!(no_stop["revision"], 2);
    sqlx::query("UPDATE entities SET revision=999 WHERE id='approval-1000'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        store.session_state("state", fixed()).await,
        Err(Error::CorruptEnvelope)
    ));
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT revision FROM session_projections WHERE session_id='state'"
        )
        .fetch_one(&mut raw)
        .await
        .unwrap(),
        2
    );
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn state_snapshot_limits_and_revision_exhaustion_leave_durable_state_unchanged() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    for r in [
        record(Kind::Session, "state", json!({})),
        record(Kind::Session, "overflow", json!({})),
        record(
            Kind::Session,
            "large",
            json!({"harness_profile_id":"large-profile","metadata":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
    ] {
        store.apply(vec![Mutation::Create(r)]).await.unwrap();
    }
    let mut raw = support::raw(&path).await;
    ledger(&mut raw).await;
    insert_dependency(
        &mut raw,
        &dependency(
            DependencyKind::HarnessProfile,
            "large-profile",
            json!({"metadata":{"opaque":"x".repeat(8*1024*1024)}}),
        ),
    )
    .await;
    assert!(
        matches!(
            store.session_state("large", fixed()).await,
            Err(Error::ReadLimit)
        ),
        "profile bytes share the root budget"
    );
    sqlx::query(
        "INSERT INTO session_projections(session_id,revision,digest) VALUES ('overflow',?,?)",
    )
    .bind(i64::MAX)
    .bind("0".repeat(64))
    .execute(&mut raw)
    .await
    .unwrap();
    assert!(matches!(
        store.session_state("overflow", fixed()).await,
        Err(Error::RevisionExhausted)
    ));
    assert_eq!(
        sqlx::query_as::<_, (i64, String)>(
            "SELECT revision,digest FROM session_projections WHERE session_id='overflow'"
        )
        .fetch_one(&mut raw)
        .await
        .unwrap(),
        (i64::MAX, "0".repeat(64))
    );
    let turn = record(Kind::Turn, "template", json!({}));
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<9999) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('turn-%04d',i),'chat_turns','state-project',2,json_set(?,'$.id',printf('turn-%04d',i)),'state','2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n").bind(serde_json::to_string(turn.payload()).unwrap()).execute(&mut raw).await.unwrap();
    assert!(matches!(
        store.session_state("state", fixed()).await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM session_projections")
            .fetch_one(&mut raw)
            .await
            .unwrap(),
        1
    );
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert_eq!(
        store.admission().available_bytes,
        Config::default().queued_bytes
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}
