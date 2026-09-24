#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    records::{AssistantKind as Kind, StoredAssistantRecord},
};
use nebula_assistant_storage::entities::{
    Config, Error, HookAdoption, Mutation, RecoveryReceiptKind, SqliteAssistantStore, StateClock,
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
fn record(kind: Kind, id: &str, changes: Value) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "recovery-project".into();
    if kind == Kind::Turn {
        p["session_id"] = "recovery".into();
        p["status"] = "interrupted".into();
        p["request_snapshot"] = json!({"recovery":{"required":true,"unknown_tool_call_ids":["tool"],"unknown_hook_execution_ids":["late"]}});
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn dependency(kind: DependencyKind, id: &str, changes: Value) -> StoredDependency {
    let fixture: Value = serde_json::from_str(match kind {
        DependencyKind::ToolCall => include_str!("../../../compatibility/python-results.json"),
        DependencyKind::NativeHookExecution => {
            include_str!("../../../compatibility/python-status.json")
        }
        _ => unreachable!(),
    })
    .unwrap();
    let mut p = fixture["dependency_records"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["kind"] == kind.as_str())
        .unwrap()["payload"]
        .clone();
    p["id"] = id.into();
    p["engagement_id"] = "foreign-project".into();
    p["chat_session_id"] = "foreign-session".into();
    p["chat_turn_id"] = "turn".into();
    if kind == DependencyKind::NativeHookExecution {
        p["status"] = "interrupted".into();
        p["completed_at"] = "2020-01-01T00:01:00Z".into();
        p["error"] = "Unknown after shutdown".into();
        p["late_outcome"] = json!({"status":"complete","exit_code":0,"stdout":"Recorded success","stderr":"Recorded detail","error":null,"observed_at":"2030-01-01T14:00:00+02:00"});
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
async fn insert(raw: &mut sqlx::SqliteConnection, r: &StoredDependency) {
    let p = r.payload();
    let stamp = |v: &Value| {
        DateTime::parse_from_rfc3339(v.as_str().unwrap())
            .unwrap()
            .with_timezone(&Utc)
            .format("%Y-%m-%d %H:%M:%S%.6f")
            .to_string()
    };
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(r.kind().as_str()).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64()).bind(serde_json::to_string(p).unwrap()).bind(p["chat_session_id"].as_str()).bind(stamp(&p["created_at"])).bind(stamp(&p["updated_at"])).execute(raw).await.unwrap();
}
fn hook(id: &str) -> HookAdoption {
    HookAdoption {
        id: id.into(),
        expected_revision: 1,
    }
}
fn changes(kind: RecoveryReceiptKind) -> Map<String, Value> {
    match kind {
        RecoveryReceiptKind::Tool=>json!({"error":"Recorded tool result adopted","request_snapshot":{"recovery":{"required":true,"unknown_tool_call_ids":[],"recorded_tool_result_ids":["tool"],"unknown_hook_execution_ids":["late"]}}}),
        RecoveryReceiptKind::Hook=>json!({"error":"Recorded hook outcome adopted","request_snapshot":{"recovery":{"required":true,"unknown_tool_call_ids":["tool"],"unknown_hook_execution_ids":[],"recorded_hook_outcome_ids":["late"]}}}),
    }.as_object().unwrap().clone()
}
async fn retained(raw: &mut sqlx::SqliteConnection) -> Vec<(String, i64, String)> {
    sqlx::query_as("SELECT id,revision,payload FROM entities ORDER BY id")
        .fetch_all(raw)
        .await
        .unwrap()
}

#[tokio::test]
async fn recovery_snapshots_preserve_session_scope_reference_order_and_deferred_errors() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![
        Mutation::Create(record(Kind::Session,"recovery",json!({}))),
        Mutation::Create(record(Kind::Turn,"turn",json!({"engagement_id":"different-project","request_snapshot":{"recovery":{"required":true,"unknown_tool_call_ids":["tool",7,"missing","wrong-kind","tool","bad"],"unknown_hook_execution_ids":["late","missing","late"]}}}))),
        Mutation::Create(record(Kind::Turn,"z-complete",json!({"status":"complete"}))),
        Mutation::Create(record(Kind::Turn,"other",json!({"session_id":"different-session"}))),
        Mutation::Create(record(Kind::Turn,"guarded",json!({"session_id":"orphan","status":"complete","request_snapshot":{"recovery":{"unknown_tool_call_ids":["bad"]}}}))),
        Mutation::Create(record(Kind::Session,"wrong-kind",json!({}))),
    ]).await.unwrap();
    let mut raw = support::raw(&path).await;
    insert(
        &mut raw,
        &dependency(DependencyKind::ToolCall, "tool", json!({})),
    )
    .await;
    insert(
        &mut raw,
        &dependency(DependencyKind::ToolCall, "bad", json!({})),
    )
    .await;
    insert(
        &mut raw,
        &dependency(DependencyKind::NativeHookExecution, "late", json!({})),
    )
    .await;
    sqlx::query("UPDATE entities SET revision=999 WHERE id IN ('bad','other')")
        .execute(&mut raw)
        .await
        .unwrap();
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.tool_history',json('[{\"status\":\"waiting_callback\",\"results_url\":{\"z\":1,\"a\":2}}]')) WHERE id='turn'").execute(&mut raw).await.unwrap();
    let before = retained(&mut raw).await;
    let unfinished = store.unfinished_turns_snapshot("recovery").await.unwrap();
    assert_eq!(unfinished.session.payload()["id"], "recovery");
    assert_eq!(unfinished.turns.len(), 1);
    assert_eq!(unfinished.turns[0].record.payload()["id"], "turn");
    assert!(
        unfinished.turns[0]
            .raw_payload
            .contains("\"results_url\":{\"z\":1,\"a\":2}")
    );
    let all = store.session_turns_snapshot("recovery").await.unwrap();
    assert_eq!(
        all.turns
            .iter()
            .map(|t| t.record.payload()["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["turn", "z-complete"]
    );
    let tools = store
        .recovery_receipts_snapshot("turn", RecoveryReceiptKind::Tool)
        .await
        .unwrap();
    assert_eq!(
        tools
            .receipts
            .iter()
            .map(|r| r.id.as_str())
            .collect::<Vec<_>>(),
        ["tool", "missing", "wrong-kind", "tool", "bad"]
    );
    let mut rows = tools.receipts.into_iter();
    assert_eq!(
        rows.next().unwrap().record.unwrap().unwrap().payload()["chat_session_id"],
        "foreign-session"
    );
    assert!(rows.next().unwrap().record.unwrap().is_none());
    assert!(rows.next().unwrap().record.unwrap().is_none());
    assert!(rows.next().unwrap().record.unwrap().is_some());
    assert!(matches!(
        rows.next().unwrap().record,
        Err(Error::CorruptEnvelope)
    ));
    assert_eq!(
        store
            .recovery_receipts_snapshot("turn", RecoveryReceiptKind::Hook)
            .await
            .unwrap()
            .receipts
            .len(),
        3
    );
    assert!(
        store
            .recovery_receipts_snapshot("guarded", RecoveryReceiptKind::Tool)
            .await
            .unwrap()
            .receipts
            .is_empty(),
        "phase guards skip corrupt receipts and parent lookup"
    );
    assert!(matches!(
        store.unfinished_turns_snapshot("missing").await,
        Err(Error::NotFound)
    ));
    assert_eq!(retained(&mut raw).await, before);
    sqlx::query("UPDATE entities SET revision=999 WHERE id='z-complete'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(store.unfinished_turns_snapshot("recovery").await.is_ok());
    assert!(
        matches!(
            store.session_turns_snapshot("recovery").await,
            Err(Error::CorruptEnvelope)
        ),
        "latest selection follows complete validation"
    );
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn hook_adoption_is_atomic_and_preserves_duplicate_cas_missing_and_binding_failures() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![Mutation::Create(record(Kind::Turn,"turn",json!({"request_snapshot":{"recovery":{"required":true,"unknown_hook_execution_ids":["late","late"],"unknown_tool_call_ids":["tool"]}}})))]).await.unwrap();
    let mut raw = support::raw(&path).await;
    insert(
        &mut raw,
        &dependency(DependencyKind::NativeHookExecution, "late", json!({})),
    )
    .await;
    insert(
        &mut raw,
        &dependency(
            DependencyKind::NativeHookExecution,
            "foreign",
            json!({"chat_turn_id":"different-turn"}),
        ),
    )
    .await;
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.tool_history',json('[{\"status\":\"waiting_callback\",\"results_url\":{\"z\":1,\"a\":2}}]')) WHERE id='turn'").execute(&mut raw).await.unwrap();
    let before = retained(&mut raw).await;
    assert!(
        matches!(
            store
                .adopt_recorded_hooks(
                    "turn",
                    1,
                    changes(RecoveryReceiptKind::Hook),
                    vec![hook("late")],
                    clock()
                )
                .await,
            Err(Error::RevisionConflict { .. })
        ),
        "stale Turn CAS rolls back the earlier hook write"
    );
    assert_eq!(retained(&mut raw).await, before);
    assert!(
        matches!(
            store
                .adopt_recorded_hooks(
                    "turn",
                    2,
                    changes(RecoveryReceiptKind::Hook),
                    vec![hook("late"), hook("late")],
                    clock()
                )
                .await,
            Err(Error::RevisionConflict { .. })
        ),
        "duplicate late refs must not be deduplicated"
    );
    assert_eq!(retained(&mut raw).await, before);
    assert!(
        matches!(store.adopt_recorded_hooks("turn",2,changes(RecoveryReceiptKind::Hook),vec![hook("late"),hook("deleted")],clock()).await,Err(Error::RecoveryHookNotFound(id)) if id=="deleted")
    );
    assert_eq!(retained(&mut raw).await, before);
    assert!(matches!(
        store
            .adopt_recorded_hooks(
                "turn",
                2,
                changes(RecoveryReceiptKind::Hook),
                vec![hook("late"), hook("foreign")],
                clock()
            )
            .await,
        Err(Error::Conflict)
    ));
    assert_eq!(retained(&mut raw).await, before);
    let repaired = store
        .adopt_recorded_hooks(
            "turn",
            2,
            changes(RecoveryReceiptKind::Hook),
            vec![hook("late")],
            clock(),
        )
        .await
        .unwrap();
    assert_eq!(repaired.record.payload()["revision"], 3);
    assert_eq!(
        repaired.record.payload()["updated_at"],
        "2030-01-01T12:00:05Z"
    );
    assert!(
        repaired
            .raw_payload
            .contains("\"results_url\":{\"z\":1,\"a\":2}")
    );
    let saved: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='late'")
        .fetch_one(&mut raw)
        .await
        .unwrap();
    let saved: Value = serde_json::from_str(&saved).unwrap();
    assert_eq!(saved["status"], "complete");
    assert_eq!(saved["completed_at"], "2030-01-01T14:00:00+02:00");
    assert_eq!(saved["revision"], 2);
    assert_eq!(saved["exit_code"], 0);
    assert_eq!(saved["stdout"], "Recorded success");
    assert_eq!(saved["stderr"], "Recorded detail");
    assert!(saved["error"].is_null());
    let original = dependency(DependencyKind::NativeHookExecution, "late", json!({}));
    for field in [
        "late_outcome",
        "hook_snapshot",
        "started_at",
        "owner_kind",
        "chat_turn_id",
        "engagement_id",
    ] {
        assert_eq!(saved[field], original.payload()[field]);
    }
    assert!(matches!(
        store
            .adopt_recorded_hooks(
                "turn",
                3,
                json!({"status":"complete"}).as_object().unwrap().clone(),
                vec![],
                clock()
            )
            .await,
        Err(Error::ProtectedField)
    ));
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        reopened
            .recovery_receipts_snapshot("turn", RecoveryReceiptKind::Hook)
            .await
            .unwrap()
            .turn
            .raw_payload,
        repaired.raw_payload
    );
    reopened.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn tool_adoption_preserves_receipts_samples_clock_after_lock_and_drains_cancellation() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let config = Config::default();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    store
        .apply(vec![Mutation::Create(record(
            Kind::Turn,
            "turn",
            json!({}),
        ))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    insert(
        &mut raw,
        &dependency(DependencyKind::ToolCall, "tool", json!({})),
    )
    .await;
    let observed = store
        .recovery_receipts_snapshot("turn", RecoveryReceiptKind::Tool)
        .await
        .unwrap();
    assert_eq!(observed.turn.record.payload()["revision"], 2);
    sqlx::query(
        "UPDATE entities SET revision=2,payload=json_set(payload,'$.revision',2) WHERE id='tool'",
    )
    .execute(&mut raw)
    .await
    .unwrap();
    let tool_before: (i64, String) =
        sqlx::query_as("SELECT revision,payload FROM entities WHERE id='tool'")
            .fetch_one(&mut raw)
            .await
            .unwrap();
    let lock = raw.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let time = Arc::new(AtomicI64::new(now().timestamp()));
    let sampled = time.clone();
    let sample: Arc<StateClock> =
        Arc::new(move || DateTime::from_timestamp(sampled.load(Ordering::SeqCst), 0).unwrap());
    let pending = {
        let store = store.clone();
        tokio::spawn(async move {
            store
                .adopt_recorded_tools("turn", 2, changes(RecoveryReceiptKind::Tool), sample)
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
    let reopened = SqliteAssistantStore::open(&path, config).await.unwrap();
    let mut raw = support::raw(&path).await;
    let turn = reopened.get(Kind::Turn, "turn").await.unwrap();
    assert_eq!(turn.payload()["revision"], 3);
    assert_eq!(turn.payload()["updated_at"], "2030-01-01T12:00:35Z");
    assert_eq!(
        sqlx::query_as::<_, (i64, String)>("SELECT revision,payload FROM entities WHERE id='tool'")
            .fetch_one(&mut raw)
            .await
            .unwrap(),
        tool_before,
        "tool phase intentionally changes only the Turn"
    );
    let before = retained(&mut raw).await;
    assert!(matches!(
        reopened
            .adopt_recorded_tools("turn", 2, changes(RecoveryReceiptKind::Tool), clock())
            .await,
        Err(Error::RevisionConflict { .. })
    ));
    assert_eq!(retained(&mut raw).await, before);
    raw.close().await.unwrap();
    reopened.shutdown().await.unwrap();
}

#[tokio::test]
async fn recovery_collections_and_repairs_enforce_shared_bounds_without_partial_writes() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![Mutation::Create(record(Kind::Session,"recovery",json!({}))),Mutation::Create(record(Kind::Turn,"turn",json!({"session_id":"single","request_snapshot":{"recovery":{"unknown_tool_call_ids":vec!["tool";1001],"unknown_hook_execution_ids":["late","large-hook"]}}})))]).await.unwrap();
    let mut raw = support::raw(&path).await;
    insert(
        &mut raw,
        &dependency(DependencyKind::ToolCall, "tool", json!({})),
    )
    .await;
    insert(
        &mut raw,
        &dependency(DependencyKind::NativeHookExecution, "late", json!({})),
    )
    .await;
    let receipts = store
        .recovery_receipts_snapshot("turn", RecoveryReceiptKind::Tool)
        .await
        .unwrap();
    assert_eq!(receipts.receipts.len(), 1001);
    assert!(
        receipts
            .receipts
            .iter()
            .all(|r| r.record.as_ref().unwrap().is_some())
    );
    let template = record(Kind::Turn, "template", json!({}));
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<1000) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('turn-%04d',i),'chat_turns','recovery-project',2,json_set(?,'$.id',printf('turn-%04d',i)),'recovery','2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n").bind(serde_json::to_string(template.payload()).unwrap()).execute(&mut raw).await.unwrap();
    for all in [false, true] {
        let rows = if all {
            store.session_turns_snapshot("recovery").await.unwrap()
        } else {
            store.unfinished_turns_snapshot("recovery").await.unwrap()
        };
        assert_eq!(rows.turns.len(), 1001);
        assert_eq!(rows.turns[1000].record.payload()["id"], "turn-1000");
    }
    insert(
        &mut raw,
        &dependency(
            DependencyKind::NativeHookExecution,
            "large-hook",
            json!({"hook_snapshot":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
    )
    .await;
    let before = retained(&mut raw).await;
    assert!(matches!(
        store
            .adopt_recorded_hooks(
                "turn",
                2,
                changes(RecoveryReceiptKind::Hook),
                vec![hook("late"), hook("large-hook")],
                clock()
            )
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        retained(&mut raw).await,
        before,
        "large second hook rolls back first adoption"
    );
    store
        .apply(vec![Mutation::Create(record(
            Kind::Turn,
            "big-turn",
            json!({"session_id":"big","request_snapshot":{"opaque":"x".repeat(9*1024*1024)}}),
        ))])
        .await
        .unwrap();
    assert!(
        matches!(
            store
                .recovery_receipts_snapshot("big-turn", RecoveryReceiptKind::Tool)
                .await,
            Err(Error::ReadLimit)
        ),
        "retained raw Turn copy shares byte budget"
    );
    let unknown = serde_json::to_string(&vec!["missing"; 10_000]).unwrap();
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.request_snapshot.recovery.unknown_tool_call_ids',json(?)) WHERE id='turn'").bind(unknown).execute(&mut raw).await.unwrap();
    assert!(
        matches!(
            store
                .recovery_receipts_snapshot("turn", RecoveryReceiptKind::Tool)
                .await,
            Err(Error::ReadLimit)
        ),
        "missing references also consume aggregate budget"
    );
    let unknown = serde_json::to_string(&vec![Value::Null; 10_000]).unwrap();
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.request_snapshot.recovery.unknown_tool_call_ids',json(?)) WHERE id='turn'").bind(unknown).execute(&mut raw).await.unwrap();
    assert!(
        matches!(
            store
                .recovery_receipts_snapshot("turn", RecoveryReceiptKind::Tool)
                .await,
            Err(Error::ReadLimit)
        ),
        "ignored nonstring references also consume aggregate budget"
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
