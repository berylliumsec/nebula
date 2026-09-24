#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_services::{
    AssistantRecords, Error,
    generated::{CatalogKind, GeneratedListRequest},
};
use nebula_assistant_storage::entities::{
    Config, Error as StorageError, Mutation, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::path::Path;

fn oracle() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-plans.json")).unwrap()
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
async fn insert(raw: &mut SqliteConnection, kind: &str, p: &Value) {
    let session = match kind {
        "chat_goals" | "chat_schedules" => p["session_id"].as_str(),
        _ => None,
    };
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(kind).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
        .bind(serde_json::to_string(p).unwrap()).bind(session).bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
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
    {
        insert(&mut raw, row["kind"].as_str().unwrap(), &row["payload"]).await;
    }
    raw.close().await.unwrap();
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    (store.clone(), AssistantRecords::new(store))
}
fn catalog(resource: &str) -> CatalogKind {
    match resource {
        "chat-goals" => CatalogKind::Goals,
        "chat-goal-usage-charges" => CatalogKind::GoalUsageCharges,
        "chat-schedules" => CatalogKind::Schedules,
        "chat-subagents" => CatalogKind::Subagents,
        _ => panic!("Unknown retained resource {resource}"),
    }
}
async fn projection(services: &AssistantRecords, step: &Value) -> Result<Value, Error> {
    match step["kind"].as_str().unwrap() {
        "goal" => {
            services
                .session_goal(step["session_id"].as_str().unwrap(), now())
                .await
        }
        "children" => {
            services
                .goal_children(step["session_id"].as_str().unwrap())
                .await
        }
        "schedule" => {
            services
                .session_schedule(step["session_id"].as_str().unwrap())
                .await
        }
        "catalog" => services
            .catalog(
                catalog(step["resource"].as_str().unwrap()),
                GeneratedListRequest {
                    engagement_id: step["engagement_id"].as_str().map(str::to_owned),
                    offset: step["offset"].as_u64().unwrap(),
                    limit: step["limit"].as_u64().unwrap() as u32,
                },
            )
            .await
            .map(|records| {
                records
                    .into_iter()
                    .map(StoredAssistantRecord::into_payload)
                    .collect::<Vec<_>>()
                    .into()
            }),
        "catalog_record" => services
            .catalog_record(
                catalog(step["resource"].as_str().unwrap()),
                step["id"].as_str().unwrap(),
            )
            .await
            .map(StoredAssistantRecord::into_payload),
        other => panic!("Unknown plan projection {other}"),
    }
}

#[tokio::test]
async fn python_plans_oracle_matches_retained_projections_and_catalogs() {
    let temp = tempfile::tempdir().unwrap();
    let (store, services) = setup(&temp.path().join("nebula.db")).await;
    let fixture = oracle();
    let mut compared = 0;
    for case in fixture["cases"].as_array().unwrap() {
        if !case["service"].is_object() {
            continue;
        }
        let expected = &case["expected"];
        match projection(&services, &case["service"]).await {
            Ok(value) => {
                assert_eq!(expected["status"], 200, "{}", case["name"]);
                assert_eq!(value, expected["body"], "{}", case["name"]);
            }
            Err(Error::Conflict(detail)) => {
                assert_eq!(expected["status"], 409, "{}", case["name"]);
                assert_eq!(expected["body"]["code"], "chat.conflict_error");
                assert_eq!(expected["body"]["detail"], detail);
            }
            Err(Error::LegacyUnhandled) => {
                assert_eq!(expected["status"], 500, "{}", case["name"]);
                assert_eq!(expected["body"]["code"], "api.unhandled_exception");
            }
            Err(error @ (Error::EntityNotFound { .. } | Error::RetainedNotFound(_))) => {
                assert_eq!(expected["status"], 404, "{}", case["name"]);
                assert_eq!(expected["body"]["detail"], error.to_string());
                assert_eq!(
                    expected["body"]["code"],
                    if case["service"]["kind"] == "catalog_record" {
                        "api.not_found_error"
                    } else {
                        "chat.not_found_error"
                    }
                );
            }
            Err(error) => panic!("{}: unexpected {error:?}", case["name"]),
        }
        compared += 1;
    }
    assert!(
        compared >= 30,
        "The oracle must exercise custom projections and four full catalogs"
    );
    store.shutdown().await.unwrap();
}

type RetainedRow = (
    String,
    String,
    Option<String>,
    i64,
    String,
    Option<String>,
    String,
    String,
);
async fn retained_rows(path: &Path) -> Vec<RetainedRow> {
    let mut raw = support::raw(path).await;
    let rows = sqlx::query_as("SELECT id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at FROM entities ORDER BY id")
        .fetch_all(&mut raw).await.unwrap();
    raw.close().await.unwrap();
    rows
}
fn record(kind: Kind, id: &str, fields: Value) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "retained-project".into();
    if kind != Kind::Session {
        p["session_id"] = "parent-session".into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
async fn pure_plans(path: &Path) -> (SqliteAssistantStore, AssistantRecords) {
    clean_database(path).await;
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    store.apply(vec![
        Mutation::Create(record(Kind::Session, "parent-session", json!({}))),
        Mutation::Create(record(Kind::Goal, "parent-goal", json!({
            "status":"running","elapsed_seconds":12.25,"active_since":"2030-01-01T11:59:59.750000Z",
            "time_budget_seconds":1,"parent_goal_id":null,"child_session_ids":[]
        }))),
        Mutation::Create(record(Kind::Goal, "child-goal", json!({
            "session_id":"missing-child-session","engagement_id":"another-project","parent_goal_id":"parent-goal",
            "status":"running","elapsed_seconds":7.5,"active_since":"2020-01-01T00:00:00Z"
        }))),
        Mutation::Create(record(Kind::Schedule, "retained-schedule", json!({
            "enabled":false,"paused_by":"archive","next_run_at":"2030-01-01T13:00:00+02:00",
            "last_run_at":null,"provider_profile_id":"missing-provider","model":"retained-model"
        }))),
    ]).await.unwrap();
    (store.clone(), AssistantRecords::new(store))
}

#[tokio::test]
async fn plan_clocks_preserve_stored_elapsed_revisions_and_schedules_after_reopen() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, services) = pure_plans(&path).await;
    let before = retained_rows(&path).await;
    let mut raw = support::raw(&path).await;
    sqlx::query("INSERT INTO session_projections(session_id,revision,digest) VALUES ('parent-session',61,'retained-plans')")
        .execute(&mut raw).await.unwrap();
    raw.close().await.unwrap();
    let initial = services
        .session_goal("parent-session", now())
        .await
        .unwrap();
    assert_eq!(initial["elapsed_seconds"], 12.5);
    assert_eq!(
        initial["status"], "running",
        "Reading an expired budget must not pause or dispatch a goal"
    );
    assert_eq!(initial["active_since"], "2030-01-01T11:59:59.750000Z");
    let future = services
        .session_goal("parent-session", now() - chrono::Duration::seconds(1))
        .await
        .unwrap();
    assert_eq!(future["elapsed_seconds"], 12.25);
    let submicro = services
        .session_goal("parent-session", now() + chrono::Duration::nanoseconds(999))
        .await
        .unwrap();
    assert_eq!(submicro["elapsed_seconds"], 12.5);
    let mut raw = support::raw(&path).await;
    let original: String =
        sqlx::query_scalar("SELECT payload FROM entities WHERE id='parent-goal'")
            .fetch_one(&mut raw)
            .await
            .unwrap();
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.active_since','2030-01-01T11:59:59.750000001Z') WHERE id='parent-goal'")
        .execute(&mut raw).await.unwrap();
    let nanos = services
        .session_goal("parent-session", now())
        .await
        .unwrap();
    assert_eq!(
        nanos["elapsed_seconds"], 12.5,
        "both retained and observed times truncate to Python microseconds"
    );
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.active_since','1744-07-29T12:12:25.259007Z','$.elapsed_seconds',0.0) WHERE id='parent-goal'")
        .execute(&mut raw).await.unwrap();
    let distant = services
        .session_goal("parent-session", now())
        .await
        .unwrap();
    assert_eq!(
        distant["elapsed_seconds"],
        json!(9_007_199_254.740_993_f64),
        "Python rounds integer true division once even beyond 2^53 microseconds"
    );
    sqlx::query("UPDATE entities SET payload=? WHERE id='parent-goal'")
        .bind(&original)
        .execute(&mut raw)
        .await
        .unwrap();
    raw.close().await.unwrap();
    let later = services
        .session_goal("parent-session", now() + chrono::Duration::seconds(10))
        .await
        .unwrap();
    assert_eq!(later["elapsed_seconds"], 22.5);
    assert_eq!(later["revision"], initial["revision"]);
    assert_eq!(later["updated_at"], initial["updated_at"]);
    let stored = services
        .catalog_record(CatalogKind::Goals, "parent-goal")
        .await
        .unwrap()
        .into_payload();
    assert_eq!(stored["elapsed_seconds"], 12.25);
    let children = services.goal_children("parent-session").await.unwrap();
    assert_eq!(children.as_array().unwrap().len(), 1);
    assert_eq!(children[0]["id"], "child-goal");
    assert_eq!(children[0]["elapsed_seconds"], 7.5);
    let schedule = services.session_schedule("parent-session").await.unwrap();
    assert_eq!(schedule["next_run_at"], "2030-01-01T11:00:00Z");
    assert_eq!(schedule["provider_profile_id"], "missing-provider");
    assert_eq!(schedule["model"], "retained-model");
    assert_eq!(schedule["enabled"], false);
    assert_eq!(schedule["paused_by"], "archive");
    assert_eq!(retained_rows(&path).await, before);
    store.shutdown().await.unwrap();
    drop(services);
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let services = AssistantRecords::new(reopened.clone());
    assert_eq!(
        services
            .session_goal("parent-session", now())
            .await
            .unwrap(),
        initial
    );
    assert_eq!(
        services.goal_children("parent-session").await.unwrap(),
        children
    );
    assert_eq!(
        services.session_schedule("parent-session").await.unwrap(),
        schedule
    );
    assert_eq!(retained_rows(&path).await, before);
    let mut raw = support::raw(&path).await;
    let watermark: (i64, String) = sqlx::query_as(
        "SELECT revision,digest FROM session_projections WHERE session_id='parent-session'",
    )
    .fetch_one(&mut raw)
    .await
    .unwrap();
    assert_eq!(watermark, (61, "retained-plans".into()));
    raw.close().await.unwrap();
    reopened.shutdown().await.unwrap();
}

