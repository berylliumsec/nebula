use chrono::{DateTime, Utc};
use nebula_assistant_services::AssistantRecords;
use nebula_assistant_storage::entities::{Config, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection, sqlite::SqliteConnectOptions};

fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn sql_time(value: &Value) -> String {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}

#[tokio::test]
async fn concurrent_pending_reads_adopt_each_recorded_effect_once_and_survive_reopen() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-recovery.json")).unwrap();
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = SqliteConnection::connect_with(
        &SqliteConnectOptions::new()
            .filename(&path)
            .create_if_missing(true)
            .foreign_keys(true),
    )
    .await
    .unwrap();
    let schema: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-storage.json")).unwrap();
    for sql in schema["schema_sql"].as_array().unwrap() {
        sqlx::query(sql.as_str().unwrap())
            .execute(&mut db)
            .await
            .unwrap();
    }
    sqlx::query(
        "INSERT INTO schema_versions(version,applied_at) VALUES(5,'2020-01-01 00:00:00.000000')",
    )
    .execute(&mut db)
    .await
    .unwrap();
    sqlx::query("INSERT INTO alembic_version VALUES('0016_chat_session_lookup')")
        .execute(&mut db)
        .await
        .unwrap();
    for row in ["projects", "initial_records", "dependency_records"]
        .into_iter()
        .flat_map(|key| fixture[key].as_array().unwrap())
    {
        let p = &row["payload"];
        let kind = row["kind"].as_str().unwrap();
        let session = if kind == "chat_turns" {
            p["session_id"].as_str()
        } else {
            p["chat_session_id"].as_str()
        };
        let raw = fixture["raw_payloads"]
            .get(p["id"].as_str().unwrap())
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| serde_json::to_string(p).unwrap());
        sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,chat_session_id,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)")
            .bind(p["id"].as_str()).bind(kind).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
            .bind(session).bind(raw).bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
            .execute(&mut db).await.unwrap();
    }
    let before: Vec<(String, String)> =
        sqlx::query_as("SELECT id,payload FROM entities ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap();
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::with_clock(store.clone(), now);
    let expected = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == "both-phases-pending-turn")
        .unwrap()["expected"]["body"]
        .clone();
    let mut tasks = tokio::task::JoinSet::new();
    for _ in 0..16 {
        let service = service.clone();
        tasks.spawn(async move { service.pending_turn("both-phases").await });
    }
    while let Some(result) = tasks.join_next().await {
        assert_eq!(result.unwrap().unwrap(), expected);
    }
    let after: Vec<(String, String)> =
        sqlx::query_as("SELECT id,payload FROM entities ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap();
    for ((id, old), (after_id, new)) in before.iter().zip(&after) {
        assert_eq!(id, after_id);
        if id != "both-phases-turn" && id != "both-phases-hook" {
            assert_eq!(old, new, "unrelated record {id}");
        }
    }
    let turn: Value = serde_json::from_str(
        &after
            .iter()
            .find(|(id, _)| id == "both-phases-turn")
            .unwrap()
            .1,
    )
    .unwrap();
    let hook: Value = serde_json::from_str(
        &after
            .iter()
            .find(|(id, _)| id == "both-phases-hook")
            .unwrap()
            .1,
    )
    .unwrap();
    assert_eq!(turn["revision"], 3);
    assert_eq!(turn["execution_tool_calls"], 1);
    assert_eq!(turn["tool_call_ids"], json!(["both-phases-call"]));
    assert_eq!(
        turn["request_snapshot"]["recovery"]["unknown_tool_call_ids"],
        json!([])
    );
    assert_eq!(
        turn["request_snapshot"]["recovery"]["unknown_hook_execution_ids"],
        json!([])
    );
    assert_eq!(hook["revision"], 2);
    assert_eq!(hook["status"], "complete");
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        AssistantRecords::with_clock(reopened.clone(), now)
            .pending_turn("both-phases")
            .await
            .unwrap(),
        expected
    );
    let final_rows: Vec<(String, String)> =
        sqlx::query_as("SELECT id,payload FROM entities ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap();
    assert_eq!(after, final_rows);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}
