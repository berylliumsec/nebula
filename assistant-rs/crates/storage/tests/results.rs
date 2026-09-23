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
    p["engagement_id"] = "results-project".into();
    if p.get("metadata").is_some() {
        p["metadata"] = json!({});
    }
    if kind != Kind::Session {
        p["session_id"] = "results".into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn dependency(kind: DependencyKind, id: &str, changes: Value) -> Value {
    let mut p = json!({"id":id,"revision":1,"created_at":"2026-09-23T12:00:00Z","updated_at":"2026-09-23T12:00:00Z","engagement_id":"results-project"});
    let fields = match kind {
        DependencyKind::ToolCall => {
            json!({"run_id":"retained","tool_name":"read_fixture","risk_class":"local_read","chat_session_id":"results"})
        }
        DependencyKind::Artifact => {
            json!({"sha256":"ab".repeat(32),"size":1,"storage_path":"sha256/ab/ab/unavailable","source":"harness-file-diff"})
        }
        _ => unreachable!(),
    };
    p.as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap())
        .unwrap()
        .payload()
        .clone()
}
async fn insert(raw: &mut SqliteConnection, kind: DependencyKind, p: &Value) {
    let time = |field: &str| {
        DateTime::parse_from_rfc3339(p[field].as_str().unwrap())
            .unwrap()
            .with_timezone(&Utc)
            .format("%Y-%m-%d %H:%M:%S%.6f")
            .to_string()
    };
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(kind.as_str()).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
        .bind(serde_json::to_string(p).unwrap()).bind(p["chat_session_id"].as_str()).bind(time("created_at")).bind(time("updated_at"))
        .execute(raw).await.unwrap();
}