#[tokio::test]
async fn child_plan_response_limits_preserve_complete_stored_rows() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    clean_database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![
            Mutation::Create(record(Kind::Session, "parent-session", json!({}))),
            Mutation::Create(record(
                Kind::Goal,
                "parent-goal",
                json!({"parent_goal_id":null,"status":"draft"}),
            )),
        ])
        .await
        .unwrap();
    let mut minimal = json!({"id":"child-0000","created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z",
        "revision":1,"engagement_id":"retained-project","session_id":"orphan-child","objective":"x",
        "completion_criteria":["Retain only"],"parent_goal_id":"parent-goal"});
    // Raw collection stays below the byte bound. Filling deterministic legacy
    // defaults exceeds it, so storage must refuse before retaining that complete
    // expanded collection; the service must never return a truncated child list.
    let raw_per_child = (16 * 1024 * 1024 - 16_384) / 1000;
    let header = serde_json::to_vec(&minimal).unwrap().len() - 1;
    let objective = "x".repeat(raw_per_child - header);
    assert!(objective.len() <= 20_000);
    minimal["objective"] = objective.into();
    let hydrated =
        StoredAssistantRecord::decode_persisted(Kind::Goal, &serde_json::to_vec(&minimal).unwrap())
            .unwrap();
    assert!(serde_json::to_vec(hydrated.payload()).unwrap().len() * 1000 > 16 * 1024 * 1024);
    let mut raw = support::raw(&path).await;
    let mut tx = raw.begin().await.unwrap();
    for index in 0..1000 {
        minimal["id"] = format!("child-{index:04}").into();
        insert(&mut tx, "chat_goals", &minimal).await;
    }
    tx.commit().await.unwrap();
    raw.close().await.unwrap();
    assert!(
        matches!(
            store.goal_children_snapshot("parent-session").await,
            Err(StorageError::ReadLimit)
        ),
        "Hydrated defaults count against the bounded storage snapshot"
    );
    let before = retained_rows(&path).await;
    let services = AssistantRecords::new(store.clone());
    assert!(
        matches!(
            services.goal_children("parent-session").await,
            Err(Error::Storage(StorageError::ReadLimit))
        ),
        "Expanded output must fail explicitly rather than return an incomplete child list"
    );
    let smaller = services
        .catalog(
            CatalogKind::Goals,
            GeneratedListRequest {
                engagement_id: Some("retained-project".into()),
                offset: 0,
                limit: 1,
            },
        )
        .await
        .unwrap();
    assert_eq!(smaller.len(), 1);
    assert_eq!(smaller[0].payload()["id"], "child-0000");
    assert_eq!(retained_rows(&path).await, before);
    store.shutdown().await.unwrap();
}
