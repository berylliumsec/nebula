#[allow(dead_code)]
mod support;
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{
    Config, Error, GeneratedListQuery, Mutation, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::Connection;

fn record(kind: Kind, id: &str, changes: Value) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "plans-project".into();
    if matches!(kind, Kind::Goal | Kind::Schedule) {
        p["session_id"] = "plans".into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn ids(records: &[StoredAssistantRecord]) -> Vec<&str> {
    records
        .iter()
        .map(|r| r.payload()["id"].as_str().unwrap())
        .collect()
}
fn query(kind: Kind, project: &str, offset: u64, limit: u32) -> GeneratedListQuery {
    GeneratedListQuery {
        kind,
        engagement_id: Some(project.into()),
        offset,
        limit,
    }
}

#[tokio::test]
async fn plan_snapshots_preserve_root_validation_global_children_and_schedule_order() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![
        Mutation::Create(record(Kind::Session,"plans",json!({}))),
        Mutation::Create(record(Kind::Session,"no-plans",json!({}))),
        Mutation::Create(record(Kind::Session,"duplicate",json!({}))),
        Mutation::Create(record(Kind::Goal,"parent",json!({"engagement_id":"legacy-project","child_session_ids":[]}))),
        Mutation::Create(record(Kind::Goal,"z-child",json!({"session_id":"missing-z","parent_goal_id":"parent"}))),
        Mutation::Create(record(Kind::Goal,"a-child",json!({"engagement_id":"foreign-project","session_id":"missing-a","parent_goal_id":"parent","status":"running","elapsed_seconds":5.0,"active_since":"2026-09-23T13:00:00+01:00"}))),
        Mutation::Create(record(Kind::Goal,"unrelated",json!({"session_id":"other"}))),
        Mutation::Create(record(Kind::Goal,"a-duplicate",json!({"session_id":"duplicate"}))),
        Mutation::Create(record(Kind::Goal,"z-duplicate",json!({"session_id":"duplicate"}))),
        Mutation::Create(record(Kind::Schedule,"z-schedule",json!({"enabled":false,"skip_reason":"Retained pause"}))),
        Mutation::Create(record(Kind::Schedule,"a-schedule",json!({"engagement_id":"foreign-project"}))),
    ]).await.unwrap();
    let mut raw = support::raw(&path).await;
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.next_run_at','2026-09-23T14:01:00+02:00') WHERE id='a-schedule'").execute(&mut raw).await.unwrap();
    let before: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut raw)
        .await
        .unwrap();
    let goals = store
        .session_plans_snapshot(Kind::Goal, "plans")
        .await
        .unwrap();
    assert_eq!(goals.session.payload()["id"], "plans");
    assert_eq!(ids(&goals.records), ["parent"]);
    assert_eq!(
        goals.records[0].payload()["engagement_id"],
        "legacy-project"
    );
    let children = store.goal_children_snapshot("plans").await.unwrap();
    assert_eq!(children.session.payload()["id"], "plans");
    assert_eq!(ids(&children.goals), ["parent"]);
    assert_eq!(ids(&children.children), ["a-child", "z-child"]);
    assert_eq!(children.children[0].payload()["elapsed_seconds"], 5.0);
    assert_eq!(
        children.children[0].payload()["active_since"],
        "2026-09-23T13:00:00+01:00"
    );
    let schedules = store
        .session_plans_snapshot(Kind::Schedule, "plans")
        .await
        .unwrap();
    assert_eq!(ids(&schedules.records), ["a-schedule", "z-schedule"]);
    assert_eq!(
        schedules.records[0].payload()["next_run_at"],
        "2026-09-23T12:01:00Z"
    );
    assert!(
        store
            .session_plans_snapshot(Kind::Schedule, "no-plans")
            .await
            .unwrap()
            .records
            .is_empty()
    );
    assert!(matches!(
        store.session_plans_snapshot(Kind::Queue, "plans").await,
        Err(Error::InvalidBounds)
    ));
    assert!(matches!(
        store.goal_children_snapshot("missing").await,
        Err(Error::NotFound)
    ));
    let after: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut raw)
        .await
        .unwrap();
    assert_eq!(before, after);
    sqlx::query("UPDATE entities SET revision=999 WHERE id='unrelated'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        store.goal_children_snapshot("plans").await,
        Err(Error::CorruptEnvelope)
    ));
    let absent = store.goal_children_snapshot("no-plans").await.unwrap();
    assert!(absent.goals.is_empty() && absent.children.is_empty());
    let duplicate = store.goal_children_snapshot("duplicate").await.unwrap();
    assert_eq!(ids(&duplicate.goals), ["a-duplicate", "z-duplicate"]);
    assert!(
        duplicate.children.is_empty(),
        "skip unrelated scan before service duplicate conflict"
    );
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.interval_seconds',1) WHERE id='z-schedule'").execute(&mut raw).await.unwrap();
    assert!(matches!(
        store.session_plans_snapshot(Kind::Schedule, "plans").await,
        Err(Error::Record(_))
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn plan_reads_and_catalog_pages_are_complete_beyond_legacy_page_sizes() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![
            Mutation::Create(record(Kind::Session, "plans", json!({}))),
            Mutation::Create(record(Kind::Session, "bulk-plans", json!({}))),
            Mutation::Create(record(Kind::Goal, "parent", json!({}))),
        ])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    let mut tx = raw.begin().await.unwrap();
    for kind in [
        Kind::Goal,
        Kind::GoalUsageCharge,
        Kind::Schedule,
        Kind::Subagent,
    ] {
        let mut changes = json!({"engagement_id":"bulk-project"});
        let session = if matches!(kind, Kind::Goal | Kind::Schedule) {
            changes["session_id"] = "bulk-plans".into();
            Some("bulk-plans")
        } else {
            None
        };
        let template = record(kind, "template", changes);
        let payload_sql = if kind == Kind::Goal {
            "json_set(?, '$.id',printf(? || '-%04d',i),'$.parent_goal_id',CASE WHEN i=1000 THEN 'parent' ELSE NULL END)"
        } else {
            "json_set(?, '$.id',printf(? || '-%04d',i))"
        };
        let statement = format!(
            "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<1000) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf(? || '-%04d',i),?,'bulk-project',2,{payload_sql},?,'2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n"
        );
        sqlx::query(&statement)
            .bind(kind.as_str())
            .bind(kind.as_str())
            .bind(serde_json::to_string(template.payload()).unwrap())
            .bind(kind.as_str())
            .bind(session)
            .execute(&mut *tx)
            .await
            .unwrap();
    }
    tx.commit().await.unwrap();
    for kind in [Kind::Goal, Kind::Schedule] {
        let snapshot = store
            .session_plans_snapshot(kind, "bulk-plans")
            .await
            .unwrap();
        assert_eq!(snapshot.records.len(), 1_001);
        assert_eq!(
            snapshot.records[0].payload()["id"],
            format!("{}-0000", kind.as_str())
        );
        assert_eq!(
            snapshot.records[1_000].payload()["id"],
            format!("{}-1000", kind.as_str())
        );
    }
    let children = store.goal_children_snapshot("plans").await.unwrap();
    assert_eq!(ids(&children.children), ["chat_goals-1000"]);
    for kind in [
        Kind::Goal,
        Kind::GoalUsageCharge,
        Kind::Schedule,
        Kind::Subagent,
    ] {
        let first = store
            .list_complete_page(query(kind, "bulk-project", 0, 1000))
            .await
            .unwrap();
        let last = store
            .list_complete_page(query(kind, "bulk-project", 1000, 1000))
            .await
            .unwrap();
        assert_eq!(first.len(), 1000);
        assert_eq!(last.len(), 1);
        assert_eq!(last[0].payload()["id"], format!("{}-1000", kind.as_str()));
        assert!(
            store
                .list_complete_page(query(kind, "bulk-project", i64::MAX as u64, 1))
                .await
                .unwrap()
                .is_empty()
        );
        assert!(matches!(
            store
                .list_complete_page(query(kind, "bulk-project", u64::MAX, 1))
                .await,
            Err(Error::InvalidBounds)
        ));
        for (index, project) in [String::new(), "β".repeat(201)].into_iter().enumerate() {
            let id = format!("legacy-{}-{index}", kind.as_str());
            store
                .apply(vec![Mutation::Create(record(
                    kind,
                    &id,
                    json!({"engagement_id":project}),
                ))])
                .await
                .unwrap();
            let rows = store
                .list_complete_page(query(kind, &project, 0, 1000))
                .await
                .unwrap();
            assert_eq!(ids(&rows), [id.as_str()]);
        }
    }
    assert!(matches!(
        store
            .list_complete_page(query(Kind::Queue, "bulk-project", 0, 1))
            .await,
        Err(Error::InvalidBounds)
    ));
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn plan_snapshot_limits_include_roots_unrelated_history_and_complete_catalog_pages() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    for value in [
        record(
            Kind::Session,
            "large",
            json!({"metadata":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
        record(
            Kind::Goal,
            "large-a",
            json!({"session_id":"large","engagement_id":"large-goals","metadata":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
        record(
            Kind::Goal,
            "large-b",
            json!({"session_id":"other","engagement_id":"large-goals","metadata":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
        record(Kind::Session, "small", json!({})),
        record(Kind::Goal, "small-parent", json!({"session_id":"small"})),
        record(Kind::Session, "many-schedules", json!({})),
    ] {
        store.apply(vec![Mutation::Create(value)]).await.unwrap();
    }
    assert!(matches!(
        store.session_plans_snapshot(Kind::Goal, "large").await,
        Err(Error::ReadLimit)
    ));
    assert!(
        matches!(
            store.goal_children_snapshot("small").await,
            Err(Error::ReadLimit)
        ),
        "unrelated global goal bytes count before filtering"
    );
    let page = store
        .list_complete_page(query(Kind::Goal, "large-goals", 0, 1))
        .await
        .unwrap();
    assert_eq!(page.len(), 1);
    assert_eq!(
        page[0].payload()["metadata"]["opaque"]
            .as_str()
            .unwrap()
            .len(),
        9 * 1024 * 1024
    );
    assert!(matches!(
        store
            .list_complete_page(query(Kind::Goal, "large-goals", 0, 2))
            .await,
        Err(Error::ReadLimit)
    ));
    let mut raw = support::raw(&path).await;
    let template = record(
        Kind::Schedule,
        "template",
        json!({"session_id":"many-schedules"}),
    );
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<9999) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT 'schedule-'||i,'chat_schedules','plans-project',2,json_set(?,'$.id','schedule-'||i),'many-schedules','2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n")
        .bind(serde_json::to_string(template.payload()).unwrap()).execute(&mut raw).await.unwrap();
    assert!(
        matches!(
            store
                .session_plans_snapshot(Kind::Schedule, "many-schedules")
                .await,
            Err(Error::ReadLimit)
        ),
        "the root session shares the10,000row budget"
    );
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert_eq!(sqlx::query_scalar::<_,i64>("SELECT count(*) FROM entities WHERE kind='chat_schedules' AND chat_session_id='many-schedules'").fetch_one(&mut raw).await.unwrap(),10_000);
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}
