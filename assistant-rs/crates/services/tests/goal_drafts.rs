#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_services::{
    AssistantRecords, Error,
    goal_drafts::{GoalDraft, GoalDraftUpdate},
};
use nebula_assistant_storage::entities::{
    Config, Error as StorageError, Mutation, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    path::Path,
    sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};

fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
static CREATE_CLOCK: AtomicUsize = AtomicUsize::new(0);
fn create_clock() -> DateTime<Utc> {
    now() + chrono::Duration::microseconds(CREATE_CLOCK.fetch_add(1, Ordering::SeqCst) as i64)
}
static UPDATE_CLOCK: AtomicUsize = AtomicUsize::new(0);
fn update_clock() -> DateTime<Utc> {
    UPDATE_CLOCK.fetch_add(1, Ordering::SeqCst);
    now()
}
fn draft(fields: Value) -> GoalDraft {
    serde_json::from_value(fields).unwrap()
}
fn update(fields: Value) -> GoalDraftUpdate {
    serde_json::from_value(fields).unwrap()
}
fn payload(kind: Kind, id: &str, session: &str, fields: Value) -> Value {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["revision"] = 1.into();
    p["created_at"] = json!("2020-01-01T00:00:00Z");
    p["updated_at"] = p["created_at"].clone();
    if matches!(kind, Kind::Goal | Kind::Turn) {
        p["session_id"] = session.into();
    }
    p.as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    p
}
fn record(kind: Kind, id: &str, session: &str, fields: Value) -> StoredAssistantRecord {
    StoredAssistantRecord::decode(
        kind,
        &serde_json::to_vec(&payload(kind, id, session, fields)).unwrap(),
    )
    .unwrap()
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
async fn rows(db: &mut SqliteConnection) -> BTreeMap<String, (String, String)> {
    let rows: Vec<(String, String, String)> =
        sqlx::query_as("SELECT id,kind,payload FROM entities ORDER BY id")
            .fetch_all(db)
            .await
            .unwrap();
    rows.into_iter()
        .map(|(id, kind, payload)| (id, (kind, payload)))
        .collect()
}
async fn search(db: &mut SqliteConnection) -> BTreeMap<String, Value> {
    let rows:Vec<String>=sqlx::query_scalar("SELECT json_object('id',id,'project_id',project_id,'resource_kind',resource_kind,'resource_id',resource_id,'revision',revision,'label',label,'description',description,'breadcrumb',breadcrumb,'content',content,'updated_at',updated_at) FROM search_documents ORDER BY id").fetch_all(db).await.unwrap();
    rows.into_iter()
        .map(|r| {
            let p: Value = serde_json::from_str(&r).unwrap();
            (p["id"].as_str().unwrap().into(), p)
        })
        .collect()
}
fn conflict(error: Error, expected: &str) {
    assert!(matches!(error,Error::Conflict(detail) if detail==expected));
}

static ORACLE_CLOCKS: Mutex<VecDeque<DateTime<Utc>>> = Mutex::new(VecDeque::new());
fn oracle_clock() -> DateTime<Utc> {
    ORACLE_CLOCKS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected semantic clock sample")
}
fn fixture_clock(value: &Value) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
}
fn prepare_clocks(case: &Value, fixture: &Value) {
    let observation = case.get("clock").unwrap_or(&fixture["clock"]);
    let mut model_index = 0;
    *ORACLE_CLOCKS.lock().unwrap() = case["expected_clock_calls"]
        .as_array()
        .unwrap()
        .iter()
        .map(|phase| {
            let value = match phase.as_str().unwrap() {
                "model" => {
                    let value = case["model_clock_values"]
                        .as_array()
                        .map(|values| &values[model_index])
                        .unwrap_or(observation);
                    model_index += 1;
                    value
                }
                "writer" => case.get("writer_clock").unwrap_or(observation),
                "elapsed" => observation,
                phase => panic!("unrecognized captured clock phase {phase}"),
            };
            fixture_clock(value)
        })
        .collect();
}
fn quoted(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}
async fn insert_row(db: &mut SqliteConnection, table: &str, row: &Value) {
    let fields = row.as_object().unwrap();
    let sql = format!(
        "INSERT INTO {} ({}) VALUES ({})",
        quoted(table),
        fields
            .keys()
            .map(|key| quoted(key))
            .collect::<Vec<_>>()
            .join(","),
        vec!["?"; fields.len()].join(",")
    );
    let mut query = sqlx::query(&sql);
    for value in fields.values() {
        query = match value {
            Value::Null => query.bind(None::<String>),
            Value::String(value) => query.bind(value),
            Value::Number(value) => query.bind(
                value
                    .as_i64()
                    .expect("fixture SQL envelopes use bounded integer columns"),
            ),
            value => panic!("unexpected raw SQL value {value:?}"),
        };
    }
    query.execute(db).await.unwrap();
}
async fn table_rows(db: &mut SqliteConnection, table: &str) -> Vec<Value> {
    let columns: Vec<String> =
        sqlx::query_scalar("SELECT name FROM pragma_table_info(?) ORDER BY cid")
            .bind(table)
            .fetch_all(&mut *db)
            .await
            .unwrap();
    let pairs = columns
        .iter()
        .map(|column| format!("'{}',{}", column.replace('\'', "''"), quoted(column)))
        .collect::<Vec<_>>()
        .join(",");
    let rows: Vec<String> = sqlx::query_scalar(&format!(
        "SELECT json_object({pairs}) FROM {} ORDER BY 1",
        quoted(table)
    ))
    .fetch_all(db)
    .await
    .unwrap();
    rows.into_iter()
        .map(|row| serde_json::from_str(&row).unwrap())
        .collect()
}
fn by_id(rows: Vec<Value>) -> BTreeMap<String, Value> {
    rows.into_iter()
        .map(|row| (row["id"].as_str().unwrap().to_owned(), row))
        .collect()
}

