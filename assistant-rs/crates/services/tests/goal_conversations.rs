#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    dependencies::DependencyKind,
    records::{AssistantKind, RecordError},
};
use nebula_assistant_services::{
    AssistantRecords, Error, goal_conversations::GoalConversationCreate,
};
use nebula_assistant_storage::entities::{
    Config, ConversationDependencies, Error as StorageError, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn fixture_clock(value: &Value) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
}
static CLOCKS: Mutex<VecDeque<DateTime<Utc>>> = Mutex::new(VecDeque::new());
static TRACE: Mutex<Vec<Value>> = Mutex::new(Vec::new());
fn oracle_clock() -> DateTime<Utc> {
    let value = CLOCKS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected source factory clock");
    TRACE.lock().unwrap().push(
        json!({"kind":"model","value":value.to_rfc3339_opts(if value.timestamp_subsec_micros() == 0 { chrono::SecondsFormat::Secs } else { chrono::SecondsFormat::Micros },false)}),
    );
    value
}
static INDEPENDENT_CLOCKS: AtomicUsize = AtomicUsize::new(0);
fn counted_clock() -> DateTime<Utc> {
    now() + chrono::Duration::microseconds(INDEPENDENT_CLOCKS.fetch_add(1, Ordering::SeqCst) as i64)
}
fn environment() -> ConversationDependencies {
    ConversationDependencies::with_resolver(
        2,
        Duration::from_secs(2),
        Arc::new(|component| match component {
            "~" => Ok("/fixture/home/operator".into()),
            "~fixture" => Ok("/fixture/users/fixture".into()),
            _ => Err(RecordError::Invariant("unknown fixture account")),
        }),
    )
    .unwrap()
}
async fn seed(db: &mut SqliteConnection) {
    for (kind, id, fields) in [
        ("engagements", "project", json!({"name":"Project"})),
        (
            "providers",
            "provider",
            json!({"name":"Provider","provider_type":"openai"}),
        ),
    ] {
        let mut p = json!({"id":id,"revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z"});
        p.as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,?,?,1,?,NULL,'2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')").bind(id).bind(kind).bind((kind=="engagements").then_some(id)).bind(p.to_string()).execute(&mut*db).await.unwrap();
    }
}
fn body(objective: &str) -> GoalConversationCreate {
    serde_json::from_value(json!({"objective":objective,"completion_criteria":["Done"],"engagement_id":"project","provider_id":"provider","model":"fixture-model"})).unwrap()
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
async fn goal_conversation_collisions_rollback_search_and_consume_factories_in_source_order() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    seed(&mut db).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::with_clock(store.clone(), counted_clock);
    let env = environment();
    // Stale search survives a second-insert collision, even though the Session
    // insert temporarily updates it in the same SQLite transaction.
    sqlx::query("INSERT INTO search_documents(id,project_id,resource_kind,resource_id,revision,label,description,breadcrumb,content,updated_at) VALUES('new-session','legacy','conversation','new-session',7,'Old search','','Workbench','','2020-01-01 00:00:00.000000')").execute(&mut db).await.unwrap();
    let before = table_rows(&mut db, "entities").await;
    let search_before = table_rows(&mut db, "search_documents").await;
    for ids in [
        ["provider", "new-goal"],
        ["new-session", "provider"],
        ["same", "same"],
    ] {
        INDEPENDENT_CLOCKS.store(0, Ordering::SeqCst);
        let mut count = 0;
        let result = service
            .create_goal_conversation(body("Configure safely"), &env, || {
                let id = ids[count];
                count += 1;
                id.into()
            })
            .await;
        assert!(matches!(
            result,
            Err(Error::Storage(StorageError::AlreadyExists(_)))
        ));
        assert_eq!(count, 2);
        assert_eq!(INDEPENDENT_CLOCKS.load(Ordering::SeqCst), 4);
        assert_eq!(table_rows(&mut db, "entities").await, before);
        assert_eq!(table_rows(&mut db, "search_documents").await, search_before);
    }
    for (objective, ids, expected_ids, expected_clocks) in [
        ("Good", ["", "unused"], 1, 2),
        ("   ", ["blank-session", "blank-goal"], 2, 4),
    ] {
        INDEPENDENT_CLOCKS.store(0, Ordering::SeqCst);
        let mut count = 0;
        assert!(matches!(
            service
                .create_goal_conversation(body(objective), &env, || {
                    let id = ids[count];
                    count += 1;
                    id.into()
                })
                .await,
            Err(Error::RetainedModelValidation(_))
        ));
        assert_eq!(count, expected_ids);
        assert_eq!(INDEPENDENT_CLOCKS.load(Ordering::SeqCst), expected_clocks);
        assert_eq!(table_rows(&mut db, "entities").await, before);
    }
    INDEPENDENT_CLOCKS.store(0, Ordering::SeqCst);
    let mut ids = ["created-session", "created-goal"].into_iter();
    let saved = service
        .create_goal_conversation(body("Configure safely"), &env, || {
            ids.next().unwrap().into()
        })
        .await
        .unwrap();
    assert_eq!(
        saved.session.payload()["created_at"],
        "2030-01-01T12:00:00Z"
    );
    assert_eq!(
        saved.session.payload()["updated_at"],
        "2030-01-01T12:00:00.000001Z"
    );
    assert_eq!(
        saved.goal.payload()["created_at"],
        "2030-01-01T12:00:00.000002Z"
    );
    assert_eq!(
        saved.goal.payload()["updated_at"],
        "2030-01-01T12:00:00.000003Z"
    );
    let final_rows = table_rows(&mut db, "entities").await;
    let final_search = table_rows(&mut db, "search_documents").await;
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        reopened
            .get(AssistantKind::Session, "created-session")
            .await
            .unwrap()
            .payload(),
        saved.session.payload()
    );
    assert_eq!(
        reopened
            .get(AssistantKind::Goal, "created-goal")
            .await
            .unwrap()
            .payload(),
        saved.goal.payload()
    );
    assert_eq!(table_rows(&mut db, "entities").await, final_rows);
    assert_eq!(table_rows(&mut db, "search_documents").await, final_search);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn goal_conversation_titles_and_budget_numbers_preserve_python_boundaries() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    seed(&mut db).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::with_clock(store.clone(), now);
    let env = environment();
    let huge = format!("1{}", "0".repeat(100));
    let value:Value=serde_json::from_str(&format!(r#"{{"objective":"\u001cA\tβ\n🦀\u001f","completion_criteria":[" Done "],"engagement_id":"project","provider_id":"provider","model":"\u001c model \u001f","token_budget":{huge},"time_budget_seconds":{huge},"step_budget":{huge},"hook_ids":["hook"," hook "],"allow_subagents":true,"max_active_subagents":100}}"#)).unwrap();
    let request: GoalConversationCreate = serde_json::from_value(value).unwrap();
    for value in [
        &request.draft.token_budget,
        &request.draft.time_budget_seconds,
        &request.draft.step_budget,
    ] {
        assert_eq!(value.as_ref().unwrap().to_string(), huge);
    }
    let mut ids = ["unicode-session", "unicode-goal"].into_iter();
    let saved = service
        .create_goal_conversation(request, &env, || ids.next().unwrap().into())
        .await
        .unwrap();
    assert_eq!(saved.session.payload()["title"], "A β 🦀");
    assert_eq!(saved.session.payload()["model"], "\u{1c} model \u{1f}");
    assert_eq!(
        saved.session.payload()["metadata"]["hook_ids"],
        json!(["hook", " hook "])
    );
    assert_eq!(
        saved.session.payload()["metadata"]["max_active_subagents"],
        100
    );
    assert_eq!(saved.goal.payload()["token_budget"].to_string(), huge);
    let search: String =
        sqlx::query_scalar("SELECT description FROM search_documents WHERE id='unicode-session'")
            .fetch_one(&mut db)
            .await
            .unwrap();
    assert_eq!(search, "model");
    let objective = format!("{} \t🦀 trailing", "β".repeat(299));
    let mut ids = ["long-session", "long-goal"].into_iter();
    let saved = service
        .create_goal_conversation(body(&objective), &env, || ids.next().unwrap().into())
        .await
        .unwrap();
    assert_eq!(saved.session.payload()["title"], "β".repeat(299));
    let before = table_rows(&mut db, "entities").await;
    let mut oversized = body("Bound expansion");
    oversized.draft.completion_criteria = vec!["x".repeat(16 * 1024 * 1024)];
    let mut ids = ["oversized-session", "oversized-goal"].into_iter();
    assert!(matches!(
        service
            .create_goal_conversation(oversized, &env, || ids.next().unwrap().into())
            .await,
        Err(Error::Storage(StorageError::ReadLimit))
    ));
    assert_eq!(table_rows(&mut db, "entities").await, before);
    store.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn python_goal_conversations_match_durable_atomic_creation_and_factory_order() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../compatibility/python-goal-conversations.json"
    ))
    .unwrap();
    let cases = fixture["cases"]
        .as_array()
        .expect("completed atomic conversation oracle");
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    for statement in fixture["schema_sql"].as_array().unwrap() {
        let sql = statement
            .as_str()
            .unwrap()
            .replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
            .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
            .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ");
        sqlx::query(&sql).execute(&mut db).await.unwrap();
    }
    sqlx::query("DELETE FROM entities")
        .execute(&mut db)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut db)
        .await
        .unwrap();
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
    let lookups = Arc::new(Mutex::new(Vec::<Value>::new()));
    let homes = Arc::new(Mutex::new(Vec::<String>::new()));
    let seen = lookups.clone();
    let home_calls = homes.clone();
    let current_environment = Arc::new(Mutex::new(fixture["environment"].clone()));
    let home_environment = current_environment.clone();
    let env = ConversationDependencies::with_resolver(
        2,
        Duration::from_secs(2),
        Arc::new(move |input| {
            home_calls.lock().unwrap().push(input.to_owned());
            TRACE
                .lock()
                .unwrap()
                .push(json!({"kind":"home","input":input}));
            let environment = home_environment.lock().unwrap();
            if input == "~" {
                Ok(environment["home"].as_str().unwrap().to_owned())
            } else {
                Ok(environment["users"][input.strip_prefix('~').unwrap()]
                    .as_str()
                    .unwrap_or(input)
                    .to_owned())
            }
        }),
    )
    .unwrap()
    .with_lookup_observer(Arc::new(move |kind: DependencyKind, id| {
        seen.lock()
            .unwrap()
            .push(json!({"kind":kind.as_str(),"id":id}));
        TRACE
            .lock()
            .unwrap()
            .push(json!({"kind":"lookup","entity_kind":kind.as_str(),"id":id}));
    }));
    let mut compared = 0;
    for case in cases.iter().filter(|case| case["service"].is_object()) {
        if case["action"] == "reopen" {
            store.shutdown().await.unwrap();
            store = SqliteAssistantStore::open(&path, Config::default())
                .await
                .unwrap();
            service = AssistantRecords::with_clock(store.clone(), oracle_clock);
        }
        *current_environment.lock().unwrap() = case
            .get("environment")
            .unwrap_or(&fixture["environment"])
            .clone();
        TRACE.lock().unwrap().clear();
        lookups.lock().unwrap().clear();
        homes.lock().unwrap().clear();
        let observation = case.get("clock").unwrap_or(&fixture["clock"]);
        *CLOCKS.lock().unwrap() = case["expected_clock_calls"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
            .map(|(i, phase)| {
                assert_eq!(phase, "model");
                fixture_clock(
                    case["model_clock_values"]
                        .as_array()
                        .map(|values| &values[i])
                        .unwrap_or(observation),
                )
            })
            .collect();
        let request: GoalConversationCreate =
            serde_json::from_value(case["service"]["body"].clone()).unwrap();
        assert_eq!(
            serde_json::to_value(&request).unwrap(),
            case["service"]["body"],
            "{}: lexical normalized body",
            case["name"]
        );
        let before = by_id(table_rows(&mut db, "entities").await);
        let before_search = by_id(table_rows(&mut db, "search_documents").await);
        let mut uuid_calls = 0;
        let actual = service
            .create_goal_conversation(request, &env, || {
                let id = case["generated_ids"][uuid_calls]
                    .as_str()
                    .unwrap()
                    .to_owned();
                uuid_calls += 1;
                TRACE
                    .lock()
                    .unwrap()
                    .push(json!({"kind":"uuid","value":id}));
                id
            })
            .await;
        assert_eq!(
            json!(uuid_calls),
            case["expected_uuid_calls"],
            "{}",
            case["name"]
        );
        assert!(
            CLOCKS.lock().unwrap().is_empty(),
            "{}: unused clocks, error {:?}",
            case["name"],
            actual.as_ref().err().map(ToString::to_string)
        );
        assert_eq!(
            json!(*lookups.lock().unwrap()),
            case["expected_lookup_calls"],
            "{}: ordered lookups",
            case["name"]
        );
        assert_eq!(
            json!(*homes.lock().unwrap()),
            case["expected_home_calls"],
            "{}: lexical home observations",
            case["name"]
        );
        assert_eq!(
            json!(*TRACE.lock().unwrap()),
            case["expected_factory_trace"],
            "{}: exact read/factory interleaving",
            case["name"]
        );
        match actual {
            Ok(result) => {
                assert_eq!(case["expected"]["status"], 201, "{}", case["name"]);
                assert_eq!(
                    result.into_payload(),
                    case["expected"]["body"],
                    "{}",
                    case["name"]
                );
            }
            Err(
                error @ (Error::Conflict(_)
                | Error::DynamicConflict(_)
                | Error::Storage(StorageError::AlreadyExists(_))),
            ) => {
                assert_eq!(case["expected"]["status"], 409, "{}", case["name"]);
                assert_eq!(
                    error.to_string(),
                    case["expected"]["body"]["detail"],
                    "{}",
                    case["name"]
                );
            }
            Err(error @ Error::EntityNotFound { .. }) => {
                assert_eq!(case["expected"]["status"], 404, "{}", case["name"]);
                assert_eq!(
                    error.to_string(),
                    case["expected"]["body"]["detail"],
                    "{}",
                    case["name"]
                );
            }
            Err(Error::RetainedModelValidation(report)) => {
                assert_eq!(case["expected"]["status"], 422, "{}", case["name"]);
                assert_eq!(
                    serde_json::to_value(report).unwrap(),
                    case["expected"]["body"]["detail"],
                    "{}",
                    case["name"]
                );
            }
            Err(Error::LegacyStorageUnhandled) => {
                assert_eq!(case["expected"]["status"], 500, "{}", case["name"]);
                assert_eq!(case["expected"]["body"]["feature"], "storage");
            }
            Err(error) => panic!("{}: unexpected error {error:?}", case["name"]),
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
            "{}: complete entity mutations",
            case["name"]
        );
        for change in changes {
            assert!(
                change["before"].is_null(),
                "atomic creation cannot edit old rows"
            );
            let id = change["after"]["payload"]["id"].as_str().unwrap();
            let row = &after[id];
            let p: Value = serde_json::from_str(row["payload"].as_str().unwrap()).unwrap();
            assert_eq!(
                json!({"kind":row["kind"],"payload":p}),
                change["after"],
                "{}: durable {id}",
                case["name"]
            );
            for key in ["id", "revision", "engagement_id"] {
                assert_eq!(row[key], p[key], "{}: {key} envelope", case["name"]);
            }
            assert_eq!(row["chat_session_id"], p["session_id"]);
            for key in ["created_at", "updated_at"] {
                assert_eq!(
                    row[key],
                    fixture_clock(&p[key])
                        .format("%Y-%m-%d %H:%M:%S%.6f")
                        .to_string()
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
            by_id(table_rows(&mut db, "search_documents").await),
            expected_search,
            "{}: atomic search changes",
            case["name"]
        );
        for (table, rows) in protected {
            let mut expected = rows.as_array().unwrap().clone();
            expected.sort_by_key(Value::to_string);
            let mut actual = table_rows(&mut db, table).await;
            actual.sort_by_key(Value::to_string);
            assert_eq!(actual, expected, "{}: protected {table}", case["name"]);
        }
        compared += 1;
    }
    assert_eq!(
        compared,
        cases
            .iter()
            .filter(|case| case["service"].is_object())
            .count()
    );
    assert!(compared >= 40, "full sequential service capture required");
    let final_rows = table_rows(&mut db, "entities").await;
    let final_search = table_rows(&mut db, "search_documents").await;
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(table_rows(&mut db, "entities").await, final_rows);
    assert_eq!(table_rows(&mut db, "search_documents").await, final_search);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}