#[tokio::test]
async fn retained_results_snapshot_preserves_storage_paging_scopes_and_reference_order() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let mut mutations = vec![
        Mutation::Create(record(Kind::Session, "results", json!({}))),
        Mutation::Create(record(
            Kind::Turn,
            "foreign-turn",
            json!({"engagement_id":"foreign-project","session_id":"foreign-session","final_message_id":"m-02"}),
        )),
        Mutation::Create(record(
            Kind::Message,
            "foreign-message",
            json!({"engagement_id":"foreign-project","sequence":1}),
        )),
    ];
    for i in 0..45 {
        mutations.push(Mutation::Create(record(Kind::Message,&format!("m-{i:02}"),json!({
            "role":if i == 0 {"user"} else {"assistant"},"sequence":i+1,"content":"Retained fixture",
            "metadata":match i {1=>json!({"retracted_at":"retained"}),2=>json!({"harness_turn_id":7}),_=>json!({})}
        }))));
    }
    store.apply(mutations).await.unwrap();
    let mut raw = support::raw(&path).await;
    let mut tx = raw.begin().await.unwrap();
    for (id, reference, project) in [
        ("call-z", "foreign-turn", "results-project"),
        ("call-a", "missing-turn", "results-project"),
        ("call-wrong-kind", "m-02", "results-project"),
        ("call-foreign", "foreign-turn", "foreign-project"),
    ] {
        insert(
            &mut tx,
            DependencyKind::ToolCall,
            &dependency(
                DependencyKind::ToolCall,
                id,
                json!({"chat_turn_id":reference,"engagement_id":project}),
            ),
        )
        .await;
    }
    for (id, reference, project) in [
        ("diff-numeric", json!(7), "results-project"),
        ("diff-string", json!("7"), "results-project"),
        ("diff-foreign", json!(7), "foreign-project"),
    ] {
        insert(
            &mut tx,
            DependencyKind::Artifact,
            &dependency(
                DependencyKind::Artifact,
                id,
                json!({"metadata":{"harness_turn_id":reference},"engagement_id":project}),
            ),
        )
        .await;
    }
    tx.commit().await.unwrap();
    let before: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut raw)
        .await
        .unwrap();
    let expected_calls: Vec<String> = sqlx::query_scalar("SELECT id FROM entities WHERE kind='tool_calls' AND engagement_id='results-project' AND chat_session_id='results'").fetch_all(&mut raw).await.unwrap();
    let page = store
        .results_snapshot("results", "results-project", 0, 2, true)
        .await
        .unwrap();
    assert_eq!(
        page.messages
            .iter()
            .map(|m| m.payload()["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["m-00", "m-01"]
    );
    assert!(page.messages[1].is_replaced_message());
    assert_eq!(page.next_offset, Some(2));
    assert_eq!(
        page.calls
            .iter()
            .map(|c| c.payload()["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        expected_calls
    );
    assert!(page.call_turns.is_empty());
    assert!(page.diffs.is_empty());
    let page = store
        .results_snapshot("results", "results-project", 2, 2, false)
        .await
        .unwrap();
    assert_eq!(page.call_turns.len(), 1);
    assert_eq!(
        page.call_turns["foreign-turn"].payload()["engagement_id"],
        "foreign-project"
    );
    assert!(page.diffs.is_empty());
    let page = store
        .results_snapshot("results", "results-project", 2, 2, true)
        .await
        .unwrap();
    assert_eq!(page.diffs["m-02"].len(), 1);
    assert_eq!(page.diffs["m-02"][0].payload()["id"], "diff-numeric");
    let context = store
        .context_sources_page("results", "results-project", 0)
        .await
        .unwrap();
    assert_eq!(context.records.len(), 40);
    assert_eq!(context.records[0].payload()["id"], "m-44");
    assert_eq!(context.next_offset, Some(40));
    let context = store
        .context_sources_page("results", "results-project", 40)
        .await
        .unwrap();
    assert_eq!(context.records.len(), 5);
    assert_eq!(context.next_offset, None);
    assert!(context.records.iter().any(|m| m.is_replaced_message()));
    assert!(matches!(
        store
            .results_snapshot("results", "wrong-project", 0, 40, false)
            .await,
        Err(Error::Conflict)
    ));
    assert!(matches!(
        store
            .context_sources_page("missing", "results-project", 0)
            .await,
        Err(Error::NotFound)
    ));
    assert!(
        store
            .context_sources_page("results", "results-project", i64::MAX as u64)
            .await
            .unwrap()
            .records
            .is_empty()
    );
    assert!(matches!(
        store
            .context_sources_page("results", "results-project", u64::MAX)
            .await,
        Err(Error::InvalidBounds)
    ));
    let after: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut raw)
        .await
        .unwrap();
    assert_eq!(before, after);
    for (index, project) in [String::new(), "p".repeat(201)].into_iter().enumerate() {
        let session_id = format!("legacy-project-{index}");
        let message_id = format!("legacy-message-{index}");
        store
            .apply(vec![
                Mutation::Create(record(
                    Kind::Session,
                    &session_id,
                    json!({"engagement_id":project}),
                )),
                Mutation::Create(record(
                    Kind::Message,
                    &message_id,
                    json!({"engagement_id":project,"session_id":session_id}),
                )),
            ])
            .await
            .unwrap();
        let page = store
            .results_snapshot(&session_id, &project, 0, 40, false)
            .await
            .unwrap();
        assert_eq!(page.messages.len(), 1);
        assert_eq!(page.messages[0].payload()["id"], message_id);
        assert_eq!(
            store
                .context_sources_page(&session_id, &project, 0)
                .await
                .unwrap()
                .records
                .len(),
            1
        );
    }
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn retained_results_budget_covers_dependencies_and_releases_readers_without_partial_pages() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![Mutation::Create(record(Kind::Session,"results",json!({}))),
        Mutation::Create(record(Kind::Message,"large",json!({"role":"assistant","metadata":{"harness_turn_id":"diff","opaque":"x".repeat(6*1024*1024)},"content":"Retained fixture"})))
    ]).await.unwrap();
    let mut raw = support::raw(&path).await;
    insert(
        &mut raw,
        DependencyKind::ToolCall,
        &dependency(
            DependencyKind::ToolCall,
            "large-call",
            json!({"result":"x".repeat(11*1024*1024)}),
        ),
    )
    .await;
    assert!(matches!(
        store
            .results_snapshot("results", "results-project", 0, 40, false)
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    sqlx::query("DELETE FROM entities WHERE id='large-call'")
        .execute(&mut raw)
        .await
        .unwrap();
    for id in ["diff-one", "diff-two"] {
        insert(
            &mut raw,
            DependencyKind::Artifact,
            &dependency(
                DependencyKind::Artifact,
                id,
                json!({"metadata":{"harness_turn_id":"diff","opaque":"x".repeat(6*1024*1024)}}),
            ),
        )
        .await;
    }
    assert!(matches!(
        store
            .results_snapshot("results", "results-project", 0, 40, true)
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store
            .results_snapshot("results", "results-project", 0, 40, false)
            .await
            .unwrap()
            .messages
            .len(),
        1
    );
    for id in ["large-second", "large-third"] {
        store
            .apply(vec![Mutation::Create(record(
                Kind::Message,
                id,
                json!({"metadata":{"opaque":"x".repeat(6*1024*1024)}}),
            ))])
            .await
            .unwrap();
    }
    assert!(matches!(
        store
            .context_sources_page("results", "results-project", 0)
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert!(store.get(Kind::Session, "results").await.is_ok());
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn retained_results_row_budget_and_dependency_envelopes_fail_closed() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(record(
            Kind::Session,
            "results",
            json!({}),
        ))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    let p = dependency(DependencyKind::ToolCall, "bulk", json!({}));
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<9999) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT 'bulk-'||i,'tool_calls','results-project',1,json_set(?,'$.id','bulk-'||i),'results','2026-09-23 12:00:00.000000','2026-09-23 12:00:00.000000' FROM n")
        .bind(serde_json::to_string(&p).unwrap()).execute(&mut raw).await.unwrap();
    // The session also consumes the common snapshot budget.
    assert!(matches!(
        store
            .results_snapshot("results", "results-project", 0, 40, false)
            .await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    sqlx::query("DELETE FROM entities WHERE kind='tool_calls' AND id<>'bulk-0'")
        .execute(&mut raw)
        .await
        .unwrap();
    sqlx::query("UPDATE entities SET revision=2 WHERE id='bulk-0'")
        .execute(&mut raw)
        .await
        .unwrap();
    // Calls are validated even when the selected stored-message page is empty.
    assert!(matches!(
        store
            .results_snapshot("results", "results-project", 0, 40, false)
            .await,
        Err(Error::CorruptEnvelope)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT revision FROM entities WHERE id='bulk-0'")
            .fetch_one(&mut raw)
            .await
            .unwrap(),
        2
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}
