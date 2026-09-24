#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::AssistantKind as Kind;
use nebula_assistant_services::{
    AssistantRecords, Error,
    settings::{ScheduleCreate, ScheduleWrite, SettingsWrite},
};
use nebula_assistant_storage::entities::{Config, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    sync::atomic::{AtomicI64, Ordering},
};

static ORACLE_MICROS: AtomicI64 = AtomicI64::new(0);
fn oracle_clock() -> DateTime<Utc> {
    DateTime::from_timestamp_micros(ORACLE_MICROS.load(Ordering::SeqCst)).unwrap()
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
fn settings(fields: Value) -> SettingsWrite {
    serde_json::from_value(fields).unwrap()
}
fn session(id: &str, harness: bool, metadata: Value) -> Value {
    let mut p = support::payload(Kind::Session);
    p["id"] = id.into();
    p["revision"] = 1.into();
    p["created_at"] = json!("2020-01-01T00:00:00Z");
    p["updated_at"] = p["created_at"].clone();
    p["backend"] = json!(if harness { "harness" } else { "provider" });
    p["provider_profile_id"] = if harness {
        Value::Null
    } else {
        json!("not-configured-provider")
    };
    p["harness_profile_id"] = if harness {
        json!("not-configured-harness")
    } else {
        Value::Null
    };
    p["harness_session_id"] = if harness {
        json!(format!("vendor-{id}"))
    } else {
        Value::Null
    };
    p["metadata"] = metadata;
    p
}
async fn clean(path: &Path) -> SqliteConnection {
    support::database(path).await;
    let mut db = support::raw(path).await;
    sqlx::query("DELETE FROM entities")
        .execute(&mut db)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut db)
        .await
        .unwrap();
    db
}
async fn insert(db: &mut SqliteConnection, kind: &str, p: &Value, raw: Option<&str>) {
    let owner = if matches!(kind, "chat_turns" | "chat_schedules") {
        p["session_id"].as_str()
    } else {
        p["chat_session_id"].as_str()
    };
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(kind).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64()).bind(raw.map(str::to_owned).unwrap_or_else(||p.to_string())).bind(owner).bind(sql_time(&p["created_at"])).bind(sql_time(&p["updated_at"]))
        .execute(db).await.unwrap();
}
async fn open(path: &Path) -> (SqliteAssistantStore, AssistantRecords) {
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::with_clock(store.clone(), now);
    (store, service)
}
async fn payload(db: &mut SqliteConnection, id: &str) -> Value {
    let raw: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id=?")
        .bind(id)
        .fetch_one(db)
        .await
        .unwrap();
    serde_json::from_str(&raw).unwrap()
}

async fn raw_records(db: &mut SqliteConnection) -> BTreeMap<String, (String, String)> {
    let rows: Vec<(String, String, String)> =
        sqlx::query_as("SELECT id,kind,payload FROM entities ORDER BY id")
            .fetch_all(db)
            .await
            .unwrap();
    rows.into_iter()
        .map(|(id, kind, payload)| (id, (kind, payload)))
        .collect()
}
async fn search_rows(db: &mut SqliteConnection) -> BTreeMap<String, Value> {
    let rows: Vec<String> = sqlx::query_scalar("SELECT json_object('id',id,'project_id',project_id,'resource_kind',resource_kind,'resource_id',resource_id,'revision',revision,'label',label,'description',description,'breadcrumb',breadcrumb,'content',content,'updated_at',updated_at) FROM search_documents ORDER BY id").fetch_all(db).await.unwrap();
    rows.into_iter()
        .map(|raw| {
            let row: Value = serde_json::from_str(&raw).unwrap();
            (row["id"].as_str().unwrap().to_owned(), row)
        })
        .collect()
}

