#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use chrono::{DateTime, Duration, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_services::{AssistantRecords, Error};
use nebula_assistant_storage::entities::{Config, Error as StorageError, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::path::Path;

fn oracle() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-subagents.json")).unwrap()
}
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
async fn insert(raw: &mut SqliteConnection, kind: &str, p: &Value, source: Option<&str>) {
    let session = match kind {
        "chat_turns" => p["session_id"].as_str(),
        "approvals" => p["chat_session_id"].as_str(),
        _ => None,
    };
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(kind).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
        .bind(source.map(str::to_owned).unwrap_or_else(|| p.to_string())).bind(session).bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
        .execute(raw).await.unwrap();
}
async fn clean_database(path: &Path) {
    support::database(path).await;
    let mut raw = support::raw(path).await;
    sqlx::query("DELETE FROM entities")
        .execute(&mut raw)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut raw)
        .await
        .unwrap();
    raw.close().await.unwrap();
}
async fn setup(path: &Path) -> (SqliteAssistantStore, AssistantRecords) {
    clean_database(path).await;
    let fixture = oracle();
    let mut raw = support::raw(path).await;
    for row in fixture["projects"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fixture["initial_records"].as_array().unwrap())
        .chain(fixture["dependency_records"].as_array().unwrap())
    {
        let p = &row["payload"];
        insert(
            &mut raw,
            row["kind"].as_str().unwrap(),
            p,
            fixture["raw_payloads"][p["id"].as_str().unwrap()].as_str(),
        )
        .await;
    }
    raw.close().await.unwrap();
    open(path).await
}
async fn open(path: &Path) -> (SqliteAssistantStore, AssistantRecords) {
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    (store.clone(), AssistantRecords::new(store))
}
type RawRow = (
    String,
    String,
    Option<String>,
    i64,
    String,
    Option<String>,
    String,
    String,
);
async fn rows(path: &Path) -> Vec<RawRow> {
    let mut raw = support::raw(path).await;
    let records = sqlx::query_as("SELECT id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at FROM entities ORDER BY id").fetch_all(&mut raw).await.unwrap();
    raw.close().await.unwrap();
    records
}
#[tokio::test]
async fn python_subagent_oracle_matches_retained_display_and_errors() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, services) = setup(&path).await;
    let before = rows(&path).await;
    let fixture = oracle();
    let mut compared = 0;
    for case in fixture["cases"].as_array().unwrap() {
        let Some(session) = case["service"]["session_id"].as_str() else {
            continue;
        };
        let expected = &case["expected"];
        match services.subagents(session, now()).await {
            Ok(value) => {
                assert_eq!(expected["status"], 200, "{}", case["name"]);
                assert_eq!(value, expected["body"], "{}", case["name"]);
            }
            Err(Error::LegacyUnhandled) => {
                assert_eq!(expected["status"], 500, "{}", case["name"]);
                assert_eq!(expected["body"]["code"], "api.unhandled_exception");
            }
            Err(error @ Error::EntityNotFound { .. }) => {
                assert_eq!(expected["status"], 404, "{}", case["name"]);
                assert_eq!(expected["body"]["code"], "chat.not_found_error");
                assert_eq!(expected["body"]["detail"], error.to_string());
            }
            Err(error) => panic!("{}: unexpected {error:?}", case["name"]),
        }
        compared += 1;
    }
    assert_eq!(compared, 69);
    assert_eq!(
        rows(&path).await,
        before,
        "Display GETs must preserve raw dependency and Assistant rows"
    );
    store.shutdown().await.unwrap();
}
#[tokio::test]
async fn subagent_clocks_preserve_recovery_usage_and_raw_state_after_reopen() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, services) = setup(&path).await;
    let mut raw = support::raw(&path).await;
    sqlx::query(
        "INSERT INTO session_projections(session_id,revision,digest) VALUES ('states',37,?)",
    )
    .bind("a".repeat(64))
    .execute(&mut raw)
    .await
    .unwrap();
    raw.close().await.unwrap();
    let before = rows(&path).await;
    let first = services.subagents("states", now()).await.unwrap();
    let later = services
        .subagents("states", now() + Duration::seconds(25))
        .await
        .unwrap();
    for (a, b) in first["subagents"]
        .as_array()
        .unwrap()
        .iter()
        .zip(later["subagents"].as_array().unwrap())
    {
        assert_eq!(a["id"], b["id"]);
        if a["status"] == "running" {
            assert_eq!(
                b["elapsed_seconds"].as_f64().unwrap(),
                a["elapsed_seconds"].as_f64().unwrap() + 25.0
            );
        } else {
            assert_eq!(a, b);
        }
    }
    let recovery = services
        .subagents("terminal-recovery", now())
        .await
        .unwrap();
    assert_eq!(recovery["subagents"][0]["status"], "recovering");
    assert_eq!(recovery["subagents"][0]["finished_at"], Value::Null);
    assert_eq!(recovery["subagents"][0]["result"], "");
    let usage = services.subagents("large-usage", now()).await.unwrap();
    let expected = oracle()["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "large-usage")
        .unwrap()["expected"]["body"]
        .clone();
    assert_eq!(usage, expected);
    assert!(
        usage["subagents"][0]["usage"]["total_tokens"]
            .as_number()
            .unwrap()
            .to_string()
            .len()
            > 20
    );
    assert_eq!(rows(&path).await, before);
    store.shutdown().await.unwrap();
    drop(services);
    let (reopened, services) = open(&path).await;
    assert_eq!(services.subagents("states", now()).await.unwrap(), first);
    assert_eq!(
        services
            .subagents("terminal-recovery", now() + Duration::days(1))
            .await
            .unwrap(),
        recovery
    );
    assert_eq!(
        services.subagents("large-usage", now()).await.unwrap(),
        usage
    );
    assert_eq!(rows(&path).await, before);
    let mut raw = support::raw(&path).await;
    let watermark: (i64, String) =
        sqlx::query_as("SELECT revision,digest FROM session_projections WHERE session_id='states'")
            .fetch_one(&mut raw)
            .await
            .unwrap();
    assert_eq!(watermark, (37, "a".repeat(64)));
    raw.close().await.unwrap();
    reopened.shutdown().await.unwrap();
}
fn record(kind: Kind, id: &str, fields: Value) -> Value {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "retained-project".into();
    p.as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap())
        .unwrap()
        .into_payload()
}
async fn display_fixture(path: &Path, name: Value, count: usize, raw_name: Option<&str>) {
    clean_database(path).await;
    let mut raw = support::raw(path).await;
    insert(
        &mut raw,
        "chat_sessions",
        &record(Kind::Session, "parent", json!({})),
        None,
    )
    .await;
    let turn = record(
        Kind::Turn,
        "child-turn",
        json!({"session_id":"missing-child","status":"complete","tool_history":[{"name":name,"status":true,"arguments":{}}]}),
    );
    let source = raw_name.map(|name| turn.to_string().replace("\"__ordered_name__\"", name));
    insert(&mut raw, "chat_turns", &turn, source.as_deref()).await;
    for index in 0..count {
        let child = record(
            Kind::Subagent,
            &format!("child-{index:04}"),
            json!({"parent_session_id":"parent","child_session_id":"missing-child","child_turn_id":"child-turn","model":"retained","status":"completed","started_at":"2020-01-01T00:00:00Z","finished_at":"2020-01-01T00:00:01Z"}),
        );
        insert(&mut raw, "chat_subagents", &child, None).await;
    }
    raw.close().await.unwrap();
}
#[tokio::test]
async fn subagent_repr_expansion_fails_without_partial_results_or_writes() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    // U+0378 is a two-byte unassigned scalar. Python container repr renders it
    // as six ASCII characters, then the response escapes each backslash.
    display_fixture(&path, json!(["\u{378}".repeat(128 * 1024)]), 20, None).await;
    let before = rows(&path).await;
    let (store, services) = open(&path).await;
    assert_eq!(
        store
            .subagents_snapshot("parent")
            .await
            .unwrap()
            .records
            .len(),
        20,
        "Input snapshot fits its aggregate bound"
    );
    assert!(matches!(
        services.subagents("parent", now()).await,
        Err(Error::Storage(StorageError::ReadLimit))
    ));
    assert_eq!(rows(&path).await, before);
    assert_eq!(
        services
            .subagents("missing-child", now())
            .await
            .unwrap_err()
            .to_string(),
        "chat_sessions entity not found: missing-child"
    );
    store.shutdown().await.unwrap();
}
#[tokio::test]
async fn subagent_renderer_preserves_order_unicode15_and_float_spelling() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    // Retained raw JSON order is deliberate; a generic Value roundtrip sorts
    // these keys and fails the projection contract. U+1FAE8 became printable in
    // Unicode15; NBSP, private-use and unassigned scalars remain escaped.
    let raw =
        r#"{"z":[1.0,1e-5,1e16,-0.0,true,null],"a":"x'\"\n\t\u00a0\u0378\ue000\ud83e\udee8"}"#;
    display_fixture(&path, json!("__ordered_name__"), 1, Some(raw)).await;
    let (store, services) = open(&path).await;
    let response = services.subagents("parent", now()).await.unwrap();
    assert_eq!(
        response["subagents"][0]["recent_steps"][0]["tool"],
        "{'z': [1.0, 1e-05, 1e+16, -0.0, True, None], 'a': 'x\\'\"\\n\\t\\xa0\\u0378\\ue000\u{1fae8}'}"
    );
    assert_eq!(
        response["subagents"][0]["recent_steps"][0]["status"],
        "True"
    );
    store.shutdown().await.unwrap();
}
