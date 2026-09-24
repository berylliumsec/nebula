#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_services::{AssistantRecords, Error, fork::ForkRequest};
use nebula_assistant_storage::entities::{Config, Error as StorageError, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
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
static CLOCKS: Mutex<VecDeque<Value>> = Mutex::new(VecDeque::new());
static TRACE: Mutex<Vec<Value>> = Mutex::new(Vec::new());
fn oracle_clock() -> DateTime<Utc> {
    let entry = CLOCKS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected source clock");
    let value = fixture_clock(&entry["value"]);
    TRACE.lock().unwrap().push(entry);
    value
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

fn envelope(row: &Value) -> Value {
    json!({"kind":row["kind"],"payload":serde_json::from_str::<Value>(row["payload"].as_str().unwrap()).unwrap()})
}
fn changed_id(change: &Value) -> &str {
    change["after"]["payload"]["id"]
        .as_str()
        .or_else(|| change["before"]["payload"]["id"].as_str())
        .unwrap()
}
fn request(through: Option<&str>, before: Option<&str>) -> ForkRequest {
    ForkRequest {
        through_message_id: through.map(str::to_owned),
        before_message_id: before.map(str::to_owned),
        title: None,
    }
}

#[tokio::test]
async fn python_forks_preserve_both_backends_factory_order_and_durable_prefixes() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-forks.json")).unwrap();
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    for sql in fixture["schema_sql"].as_array().unwrap() {
        let sql = sql
            .as_str()
            .unwrap()
            .replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
            .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
            .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ");
        sqlx::query(&sql).execute(&mut db).await.unwrap();
    }
    for table in ["entities", "search_documents"] {
        sqlx::query(&format!("DELETE FROM {table}"))
            .execute(&mut db)
            .await
            .unwrap();
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
    let cases = fixture["cases"].as_array().unwrap();
    let mut compared = 0;
    for case in cases.iter().filter(|case| case["service"].is_object()) {
        if case["action"] == "reopen" {
            store.shutdown().await.unwrap();
            store = SqliteAssistantStore::open(&path, Config::default())
                .await
                .unwrap();
            service = AssistantRecords::with_clock(store.clone(), oracle_clock);
        }
        TRACE.lock().unwrap().clear();
        // Values are the trusted source clock observations, never client input.
        *CLOCKS.lock().unwrap() = case["expected_factory_trace"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|entry| entry["kind"] == "model" || entry["kind"] == "writer")
            .cloned()
            .collect();
        let body: ForkRequest = serde_json::from_value(case["service"]["body"].clone()).unwrap();
        assert_eq!(
            serde_json::to_value(&body).unwrap(),
            case["service"]["body"]
        );
        let before = by_id(table_rows(&mut db, "entities").await);
        let before_search = by_id(table_rows(&mut db, "search_documents").await);
        let cleanup_fault = case["fault"]["kind"] == "fork_cleanup_database_error";
        if cleanup_fault {
            let target = case["fault"]["harness_session_id"]
                .as_str()
                .unwrap()
                .replace('\'', "''");
            sqlx::query(&format!("CREATE TRIGGER fixture_fork_cleanup_failure BEFORE DELETE ON entities WHEN OLD.id='{target}' BEGIN SELECT RAISE(ABORT, 'fixture fork cleanup failure'); END"))
                .execute(&mut db).await.unwrap();
        }
        let calls = Arc::new(AtomicUsize::new(0));
        let uuid_calls = calls.clone();
        let generated: Vec<String> = case["generated_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|id| id.as_str().unwrap().to_owned())
            .collect();
        let actual = service
            .fork_conversation(
                case["service"]["session_id"].as_str().unwrap(),
                body,
                move || {
                    let id = generated[uuid_calls.fetch_add(1, Ordering::SeqCst)].clone();
                    TRACE
                        .lock()
                        .unwrap()
                        .push(json!({"kind":"uuid","value":id}));
                    id
                },
            )
            .await;
        if cleanup_fault {
            sqlx::query("DROP TRIGGER fixture_fork_cleanup_failure")
                .execute(&mut db)
                .await
                .unwrap();
        }
        assert_eq!(
            json!(calls.load(Ordering::SeqCst)),
            case["expected_uuid_calls"],
            "{} error {:?}",
            case["name"],
            actual.as_ref().err()
        );
        assert!(
            CLOCKS.lock().unwrap().is_empty(),
            "{} unused clocks error {:?}",
            case["name"],
            actual.as_ref().err()
        );
        assert_eq!(
            json!(*TRACE.lock().unwrap()),
            case["expected_factory_trace"],
            "{} factory interleave",
            case["name"]
        );
        match actual {
            Ok(record) => {
                assert_eq!(case["expected"]["status"], 201, "{}", case["name"]);
                assert_eq!(
                    record.payload(),
                    &case["expected"]["body"],
                    "{}",
                    case["name"]
                );
            }
            Err(
                error @ (Error::Conflict(_)
                | Error::HistoryConflict(_)
                | Error::HarnessState(_)
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
                assert_eq!(error.to_string(), case["expected"]["body"]["detail"]);
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
            Err(Error::LegacyUnhandled) => {
                assert_eq!(case["expected"]["status"], 500, "{}", case["name"]);
                assert_eq!(case["expected"]["body"]["feature"], "chat");
            }
            Err(Error::LegacyStorageUnhandled) => {
                assert_eq!(case["expected"]["status"], 500, "{}", case["name"]);
                assert_eq!(case["expected"]["body"]["feature"], "storage");
            }
            Err(error) => panic!("{} unexpected {error:?}", case["name"]),
        }
        let after = by_id(table_rows(&mut db, "entities").await);
        let changed: BTreeSet<_> = before
            .keys()
            .chain(after.keys())
            .filter(|id| before.get(*id) != after.get(*id))
            .cloned()
            .collect();
        let changes = case["expected_changes"].as_array().unwrap();
        assert_eq!(
            changed,
            changes.iter().map(|c| changed_id(c).to_owned()).collect(),
            "{} all entity changes",
            case["name"]
        );
        for change in changes {
            let id = changed_id(change);
            assert_eq!(
                before.get(id).map(envelope).unwrap_or(Value::Null),
                change["before"],
                "{} before {id}",
                case["name"]
            );
            assert_eq!(
                after.get(id).map(envelope).unwrap_or(Value::Null),
                change["after"],
                "{} after {id}",
                case["name"]
            );
            if let Some(row) = after.get(id) {
                let p = envelope(row)["payload"].clone();
                for key in ["id", "revision", "engagement_id"] {
                    assert_eq!(row[key], p[key], "{} envelope {key}", case["name"]);
                }
                let scope = if [
                    "chat_messages",
                    "chat_decisions",
                    "chat_goals",
                    "chat_turns",
                ]
                .contains(&row["kind"].as_str().unwrap())
                {
                    p["session_id"].clone()
                } else {
                    Value::Null
                };
                assert_eq!(
                    row["chat_session_id"], scope,
                    "{} session projection",
                    case["name"]
                );
                for key in ["created_at", "updated_at"] {
                    assert_eq!(
                        row[key],
                        fixture_clock(&p[key])
                            .format("%Y-%m-%d %H:%M:%S%.6f")
                            .to_string(),
                        "{} envelope time",
                        case["name"]
                    );
                }
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
            "{} search",
            case["name"]
        );
        for (table, rows) in protected {
            let mut expected = rows.as_array().unwrap().clone();
            expected.sort_by_key(Value::to_string);
            let mut actual = table_rows(&mut db, table).await;
            actual.sort_by_key(Value::to_string);
            assert_eq!(actual, expected, "{} protected {table}", case["name"]);
        }
        compared += 1;
    }
    assert_eq!(compared, 69);
    // The authorized paired-device body is included in service replay.
    // Transport-only invalid requests and auth refusals leave state unchanged.
    let expected = by_id(fixture["final_entity_rows"].as_array().unwrap().clone());
    let actual = by_id(table_rows(&mut db, "entities").await);
    assert_eq!(
        actual.keys().collect::<Vec<_>>(),
        expected.keys().collect::<Vec<_>>()
    );
    for (id, row) in &actual {
        assert_eq!(envelope(row), envelope(&expected[id]), "final {id}");
    }
    assert_eq!(
        by_id(table_rows(&mut db, "search_documents").await),
        by_id(
            fixture["final_search_documents"]
                .as_array()
                .unwrap()
                .clone()
        )
    );
    let raw_before = table_rows(&mut db, "entities").await;
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(table_rows(&mut db, "entities").await, raw_before);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn forks_copy_complete_history_with_exact_ties_and_shared_opaque_provenance() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    let base =
        r#""revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z""#;
    let source = format!(
        r#"{{"id":"large","engagement_id":"fork-project","title":"Large","provider_profile_id":"not-looked-up","model":"fixture",{base},"metadata":{{"archived_at":"old","temporary_assistant":true,"message_count":9999,"last_sequence":8888,"opaque":{{"z":1,"a":2}}}}}}"#
    );
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES('large','chat_sessions','fork-project',1,?,NULL,'2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')").bind(&source).execute(&mut db).await.unwrap();
    let huge = format!("1{}", "0".repeat(60));
    let mut tx = db.begin().await.unwrap();
    for index in 0..1003 {
        let sequence = if index < 1000 {
            (index + 1).to_string()
        } else {
            huge.clone()
        };
        let id = format!("large-{index:04}");
        let raw = format!(
            r#"{{"id":"{id}","engagement_id":"fork-project","session_id":"large","sequence":{sequence},"role":"assistant","content":"content {index}","reasoning":"not copied","elapsed_ms":12,"approval_wait_ms":3,"metadata":{{"opaque":{{"z":1,"a":2}}}},{base}}}"#
        );
        sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,'chat_messages','fork-project',1,?,'large','2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')").bind(&id).bind(raw).execute(&mut*tx).await.unwrap();
    }
    tx.commit().await.unwrap();
    let before = by_id(table_rows(&mut db, "entities").await);
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::with_clock(store.clone(), now);
    // XOR is an application guard even when the source does not exist.
    assert!(matches!(
        service
            .fork_conversation("absent", request(Some("one"), Some("two")), || panic!(
                "guard consumed UUID"
            ))
            .await,
        Err(Error::Invalid(_))
    ));
    for (prefix, body, expected) in [
        ("through", request(Some("large-1001"), None), 1003_i64),
        ("before", request(None, Some("large-1001")), 1000_i64),
    ] {
        let count = Arc::new(AtomicUsize::new(0));
        let saved_count = count.clone();
        let saved = service
            .fork_conversation("large", body, move || {
                format!("{prefix}-{:04}", saved_count.fetch_add(1, Ordering::SeqCst))
            })
            .await
            .unwrap();
        assert_eq!(count.load(Ordering::SeqCst) as i64, expected + 1);
        let p = saved.payload();
        assert_eq!(p["metadata"]["message_count"], 9999);
        assert_eq!(p["metadata"]["last_sequence"], 8888);
        assert!(p["metadata"].get("archived_at").is_none());
        assert!(p["metadata"].get("temporary_assistant").is_none());
        let id = p["id"].as_str().unwrap();
        let copied:Vec<String>=sqlx::query_scalar("SELECT payload FROM entities WHERE kind='chat_messages' AND chat_session_id=? ORDER BY id").bind(id).fetch_all(&mut db).await.unwrap();
        assert_eq!(copied.len() as i64, expected);
        for (index, raw) in copied.iter().enumerate() {
            let p: Value = serde_json::from_str(raw).unwrap();
            assert_eq!(p["reasoning"], "");
            assert!(p["elapsed_ms"].is_null());
            assert!(p["approval_wait_ms"].is_null());
            assert_eq!(p["source_message_id"], format!("large-{index:04}"));
            assert_eq!(
                p["metadata"]["fork_source_message_id"],
                p["source_message_id"]
            );
            assert!(
                raw.contains(r#""opaque":{"z":1,"a":2}"#),
                "opaque dict order changed"
            );
            if index >= 1000 {
                assert_eq!(p["sequence"].to_string(), huge);
            }
        }
        let raw: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id=?")
            .bind(id)
            .fetch_one(&mut db)
            .await
            .unwrap();
        assert!(raw.contains(r#""opaque":{"z":1,"a":2}"#));
        let search: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM search_documents WHERE id=?")
            .bind(id)
            .fetch_one(&mut db)
            .await
            .unwrap();
        assert_eq!(search, 1);
    }
    let after = by_id(table_rows(&mut db, "entities").await);
    for (id, row) in before {
        assert_eq!(after[&id], row, "source row changed {id}");
    }
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(by_id(table_rows(&mut db, "entities").await), after);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn cancelled_fork_callers_keep_bounded_workflows_until_commits_and_cleanup_finish() {
    use std::time::Duration;
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    for (kind, id, session, fields) in [
        (
            "chat_sessions",
            "cancel-source",
            None,
            json!({"title":"Source","provider_profile_id":"not-used","model":"fixture"}),
        ),
        (
            "chat_sessions",
            "cancel-h-source",
            None,
            json!({"title":"Harness","backend":"harness","harness_profile_id":"not-used","harness_session_id":"cancel-vendor","model":"fixture"}),
        ),
        (
            "harness_sessions",
            "cancel-vendor",
            None,
            json!({"harness_profile_id":"not-used","model":"fixture","status":"idle","last_activity_at":"2020-01-01T00:00:00Z"}),
        ),
        (
            "chat_messages",
            "cancel-message",
            Some("cancel-source"),
            json!({"session_id":"cancel-source","sequence":1,"role":"assistant","content":"Retained"}),
        ),
        (
            "chat_messages",
            "cancel-h-message",
            Some("cancel-h-source"),
            json!({"session_id":"cancel-h-source","sequence":1,"role":"assistant","content":"Retained harness"}),
        ),
    ] {
        let mut p = json!({"id":id,"engagement_id":"cancel-project","revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z"});
        p.as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,?,'cancel-project',1,?,?,'2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')").bind(id).bind(kind).bind(p.to_string()).bind(session).execute(&mut db).await.unwrap();
    }
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::with_clock(store.clone(), now);
    // A real SQLite lock holds the first commit without blocking Tokio workers.
    let lock = db.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let started = Arc::new(AtomicUsize::new(0));
    let mut awaiters = Vec::new();
    for index in 0..4 {
        let service = service.clone();
        let seen = started.clone();
        awaiters.push(tokio::spawn(async move {
            let mut counter = 0;
            let harness = index == 3;
            service
                .fork_conversation(
                    if harness {
                        "cancel-h-source"
                    } else {
                        "cancel-source"
                    },
                    request(
                        Some(if harness {
                            "cancel-h-message"
                        } else {
                            "cancel-message"
                        }),
                        None,
                    ),
                    move || {
                        if counter == 0 {
                            seen.fetch_add(1, Ordering::SeqCst);
                        }
                        let id = if harness {
                            match counter {
                                0 => " cancel-v-new ".into(),
                                1 => "cancel-h-new".into(),
                                2 => "cancel-message".into(),
                                _ => panic!("unexpected late fork factory"),
                            }
                        } else {
                            format!("cancel-p{index}-{counter}")
                        };
                        counter += 1;
                        id
                    },
                )
                .await
        }));
    }
    tokio::time::timeout(Duration::from_secs(2), async {
        while started.load(Ordering::SeqCst) < 4 {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    for awaiter in awaiters {
        awaiter.abort();
        assert!(awaiter.await.unwrap_err().is_cancelled());
    }
    assert!(matches!(
        service
            .fork_conversation(
                "cancel-source",
                request(Some("cancel-message"), None),
                || panic!("overload consumed factory")
            )
            .await,
        Err(Error::Storage(StorageError::Capacity))
    ));
    lock.commit().await.unwrap();
    // Source-preserving successful branches finish; a late harness collision
    // leaves its committed chat prefix and cleans up the canonical vendor ID.
    tokio::time::timeout(Duration::from_secs(5),async {
        loop {
            let complete:i64=sqlx::query_scalar("SELECT count(*) FROM entities WHERE id IN ('cancel-p0-1','cancel-p1-1','cancel-p2-1','cancel-h-new')").fetch_one(&mut db).await.unwrap();
            let vendor:i64=sqlx::query_scalar("SELECT count(*) FROM entities WHERE id='cancel-v-new'").fetch_one(&mut db).await.unwrap();
            if complete==4 && vendor==0 {
                let mut permits=Vec::new();for _ in 0..4 {match store.fork_workflow(){Ok(p)=>permits.push(p),Err(StorageError::Capacity)=>break,Err(e)=>panic!("workflow admission {e:?}")}}
                if permits.len()==4 {break;}
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }).await.unwrap();
    let raw: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='cancel-h-new'")
        .fetch_one(&mut db)
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&raw).unwrap()["harness_session_id"],
        "cancel-v-new"
    );
    let source_vendor: i64 =
        sqlx::query_scalar("SELECT count(*) FROM entities WHERE id='cancel-vendor'")
            .fetch_one(&mut db)
            .await
            .unwrap();
    assert_eq!(source_vendor, 1);
    let before = table_rows(&mut db, "entities").await;
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(table_rows(&mut db, "entities").await, before);
    reopened.shutdown().await.unwrap();
    db.close().await.unwrap();
}