#[tokio::test]
async fn python_settings_oracle_matches_saved_choices_and_commit_order() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-settings.json")).unwrap();
    let cases = fixture["cases"]
        .as_array()
        .expect("Completed settings capture is required");
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = clean(&path).await;
    for row in ["projects", "initial_records", "dependency_records"]
        .into_iter()
        .flat_map(|key| fixture[key].as_array().unwrap())
    {
        let p = &row["payload"];
        insert(
            &mut db,
            row["kind"].as_str().unwrap(),
            p,
            fixture["raw_payloads"][p["id"].as_str().unwrap()].as_str(),
        )
        .await;
    }
    for row in fixture["initial_search_documents"].as_array().unwrap() {
        sqlx::query("INSERT INTO search_documents(id,project_id,resource_kind,resource_id,revision,label,description,breadcrumb,content,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)")
            .bind(row["id"].as_str()).bind(row["project_id"].as_str()).bind(row["resource_kind"].as_str()).bind(row["resource_id"].as_str()).bind(row["revision"].as_i64()).bind(row["label"].as_str()).bind(row["description"].as_str()).bind(row["breadcrumb"].as_str()).bind(row["content"].as_str()).bind(row["updated_at"].as_str()).execute(&mut db).await.unwrap();
    }
    let mut store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let mut service = AssistantRecords::with_clock(store.clone(), oracle_clock);
    let mut compared = 0;
    let mut repaired_on_refusal = false;
    for case in cases {
        // HTTP owns authentication/request decoding and deterministic writer
        // fault injection; the independent test below proves partial commits.
        if !case["service"].is_object()
            || case.get("fault").is_some()
            || case["known_unsupported"] == true
        {
            continue;
        }
        let clock = case.get("clock").unwrap_or(&fixture["clock"]);
        ORACLE_MICROS.store(
            DateTime::parse_from_rfc3339(clock.as_str().unwrap())
                .unwrap()
                .timestamp_micros(),
            Ordering::SeqCst,
        );
        if case["action"] == "reopen" {
            store.shutdown().await.unwrap();
            store = SqliteAssistantStore::open(&path, Config::default())
                .await
                .unwrap();
            service = AssistantRecords::with_clock(store.clone(), oracle_clock);
        }
        let descriptor = &case["service"];
        let session = descriptor["session_id"].as_str().unwrap();
        let before = raw_records(&mut db).await;
        let search_before = search_rows(&mut db).await;
        let mut uuid_calls = 0;
        let actual = match descriptor["kind"].as_str().unwrap() {
            "session_patch" => {
                service
                    .update_session_settings(
                        session,
                        serde_json::from_value(descriptor["body"].clone()).unwrap(),
                    )
                    .await
            }
            "schedule_create" => {
                service
                    .create_schedule(
                        session,
                        serde_json::from_value(descriptor["body"].clone()).unwrap(),
                        || {
                            uuid_calls += 1;
                            case["generated_ids"][0].as_str().unwrap().to_owned()
                        },
                    )
                    .await
            }
            "schedule_write" => {
                service
                    .write_schedule(
                        session,
                        serde_json::from_value(descriptor["body"].clone()).unwrap(),
                    )
                    .await
            }
            kind => panic!("Unrecognized service descriptor {kind}"),
        };
        if let Some(expected_calls) = case.get("expected_uuid_calls") {
            assert_eq!(
                expected_calls,
                &json!(uuid_calls),
                "{}: guarded identity allocation",
                case["name"]
            );
        }
        match actual {
            Ok(record) => {
                assert_eq!(case["expected"]["status"], 200, "{}", case["name"]);
                assert_eq!(
                    record.payload(),
                    &case["expected"]["body"],
                    "{}",
                    case["name"]
                );
            }
            Err(
                error @ (Error::Conflict(_)
                | Error::DynamicConflict(_)
                | Error::RevisionConflict { .. }
                | Error::HistoryConflict(_)),
            ) => {
                assert_eq!(case["expected"]["status"], 409, "{}", case["name"]);
                assert_eq!(
                    case["expected"]["body"]["detail"],
                    error.to_string(),
                    "{}",
                    case["name"]
                );
            }
            Err(error @ (Error::EntityNotFound { .. } | Error::RetainedNotFound(_))) => {
                assert_eq!(case["expected"]["status"], 404, "{}", case["name"]);
                assert_eq!(
                    case["expected"]["body"]["detail"],
                    error.to_string(),
                    "{}",
                    case["name"]
                );
            }
            Err(Error::RetainedModelValidation(report)) => {
                assert_eq!(case["expected"]["status"], 422, "{}", case["name"]);
                assert_eq!(case["expected"]["body"]["code"], "api.model_validation");
                assert_eq!(
                    serde_json::to_value(report).unwrap(),
                    case["expected"]["body"]["detail"],
                    "{}",
                    case["name"]
                );
            }
            Err(Error::LegacyUnhandled | Error::LegacyStorageUnhandled) => {
                assert_eq!(case["expected"]["status"], 500, "{}", case["name"]);
                assert_eq!(
                    case["expected"]["body"]["code"], "api.unhandled_exception",
                    "{}",
                    case["name"]
                );
            }
            Err(error) => panic!("{}: unexpected service error {error:?}", case["name"]),
        }
        let after = raw_records(&mut db).await;
        let actual_changed: BTreeSet<_> = before
            .keys()
            .chain(after.keys())
            .filter(|id| before.get(*id) != after.get(*id))
            .cloned()
            .collect();
        let expected_changes = case["expected_changes"]
            .as_array()
            .expect("Capture must state all durable changes");
        let expected_changed: BTreeSet<_> = expected_changes
            .iter()
            .map(|change| {
                change["after"]["payload"]["id"]
                    .as_str()
                    .unwrap()
                    .to_owned()
            })
            .collect();
        assert_eq!(
            actual_changed, expected_changed,
            "{}: changed entities",
            case["name"]
        );
        for change in expected_changes {
            let id = change["after"]["payload"]["id"].as_str().unwrap();
            let (kind, raw) = &after[id];
            assert_eq!(
                json!({"kind":kind,"payload":serde_json::from_str::<Value>(raw).unwrap()}),
                change["after"],
                "{}: durable {id}",
                case["name"]
            );
        }
        let search_after = search_rows(&mut db).await;
        let mut expected_search = search_before;
        for change in case["expected_search_changes"].as_array().unwrap() {
            if change["after"].is_null() {
                expected_search.remove(change["before"]["id"].as_str().unwrap());
            } else {
                expected_search.insert(
                    change["after"]["id"].as_str().unwrap().to_owned(),
                    change["after"].clone(),
                );
            }
        }
        assert_eq!(
            search_after, expected_search,
            "{}: saved search projections",
            case["name"]
        );
        repaired_on_refusal |= case["expected"]["status"] == 409
            && actual_changed.iter().any(|id| after[id].0 == "chat_turns");
        compared += 1;
    }
    assert_eq!(compared, 113, "Complete canonical service corpus");
    assert!(
        repaired_on_refusal,
        "Capture must verify repair commits before active-response refusal"
    );
    let final_rows = raw_records(&mut db).await;
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(raw_records(&mut db).await, final_rows);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn settings_preserve_ordered_opaque_choices_and_nullable_fields_after_reopen() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = clean(&path).await;
    let ordered = session(
        "ordered",
        true,
        json!({"keep":{"private":"retained"},"provider_subagent":{"provider_profile_id":"RAW_PROVIDER", "model":"RAW_MODEL", "max_active":{"legacy":"opaque"}}}),
    );
    let raw = ordered
        .to_string()
        .replace("\"RAW_PROVIDER\"", "{\"z\":1,\"a\":2}")
        .replace("\"RAW_MODEL\"", "[\"fixture\",{\"b\":false,\"a\":null}]");
    let ordered: Value = serde_json::from_str(&raw).unwrap();
    insert(&mut db, "chat_sessions", &ordered, Some(&raw)).await;
    for id in ["scalar-override", "scalar-error"] {
        insert(
            &mut db,
            "chat_sessions",
            &session(id, true, json!({"provider_subagent":true})),
            None,
        )
        .await;
    }
    let (store, service) = open(&path).await;
    service
        .update_session_settings("ordered", settings(json!({"title":"Renamed"})))
        .await
        .unwrap();
    let retained = store.settings_session("ordered").await.unwrap();
    assert!(retained.raw_payload.contains("{\"z\":1,\"a\":2}"));
    assert!(retained.raw_payload.contains("{\"b\":false,\"a\":null}"));
    store.shutdown().await.unwrap();
    let (store, service) = open(&path).await;
    let updated = service
        .update_session_settings(
            "ordered",
            settings(
                json!({"allow_subagents":true,"reasoning_effort":null,"max_active_subagents":null}),
            ),
        )
        .await
        .unwrap();
    assert_eq!(
        updated.payload()["metadata"]["provider_subagent"],
        json!({"provider_profile_id":"{'z': 1, 'a': 2}","model":"['fixture', {'b': False, 'a': None}]"})
    );
    assert_eq!(
        updated.payload()["metadata"]["reasoning_effort"],
        Value::Null
    );
    assert!(
        updated.payload()["metadata"]
            .as_object()
            .unwrap()
            .contains_key("reasoning_effort")
    );
    assert_eq!(
        updated.payload()["metadata"]["keep"],
        ordered["metadata"]["keep"]
    );
    assert_eq!(updated.payload()["revision"], 3);
    assert_eq!(updated.payload()["created_at"], ordered["created_at"]);
    assert_eq!(updated.payload()["updated_at"], "2030-01-01T12:00:00Z");
    let overridden = service.update_session_settings("scalar-override",settings(json!({"allow_subagents":true,"subagent_provider_id":"selected","subagent_model":"selected-model","max_active_subagents":null}))).await.unwrap();
    assert_eq!(
        overridden.payload()["metadata"]["provider_subagent"],
        json!({"provider_profile_id":"selected","model":"selected-model"})
    );
    assert!(matches!(
        service
            .update_session_settings("scalar-error", settings(json!({"allow_subagents":true})))
            .await,
        Err(Error::LegacyUnhandled)
    ));
    assert_eq!(payload(&mut db, "scalar-error").await["revision"], 1);
    let search: (i64, String) =
        sqlx::query_as("SELECT revision,label FROM search_documents WHERE resource_id='ordered'")
            .fetch_one(&mut db)
            .await
            .unwrap();
    assert_eq!(search, (3, "Renamed".into()));
    store.shutdown().await.unwrap();
    let (store, _) = open(&path).await;
    assert_eq!(
        store
            .settings_session("ordered")
            .await
            .unwrap()
            .record
            .payload(),
        updated.payload()
    );
    store.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn settings_repair_before_refusal_and_keep_independent_schedule_commits() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = clean(&path).await;
    let recovery: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-recovery.json")).unwrap();
    for row in recovery["initial_records"]
        .as_array()
        .unwrap()
        .iter()
        .chain(recovery["dependency_records"].as_array().unwrap())
        .filter(|r| {
            r["payload"]["id"]
                .as_str()
                .unwrap()
                .starts_with("adopt-complete")
        })
    {
        insert(
            &mut db,
            row["kind"].as_str().unwrap(),
            &row["payload"],
            None,
        )
        .await;
    }
    let parent = session("schedule-parent", false, json!({}));
    insert(&mut db, "chat_sessions", &parent, None).await;
    insert(
        &mut db,
        "chat_sessions",
        &session("unconfigured", false, json!({})),
        None,
    )
    .await;
    let schedule = json!({"id":"saved-schedule","engagement_id":parent["engagement_id"],"session_id":"schedule-parent","provider_profile_id":"not-configured-provider","model":parent["model"],"interval_seconds":3600,"next_run_at":"2020-01-01T01:00:00Z","revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z"});
    insert(&mut db, "chat_schedules", &schedule, None).await;
    let (store, service) = open(&path).await;
    let original_session = payload(&mut db, "adopt-complete").await;
    assert!(matches!(
        service
            .update_session_settings("adopt-complete", settings(json!({"title":"Refused title"})))
            .await,
        Err(Error::Conflict(
            "conversation cannot be changed while a response is active"
        ))
    ));
    assert_eq!(payload(&mut db, "adopt-complete").await, original_session);
    let repaired = payload(&mut db, "adopt-complete-turn").await;
    assert_eq!(repaired["revision"], 2);
    assert_eq!(repaired["execution_tool_calls"], 1);
    assert_eq!(
        repaired["request_snapshot"]["recovery"]["unknown_tool_call_ids"],
        json!([])
    );
    // An injected storage failure is confined to the second, schedule commit.
    sqlx::query("CREATE TRIGGER fail_schedule BEFORE UPDATE ON entities WHEN OLD.id='saved-schedule' BEGIN SELECT RAISE(ABORT,'fixture schedule write rejected'); END").execute(&mut db).await.unwrap();
    assert!(
        service
            .update_session_settings("schedule-parent", settings(json!({"archived":true})))
            .await
            .is_err()
    );
    assert_eq!(payload(&mut db, "schedule-parent").await["revision"], 2);
    assert_eq!(
        payload(&mut db, "schedule-parent").await["metadata"]["archived_at"],
        "2030-01-01T12:00:00+00:00"
    );
    assert_eq!(payload(&mut db, "saved-schedule").await["revision"], 1);
    sqlx::query("DROP TRIGGER fail_schedule")
        .execute(&mut db)
        .await
        .unwrap();
    // Enabling commits first even if the independent session unarchive fails.
    sqlx::query("CREATE TRIGGER fail_unarchive BEFORE UPDATE ON entities WHEN OLD.id='schedule-parent' AND json_extract(NEW.payload,'$.metadata.archived_at') IS NULL BEGIN SELECT RAISE(ABORT,'fixture unarchive rejected'); END").execute(&mut db).await.unwrap();
    assert!(
        service
            .write_schedule(
                "schedule-parent",
                ScheduleWrite {
                    expected_revision: 1.into(),
                    enabled: Some(true)
                }
            )
            .await
            .is_err()
    );
    let committed = payload(&mut db, "saved-schedule").await;
    assert_eq!(committed["revision"], 2);
    assert_eq!(committed["next_run_at"], "2030-01-01T13:00:00Z");
    assert_eq!(payload(&mut db, "schedule-parent").await["revision"], 2);
    sqlx::query("DROP TRIGGER fail_unarchive")
        .execute(&mut db)
        .await
        .unwrap();
    service
        .write_schedule(
            "schedule-parent",
            ScheduleWrite {
                expected_revision: 2.into(),
                enabled: Some(true),
            },
        )
        .await
        .unwrap();
    assert!(
        payload(&mut db, "schedule-parent").await["metadata"]
            .get("archived_at")
            .is_none()
    );
    // Configuring recurrence requires no configured provider record or process.
    let created = service
        .create_schedule(
            "unconfigured",
            ScheduleCreate {
                interval_seconds: 3600,
            },
            || "trusted-schedule-id".into(),
        )
        .await
        .unwrap();
    assert_eq!(created.payload()["enabled"], true);
    assert_eq!(created.payload()["next_run_at"], "2030-01-01T13:00:00Z");
    let final_rows: Vec<(String, String)> =
        sqlx::query_as("SELECT id,payload FROM entities ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap();
    store.shutdown().await.unwrap();
    let (store, _) = open(&path).await;
    assert_eq!(
        sqlx::query_as::<_, (String, String)>("SELECT id,payload FROM entities ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap(),
        final_rows
    );
    store.shutdown().await.unwrap();
    db.close().await.unwrap();
}