#[tokio::test]
async fn python_goal_drafts_oracle_matches_configuration_and_clock_boundaries() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../compatibility/python-goal-drafts.json"
    ))
    .unwrap();
    let cases = fixture["cases"]
        .as_array()
        .expect("completed Goal HTTP capture");
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = clean(&path).await;
    for statement in fixture["schema_sql"].as_array().unwrap() {
        let sql = statement
            .as_str()
            .unwrap()
            .replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
            .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
            .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ");
        sqlx::query(&sql).execute(&mut db).await.unwrap();
    }
    for row in fixture["initial_entity_rows"].as_array().unwrap() {
        insert_row(&mut db, "entities", row).await;
    }
    for row in fixture["initial_search_documents"].as_array().unwrap() {
        insert_row(&mut db, "search_documents", row).await;
    }
    let protected = fixture["initial_protected_tables"].as_object().unwrap();
    for (table, rows) in protected {
        sqlx::query(&format!("DELETE FROM {}", quoted(table)))
            .execute(&mut db)
            .await
            .unwrap();
        for row in rows.as_array().unwrap() {
            insert_row(&mut db, table, row).await;
        }
    }
    let mut store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let mut service = AssistantRecords::with_clock(store.clone(), oracle_clock);
    let mut expected_envelopes: BTreeMap<_, _> = fixture["initial_entity_rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            (
                row["id"].as_str().unwrap().to_owned(),
                json!({"kind":row["kind"],"payload":serde_json::from_str::<Value>(row["payload"].as_str().unwrap()).unwrap()}),
            )
        })
        .collect();
    let mut compared = 0;
    for case in cases {
        // Transport separately proves authentication, request coercion, the
        // custom writer-clock control and the read-only GET case. Only explicit
        // normalized service descriptors are accepted by this service runner.
        if !case["service"].is_object() {
            continue;
        }
        if case["action"] == "reopen" {
            store.shutdown().await.unwrap();
            store = SqliteAssistantStore::open(&path, Config::default())
                .await
                .unwrap();
            service = AssistantRecords::with_clock(store.clone(), oracle_clock);
        }
        prepare_clocks(case, &fixture);
        let descriptor = &case["service"];
        let session = descriptor["session_id"].as_str().unwrap();
        let before = by_id(table_rows(&mut db, "entities").await);
        let before_search = search(&mut db).await;
        let mut uuid_calls = 0;
        let actual = match descriptor["kind"].as_str().unwrap() {
            "goal_create" => {
                let body: GoalDraft = serde_json::from_value(descriptor["body"].clone()).unwrap();
                assert_eq!(
                    serde_json::to_value(&body).unwrap(),
                    descriptor["body"],
                    "{}: exact normalized integer inputs",
                    case["name"]
                );
                service
                    .create_goal(session, body, || {
                        let id = case["generated_ids"][uuid_calls]
                            .as_str()
                            .unwrap()
                            .to_owned();
                        uuid_calls += 1;
                        id
                    })
                    .await
            }
            "goal_update" => {
                let body: GoalDraftUpdate =
                    serde_json::from_value(descriptor["body"].clone()).unwrap();
                assert_eq!(
                    serde_json::to_value(&body).unwrap(),
                    descriptor["body"],
                    "{}: exact normalized integer inputs",
                    case["name"]
                );
                service.update_goal(session, body).await
            }
            kind => panic!("unrecognized Goal service {kind}"),
        };
        assert_eq!(
            case["expected_uuid_calls"],
            json!(uuid_calls),
            "{}: identity timing",
            case["name"]
        );
        assert!(
            ORACLE_CLOCKS.lock().unwrap().is_empty(),
            "{}: omitted semantic clock sample; service error: {:?}",
            case["name"],
            actual.as_ref().err().map(ToString::to_string)
        );
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
                | Error::RevisionConflict { .. }
                | Error::Storage(StorageError::AlreadyExists(_))),
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
            Err(error @ (Error::LegacyUnhandled | Error::LegacyStorageUnhandled)) => {
                assert_eq!(case["expected"]["status"], 500, "{}", case["name"]);
                assert_eq!(case["expected"]["body"]["code"], "api.unhandled_exception");
                assert_eq!(
                    case["expected"]["body"]["feature"],
                    if matches!(error, Error::LegacyStorageUnhandled) {
                        "storage"
                    } else {
                        "chat"
                    }
                );
            }
            Err(error) => panic!("{}: unexpected service error {error:?}", case["name"]),
        }
        let after = by_id(table_rows(&mut db, "entities").await);
        let changed: BTreeSet<_> = before
            .keys()
            .chain(after.keys())
            .filter(|id| before.get(*id) != after.get(*id))
            .cloned()
            .collect();
        let changes = case["expected_changes"].as_array().unwrap();
        let expected_changed: BTreeSet<_> = changes
            .iter()
            .map(|change| {
                change["after"]["payload"]["id"]
                    .as_str()
                    .unwrap()
                    .to_owned()
            })
            .collect();
        assert_eq!(
            changed, expected_changed,
            "{}: complete envelope and payload mutations",
            case["name"]
        );
        for change in changes {
            let id = change["after"]["payload"]["id"].as_str().unwrap();
            let row = &after[id];
            let p: Value = serde_json::from_str(row["payload"].as_str().unwrap()).unwrap();
            assert_eq!(
                json!({"kind":row["kind"],"payload":p}),
                change["after"],
                "{}: durable {id}",
                case["name"]
            );
            expected_envelopes.insert(id.to_owned(), change["after"].clone());
            for field in ["id", "revision", "engagement_id"] {
                assert_eq!(row[field], p[field], "{}: {field} envelope", case["name"]);
            }
            assert_eq!(row["chat_session_id"], p["session_id"]);
            for field in ["created_at", "updated_at"] {
                let time = fixture_clock(&p[field]);
                assert_eq!(
                    row[field],
                    time.format("%Y-%m-%d %H:%M:%S%.6f").to_string(),
                    "{}: {field} envelope",
                    case["name"]
                );
            }
        }
        let mut expected_search = before_search;
        for change in case["expected_search_changes"].as_array().unwrap() {
            if change["after"].is_null() {
                expected_search.remove(change["before"]["id"].as_str().unwrap());
            } else {
                expected_search.insert(
                    change["after"]["id"].as_str().unwrap().into(),
                    change["after"].clone(),
                );
            }
        }
        assert_eq!(
            search(&mut db).await,
            expected_search,
            "{}: search projection",
            case["name"]
        );
        for (table, expected) in protected {
            let mut rows = table_rows(&mut db, table).await;
            let mut expected = expected.as_array().unwrap().clone();
            rows.sort_by_key(Value::to_string);
            expected.sort_by_key(Value::to_string);
            assert_eq!(rows, expected, "{}: protected {table}", case["name"]);
        }
        compared += 1;
    }
    assert_eq!(
        compared, 93,
        "every normalized service case must be exercised"
    );
    let final_rows = table_rows(&mut db, "entities").await;
    let final_envelopes:BTreeMap<_,_>=final_rows.iter().map(|row|(row["id"].as_str().unwrap().to_owned(),json!({"kind":row["kind"],"payload":serde_json::from_str::<Value>(row["payload"].as_str().unwrap()).unwrap()}))).collect();
    // The two successful paired-device mutations are exercised by HTTP, so this
    // service-only expected state applies precisely the 93 declared operations.
    assert_eq!(final_envelopes, expected_envelopes, "all final entities");
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        table_rows(&mut db, "entities").await,
        final_rows,
        "raw envelopes survive reopen"
    );
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn goal_creation_defers_identity_and_factories_without_repairing_pending_work() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = clean(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.apply(vec![
        Mutation::Create(record(Kind::Session,"fresh","",json!({"provider_profile_id":"unconfigured-provider"}))),
        Mutation::Create(record(Kind::Session,"harness","",json!({"backend":"harness","provider_profile_id":null,"harness_profile_id":"unconfigured-harness","harness_session_id":"vendor-session"}))),
        Mutation::Create(record(Kind::Session,"invalid-objective","",json!({}))),
        Mutation::Create(record(Kind::Session,"multiple","",json!({}))),
        Mutation::Create(record(Kind::Goal,"a-goal","multiple",json!({}))),
        Mutation::Create(record(Kind::Goal,"z-goal","multiple",json!({}))),
        Mutation::Create(record(Kind::Turn,"pending","fresh",json!({"status":"interrupted","request_snapshot":{"recovery":{"required":true,"unknown_outcome_tool_call_ids":["missing-call"]}}}))),
    ]).await.unwrap();
    CREATE_CLOCK.store(0, Ordering::SeqCst);
    let service = AssistantRecords::with_clock(store.clone(), create_clock);
    for (session, expected) in [
        (
            "harness",
            "provider-backed goals require a provider conversation",
        ),
        (
            "multiple",
            "conversation has more than one authoritative goal",
        ),
    ] {
        let error = service
            .create_goal(
                session,
                draft(json!({"objective":"Goal","completion_criteria":["Done"]})),
                || panic!("identity before guards"),
            )
            .await
            .unwrap_err();
        conflict(error, expected);
    }
    assert!(matches!(
        service
            .create_goal(
                "missing",
                draft(json!({"objective":"Goal","completion_criteria":["Done"]})),
                || panic!("identity before missing session")
            )
            .await,
        Err(Error::EntityNotFound { .. })
    ));
    assert_eq!(CREATE_CLOCK.load(Ordering::SeqCst), 0);
    let before = rows(&mut db).await;
    let before_search = search(&mut db).await;
    let mut allocated = 0;
    let error = service
        .create_goal(
            "invalid-objective",
            draft(json!({"objective":"  ","completion_criteria":[""]})),
            || {
                allocated += 1;
                "invalid-goal".into()
            },
        )
        .await
        .unwrap_err();
    let Error::RetainedModelValidation(report) = error else {
        panic!("expected construction model validation")
    };
    assert_eq!(serde_json::to_value(report).unwrap()[0]["input"], "  ");
    assert_eq!(allocated, 1);
    assert_eq!(CREATE_CLOCK.load(Ordering::SeqCst), 2);
    assert_eq!(rows(&mut db).await, before);
    assert_eq!(search(&mut db).await, before_search);
    CREATE_CLOCK.store(0, Ordering::SeqCst);
    let saved=service.create_goal("fresh",draft(json!({"objective":"  Saved goal  ","completion_criteria":["  ","  Done  "],"plan":["  First  "]})),||"created-goal".into()).await.unwrap();
    let p = saved.payload();
    assert_eq!(p["objective"], "Saved goal");
    assert_eq!(p["completion_criteria"], json!(["", "Done"]));
    assert_eq!(p["plan"], json!(["First"]));
    assert_eq!(p["status"], "draft");
    assert_eq!(p["revision"], 1);
    assert_eq!(p["created_at"], "2030-01-01T12:00:00Z");
    assert_eq!(p["updated_at"], "2030-01-01T12:00:00.000001Z");
    assert_eq!(CREATE_CLOCK.load(Ordering::SeqCst), 2);
    let after = rows(&mut db).await;
    assert_eq!(after.len(), before.len() + 1);
    for (id, row) in &before {
        assert_eq!(&after[id], row, "unrelated {id} changed");
    }
    assert_eq!(search(&mut db).await, before_search);
    conflict(
        service
            .create_goal(
                "fresh",
                draft(json!({"objective":"Again","completion_criteria":["Done"]})),
                || panic!("duplicate must not allocate"),
            )
            .await
            .unwrap_err(),
        "conversation already has a goal",
    );
    assert_eq!(CREATE_CLOCK.load(Ordering::SeqCst), 2);
    assert_eq!(
        service.session_goal_with_clock("fresh").await.unwrap()["status"],
        "draft"
    );
    assert_eq!(
        CREATE_CLOCK.load(Ordering::SeqCst),
        2,
        "draft GET does not sample observation time"
    );
    db.close().await.unwrap();
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        reopened
            .get(Kind::Goal, "created-goal")
            .await
            .unwrap()
            .payload(),
        p
    );
    reopened.shutdown().await.unwrap();
}

