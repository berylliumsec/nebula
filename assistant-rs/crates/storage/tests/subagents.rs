#[allow(dead_code)]
mod support;
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{Config, Error, Mutation, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::Connection;

fn record(kind: Kind, id: &str, changes: Value) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "subagent-project".into();
    if kind == Kind::Subagent {
        p["parent_session_id"] = "parent".into();
        p["model"] = "stored-model".into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}

async fn insert_approval(raw: &mut sqlx::SqliteConnection, id: &str) {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-catchup.json")).unwrap();
    let mut p = fixture["dependency_records"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["kind"] == "approvals")
        .unwrap()["payload"]
        .clone();
    p["id"] = id.into();
    // Intentionally unrelated to the child: source lookup is by kind/id only.
    p["engagement_id"] = "other-project".into();
    p["chat_session_id"] = "other-session".into();
    p["expires_at"] = "2020-01-01T00:00:00Z".into();
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,'approvals','other-project',1,?,'other-session','2020-01-01 00:00:00.000000','2020-01-01 00:00:30.000000')")
        .bind(id).bind(serde_json::to_string(&p).unwrap()).execute(raw).await.unwrap();
}

#[tokio::test]
async fn subagent_snapshot_preserves_scopes_conditional_reads_raw_order_and_purity() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![
        Mutation::Create(record(Kind::Session, "parent", json!({}))),
        Mutation::Create(record(Kind::Session, "empty", json!({}))),
        Mutation::Create(record(Kind::Session, "child", json!({"engagement_id":"foreign-child","model":"child-model","provider_profile_id":"child-provider"}))),
        Mutation::Create(record(Kind::Session, "corrupt-child", json!({}))),
        Mutation::Create(record(Kind::Turn, "child-turn", json!({"session_id":"unrelated-session","engagement_id":"foreign-turn","status":"waiting_approval","approval_id":"approval","tool_history":[{"name":{"z":1,"a":2},"status":"complete"}]}))),
        Mutation::Create(record(Kind::Turn, "terminal-turn", json!({"status":"waiting_approval","approval_id":"bad-approval"}))),
        Mutation::Create(record(Kind::Turn, "corrupt-turn", json!({}))),
        Mutation::Create(record(Kind::Message, "wrong-kind", json!({}))),
        Mutation::Create(record(Kind::Subagent, "a-running", json!({"child_turn_id":"child-turn","child_session_id":"child","model":null,"engagement_id":"foreign-subagent","started_at":"2026-09-23T14:00:00+02:00"}))),
        Mutation::Create(record(Kind::Subagent, "b-finished", json!({"child_turn_id":"terminal-turn","status":"completed","finished_at":"2026-09-23T12:01:00Z","child_session_id":"corrupt-child","model":""}))),
        Mutation::Create(record(Kind::Subagent, "c-missing", json!({"child_turn_id":"missing","child_session_id":"wrong-kind","model":null}))),
        Mutation::Create(record(Kind::Subagent, "d-wrong-kind", json!({"child_turn_id":"wrong-kind"}))),
        Mutation::Create(record(Kind::Subagent, "e-bad-child", json!({"model":null,"child_session_id":"corrupt-child"}))),
        Mutation::Create(record(Kind::Subagent, "f-bad-turn", json!({"child_turn_id":"corrupt-turn"}))),
        Mutation::Create(record(Kind::Subagent, "unrelated", json!({"parent_session_id":"other-parent"}))),
        Mutation::Create(record(Kind::SubagentMessage, "z-question", json!({"subagent_id":"a-running","direction":"to_parent","expects_reply":true,"awaiting_reply":true,"parent_session_id":"other-parent","engagement_id":"other-project"}))),
        Mutation::Create(record(Kind::SubagentMessage, "a-closed", json!({"subagent_id":"a-running","direction":"to_parent","status":"undelivered"}))),
        Mutation::Create(record(Kind::SubagentMessage, "irrelevant-direction", json!({"subagent_id":"a-running","direction":"to_child"}))),
        Mutation::Create(record(Kind::SubagentMessage, "irrelevant-terminal", json!({"subagent_id":"b-finished","direction":"to_parent"}))),
        Mutation::Create(record(Kind::SubagentMessage, "irrelevant-missing", json!({"subagent_id":"c-missing","direction":"to_parent"}))),
    ]).await.unwrap();
    let mut raw = support::raw(&path).await;
    insert_approval(&mut raw, "approval").await;
    insert_approval(&mut raw, "bad-approval").await;
    sqlx::query("UPDATE entities SET revision=999 WHERE id IN ('corrupt-child','corrupt-turn','unrelated','bad-approval','irrelevant-direction','irrelevant-terminal','irrelevant-missing')").execute(&mut raw).await.unwrap();
    // SQL json_set preserves this object's deliberately unsorted key order.
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.tool_history',json('[{\"name\":{\"z\":1,\"a\":2},\"status\":\"complete\"}]')) WHERE id='child-turn'").execute(&mut raw).await.unwrap();
    let before: Vec<(String, i64, String)> =
        sqlx::query_as("SELECT id,revision,payload FROM entities ORDER BY id")
            .fetch_all(&mut raw)
            .await
            .unwrap();
    let snapshot = store.subagents_snapshot("parent").await.unwrap();
    assert_eq!(snapshot.session.payload()["id"], "parent");
    assert_eq!(
        snapshot
            .records
            .iter()
            .map(|r| r.record.payload()["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        [
            "a-running",
            "b-finished",
            "c-missing",
            "d-wrong-kind",
            "e-bad-child",
            "f-bad-turn"
        ]
    );
    let mut records = snapshot.records.into_iter();
    let running = records.next().unwrap();
    assert_eq!(
        running.record.payload()["started_at"],
        "2026-09-23T14:00:00+02:00"
    );
    let turn = running.turn.unwrap().unwrap();
    assert_eq!(turn.record.payload()["session_id"], "unrelated-session");
    assert!(turn.raw.contains("\"name\":{\"z\":1,\"a\":2}"));
    let approval = running.approval.unwrap().unwrap();
    assert_eq!(approval.payload()["id"], "approval");
    assert_eq!(approval.payload()["engagement_id"], "other-project");
    let messages = running.messages.unwrap();
    assert_eq!(
        messages
            .iter()
            .map(|r| r.payload()["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["a-closed", "z-question"]
    );
    assert_eq!(
        running.child_session.unwrap().unwrap().payload()["model"],
        "child-model"
    );
    let finished = records.next().unwrap();
    assert!(finished.turn.unwrap().is_some());
    assert!(finished.approval.unwrap().is_none());
    assert!(finished.messages.unwrap().is_empty());
    assert!(
        finished.child_session.unwrap().is_none(),
        "empty model skips fallback"
    );
    let missing = records.next().unwrap();
    assert!(missing.turn.unwrap().is_none());
    assert!(missing.messages.unwrap().is_empty());
    assert!(missing.child_session.unwrap().is_none());
    assert!(records.next().unwrap().turn.unwrap().is_none());
    assert!(matches!(
        records.next().unwrap().child_session,
        Err(Error::CorruptEnvelope)
    ));
    assert!(matches!(
        records.next().unwrap().turn,
        Err(Error::CorruptEnvelope)
    ));
    assert!(
        store
            .subagents_snapshot("empty")
            .await
            .unwrap()
            .records
            .is_empty()
    );
    assert!(matches!(
        store.subagents_snapshot("wrong-kind").await,
        Err(Error::NotFound)
    ));
    assert!(matches!(
        store.subagents_snapshot("missing").await,
        Err(Error::NotFound)
    ));
    let after: Vec<(String, i64, String)> =
        sqlx::query_as("SELECT id,revision,payload FROM entities ORDER BY id")
            .fetch_all(&mut raw)
            .await
            .unwrap();
    assert_eq!(before, after);
    // Conditional errors are deferred independently so services can preserve
    // approval-before-message precedence while projecting sorted records.
    sqlx::query("UPDATE entities SET revision=999 WHERE id IN ('approval','a-closed')")
        .execute(&mut raw)
        .await
        .unwrap();
    let broken = store.subagents_snapshot("parent").await.unwrap();
    assert!(matches!(
        broken.records[0].approval,
        Err(Error::CorruptEnvelope)
    ));
    assert!(matches!(
        broken.records[0].messages,
        Err(Error::CorruptEnvelope)
    ));
    sqlx::query("UPDATE entities SET revision=999 WHERE id='b-finished'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(
        matches!(
            store.subagents_snapshot("parent").await,
            Err(Error::CorruptEnvelope)
        ),
        "all matched subagents are validated before dependencies"
    );
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert!(
        reopened
            .subagents_snapshot("empty")
            .await
            .unwrap()
            .records
            .is_empty()
    );
    reopened.shutdown().await.unwrap();
}

#[tokio::test]
async fn subagent_snapshot_batches_complete_collections_beyond_one_thousand() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(record(
            Kind::Session,
            "parent",
            json!({}),
        ))])
        .await
        .unwrap();
    let mut raw = support::raw(&path).await;
    let mut tx = raw.begin().await.unwrap();
    for kind in [Kind::Subagent, Kind::Turn, Kind::SubagentMessage] {
        let (changes, expression, session) = match kind {
            Kind::Subagent => (
                json!({}),
                "json_set(?,'$.id',printf('chat_subagents-%04d',i),'$.child_turn_id',printf('chat_turns-%04d',i))",
                None,
            ),
            Kind::Turn => (
                json!({}),
                "json_set(?,'$.id',printf('chat_turns-%04d',i))",
                Some("session"),
            ),
            Kind::SubagentMessage => (
                json!({"direction":"to_parent","expects_reply":true,"awaiting_reply":true}),
                "json_set(?,'$.id',printf('chat_subagent_messages-%04d',i),'$.subagent_id',printf('chat_subagents-%04d',i))",
                None,
            ),
            _ => unreachable!(),
        };
        let template = record(kind, "template", changes);
        let query = format!(
            "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<1000) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf(?||'-%04d',i),?,'subagent-project',2,{expression},?,'2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n"
        );
        sqlx::query(&query)
            .bind(kind.as_str())
            .bind(kind.as_str())
            .bind(serde_json::to_string(template.payload()).unwrap())
            .bind(session)
            .execute(&mut *tx)
            .await
            .unwrap();
    }
    let template = record(
        Kind::SubagentMessage,
        "template",
        json!({"direction":"to_parent","subagent_id":"chat_subagents-0000"}),
    );
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<999) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('extra-%04d',i),'chat_subagent_messages','subagent-project',2,json_set(?,'$.id',printf('extra-%04d',i)),NULL,'2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n").bind(serde_json::to_string(template.payload()).unwrap()).execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    let snapshot = store.subagents_snapshot("parent").await.unwrap();
    assert_eq!(snapshot.records.len(), 1001);
    for (index, row) in snapshot.records.into_iter().enumerate() {
        assert_eq!(
            row.record.payload()["id"],
            format!("chat_subagents-{index:04}")
        );
        assert_eq!(
            row.turn.unwrap().unwrap().record.payload()["id"],
            format!("chat_turns-{index:04}")
        );
        let messages = row.messages.unwrap();
        assert_eq!(messages.len(), if index == 0 { 1001 } else { 1 });
        assert_eq!(
            messages[0].payload()["id"],
            format!("chat_subagent_messages-{index:04}")
        );
        if index == 0 {
            assert_eq!(messages[1000].payload()["id"], "extra-0999");
        }
    }
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn subagent_snapshot_limits_cover_roots_raw_history_and_conditional_dependencies() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    for value in [
        record(Kind::Session, "parent", json!({})),
        record(
            Kind::Session,
            "large-parent",
            json!({"metadata":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
        record(
            Kind::Session,
            "large-child",
            json!({"metadata":{"opaque":"x".repeat(8*1024*1024)}}),
        ),
        record(
            Kind::Subagent,
            "fallback",
            json!({"parent_session_id":"large-parent","model":null,"child_session_id":"large-child"}),
        ),
        record(Kind::Session, "raw-history", json!({})),
        record(
            Kind::Subagent,
            "raw-child",
            json!({"parent_session_id":"raw-history","child_turn_id":"large-turn"}),
        ),
        record(
            Kind::Turn,
            "large-turn",
            json!({"request_snapshot":{"opaque":"x".repeat(9*1024*1024)}}),
        ),
        record(Kind::Session, "shared-history", json!({})),
        record(
            Kind::Subagent,
            "shared-a",
            json!({"parent_session_id":"shared-history","child_turn_id":"shared-turn"}),
        ),
        record(
            Kind::Subagent,
            "shared-b",
            json!({"parent_session_id":"shared-history","child_turn_id":"shared-turn"}),
        ),
        record(
            Kind::Turn,
            "shared-turn",
            json!({"request_snapshot":{"opaque":"x".repeat(5*1024*1024)}}),
        ),
    ] {
        store.apply(vec![Mutation::Create(value)]).await.unwrap();
    }
    for parent in ["large-parent", "raw-history", "shared-history"] {
        assert!(
            matches!(
                store.subagents_snapshot(parent).await,
                Err(Error::ReadLimit)
            ),
            "aggregate read limit: {parent}"
        );
    }
    // Root + 10,000 matched rows exceed the shared count even though the old
    // collection API could retrieve them in independent 1,000-record pages.
    let mut raw = support::raw(&path).await;
    let template = record(Kind::Subagent, "template", json!({}));
    sqlx::query("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<9999) INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT printf('child-%04d',i),'chat_subagents','subagent-project',2,json_set(?,'$.id',printf('child-%04d',i)),NULL,'2026-09-23 12:00:00.000000','2026-09-23 12:01:00.000000' FROM n").bind(serde_json::to_string(template.payload()).unwrap()).execute(&mut raw).await.unwrap();
    assert!(matches!(
        store.subagents_snapshot("parent").await,
        Err(Error::ReadLimit)
    ));
    assert_eq!(sqlx::query_scalar::<_,i64>("SELECT count(*) FROM entities WHERE kind='chat_subagents' AND json_extract(payload,'$.parent_session_id')='parent'").fetch_one(&mut raw).await.unwrap(),10_000);
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}