#[tokio::test]
async fn goal_updates_preserve_budget_precedence_integer_precision_and_owned_state() {
    // A power of ten beyond u128 is especially important: serde_json's generic
    // Value deserializer can choose f64 even when its decimal display matches.
    // Its adjacent integer verifies that no rounding is hidden by that spelling.
    for digits in [
        format!("1{}", "0".repeat(100)),
        format!("1{}1", "0".repeat(99)),
    ] {
        let huge: Value = serde_json::from_str(&digits).unwrap();
        let fields = json!({"objective":"Exact integer","completion_criteria":["Done"],"plan":[],"token_budget":huge,"time_budget_seconds":huge,"step_budget":huge,"child_budget":null});
        assert_eq!(serde_json::to_value(draft(fields.clone())).unwrap(), fields);
        let mut fields = fields;
        fields["expected_revision"] = huge;
        assert_eq!(
            serde_json::to_value(update(fields.clone())).unwrap(),
            fields
        );
    }
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut db = clean(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let tokens: Value = serde_json::from_str("1000000000000000000000000000001").unwrap();
    store.apply(vec![
        Mutation::Create(record(Kind::Session,"edit","",json!({"backend":"harness","provider_profile_id":null,"harness_profile_id":"historical","harness_session_id":"vendor"}))),
        Mutation::Create(record(Kind::Goal,"goal","edit",json!({"engagement_id":"historical-project","status":"running","current_step":3,"children_started":2,"elapsed_seconds":3.25,"active_since":null,"usage":{"input_tokens":0,"output_tokens":0,"total_tokens":tokens},"execution_owner_id":"owner","execution_claim_id":"claim","execution_claimed_at":"2029-01-01T00:00:00Z","metadata":{"opaque":{"z":1,"a":2}},"skill_snapshots":[{"retained":true}],"child_session_ids":["child"],"linked_turn_ids":["pending"]}))),
        Mutation::Create(record(Kind::Turn,"pending","edit",json!({"status":"interrupted","request_snapshot":{"recovery":{"required":true}}}))),
    ]).await.unwrap();
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.metadata',json('{\"opaque\":{\"z\":1,\"a\":2}}')) WHERE id='goal'").execute(&mut db).await.unwrap();
    sqlx::query("INSERT INTO session_projections(session_id,revision,digest) VALUES('edit',71,'unchanged-goal')").execute(&mut db).await.unwrap();
    let service = AssistantRecords::with_clock(store.clone(), update_clock);
    let before = rows(&mut db).await;
    let before_search = search(&mut db).await;
    let fields = json!({"objective":" Updated ","completion_criteria":[" Done "],"expected_revision":1,"step_budget":2,"child_budget":1,"time_budget_seconds":3,"token_budget":1});
    let mut stale = fields.clone();
    stale["expected_revision"] = 2.into();
    UPDATE_CLOCK.store(0, Ordering::SeqCst);
    conflict(
        service
            .update_goal("edit", update(stale))
            .await
            .unwrap_err(),
        "goal changed on another device; review the latest goal before retrying",
    );
    conflict(
        service
            .update_goal("edit", update(fields.clone()))
            .await
            .unwrap_err(),
        "step budget cannot be lower than completed steps",
    );
    let mut child = fields.clone();
    child["step_budget"] = 3.into();
    conflict(
        service
            .update_goal("edit", update(child.clone()))
            .await
            .unwrap_err(),
        "child budget cannot be lower than children already started",
    );
    assert_eq!(UPDATE_CLOCK.load(Ordering::SeqCst), 0);
    child["child_budget"] = 2.into();
    conflict(
        service
            .update_goal("edit", update(child.clone()))
            .await
            .unwrap_err(),
        "time budget cannot be lower than time already used",
    );
    assert_eq!(UPDATE_CLOCK.load(Ordering::SeqCst), 1);
    child["time_budget_seconds"] = 4.into();
    conflict(
        service
            .update_goal("edit", update(child))
            .await
            .unwrap_err(),
        "token budget cannot be lower than tokens already used",
    );
    assert_eq!(UPDATE_CLOCK.load(Ordering::SeqCst), 2);
    assert_eq!(rows(&mut db).await, before);
    assert_eq!(search(&mut db).await, before_search);
    // Integer-to-float conversion would round this smaller limit up to elapsed.
    sqlx::query(
        "UPDATE entities SET payload=json_set(payload,'$.elapsed_seconds',json(?)) WHERE id='goal'",
    )
    .bind("9007199254740996.0")
    .execute(&mut db)
    .await
    .unwrap();
    let exact_raw: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='goal'")
        .fetch_one(&mut db)
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&exact_raw).unwrap()["elapsed_seconds"].as_f64(),
        Some(9_007_199_254_740_996.0)
    );
    let mut precise = json!({"objective":"Updated","completion_criteria":["Done"],"expected_revision":1,"time_budget_seconds":9007199254740995u64});
    conflict(
        service
            .update_goal("edit", update(precise.clone()))
            .await
            .unwrap_err(),
        "time budget cannot be lower than time already used",
    );
    precise["time_budget_seconds"] = 9007199254740996u64.into();
    precise["token_budget"] = tokens.clone();
    let saved = service.update_goal("edit", update(precise)).await.unwrap();
    assert_eq!(saved.payload()["revision"], 2);
    assert_eq!(saved.payload()["token_budget"], tokens);
    assert_eq!(saved.payload()["plan"], json!([]));
    assert!(saved.payload()["step_budget"].is_null());
    assert!(saved.payload()["child_budget"].is_null());
    for key in [
        "execution_owner_id",
        "execution_claim_id",
        "execution_claimed_at",
        "usage",
        "children_started",
        "child_session_ids",
        "linked_turn_ids",
        "skill_snapshots",
        "metadata",
        "status",
    ] {
        let prior: Value = serde_json::from_str(&before["goal"].1).unwrap();
        assert_eq!(saved.payload()[key], prior[key], "{key}");
    }
    let after = rows(&mut db).await;
    assert_eq!(after["pending"], before["pending"]);
    assert_eq!(after["edit"], before["edit"]);
    assert!(after["goal"].1.contains("\"opaque\":{\"z\":1,\"a\":2}"));
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.active_since','2030-01-01T11:00:00') WHERE id='goal'").execute(&mut db).await.unwrap();
    let removed=service.update_goal("edit",update(json!({"expected_revision":2,"objective":"No limits","completion_criteria":["Done"]}))).await.unwrap();
    assert_eq!(removed.payload()["revision"], 3);
    assert!(removed.payload()["time_budget_seconds"].is_null());
    let error=service.update_goal("edit",update(json!({"expected_revision":3,"objective":"Timed","completion_criteria":["Done"],"time_budget_seconds":1}))).await.unwrap_err();
    assert!(matches!(error, Error::LegacyUnhandled));
    let watermark: (i64, String) =
        sqlx::query_as("SELECT revision,digest FROM session_projections WHERE session_id='edit'")
            .fetch_one(&mut db)
            .await
            .unwrap();
    assert_eq!(watermark, (71, "unchanged-goal".into()));
    assert_eq!(search(&mut db).await, before_search);
    let final_rows = rows(&mut db).await;
    db.close().await.unwrap();
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let mut db = support::raw(&path).await;
    assert_eq!(rows(&mut db).await, final_rows);
    db.close().await.unwrap();
    reopened.shutdown().await.unwrap();
}
