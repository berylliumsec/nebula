#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    records::RecordError,
    scope_policy::{ProviderPrivacyViolation, validate_provider_privacy, web_search_enabled},
};
use nebula_assistant_storage::entities::{
    Config, ConversationDependencies, DependencyReadBudget, Error, SqliteAssistantStore, StateClock,
};
use serde_json::{Value, json};
use sqlx::{Connection, Row, SqliteConnection};
use std::sync::{Arc, Mutex};
use std::time::Duration;
fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-scope-policy.json"
    ))
    .unwrap()
}
fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
async fn seed(db: &mut SqliteConnection, fixture: &Value) {
    for row in fixture["initial_entity_rows"].as_array().unwrap() {
        sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)")
        .bind(row["id"].as_str()).bind(row["kind"].as_str()).bind(row["engagement_id"].as_str()).bind(row["revision"].as_i64()).bind(row["payload"].as_str()).bind(row["chat_session_id"].as_str()).bind(row["created_at"].as_str()).bind(row["updated_at"].as_str()).execute(&mut *db).await.unwrap();
    }
}
async fn snapshot(db: &mut SqliteConnection) -> Vec<Value> {
    sqlx::query("SELECT * FROM entities ORDER BY id").fetch_all(&mut *db).await.unwrap().into_iter().map(|row|{
        json!({"id":row.get::<String,_>("id"),"kind":row.get::<String,_>("kind"),"engagement_id":row.get::<Option<String>,_>("engagement_id"),"revision":row.get::<i64,_>("revision"),"payload":row.get::<String,_>("payload"),"chat_session_id":row.get::<Option<String>,_>("chat_session_id"),"automation_run_id":row.get::<Option<String>,_>("automation_run_id"),"automation_session_id":row.get::<Option<String>,_>("automation_session_id"),"automation_status":row.get::<Option<String>,_>("automation_status"),"automation_expires_at":row.get::<Option<String>,_>("automation_expires_at"),"created_at":row.get::<String,_>("created_at"),"updated_at":row.get::<String,_>("updated_at")})
    }).collect()
}
fn environment() -> ConversationDependencies {
    ConversationDependencies::with_resolver(
        2,
        Duration::from_secs(2),
        Arc::new(|_| panic!("policy reads must not resolve host accounts")),
    )
    .unwrap()
}
#[tokio::test]
async fn python_scope_policy_privacy_reads_preserve_raw_rows_and_reopen() {
    let fixture = fixture();
    assert_eq!(fixture["privacy_cases"].as_array().unwrap().len(), 12);
    let temporary = tempfile::tempdir().unwrap();
    let path = temporary.path().join("nebula.db");
    support::database(&path).await;
    let mut raw = support::raw(&path).await;
    seed(&mut raw, &fixture).await;
    let before = snapshot(&mut raw).await;
    for reopen in 0..2 {
        let store = SqliteAssistantStore::open(&path, Config::default())
            .await
            .unwrap();
        for case in fixture["privacy_cases"].as_array().unwrap() {
            let clocks = Arc::new(Mutex::new(Vec::<Value>::new()));
            let seen = clocks.clone();
            let clock: Arc<StateClock> = Arc::new(move || {
                let mut values = seen.lock().unwrap();
                let time = now() + chrono::Duration::microseconds(values.len() as i64);
                values.push(
                    time.to_rfc3339_opts(
                        if time.timestamp_subsec_micros() == 0 {
                            chrono::SecondsFormat::Secs
                        } else {
                            chrono::SecondsFormat::Micros
                        },
                        false,
                    )
                    .into(),
                );
                time
            });
            let lookups = Arc::new(Mutex::new(Vec::<Value>::new()));
            let seen = lookups.clone();
            let environment = environment().with_lookup_observer(Arc::new(move |kind, id| {
                seen.lock()
                    .unwrap()
                    .push(json!({"kind":kind.as_str(),"id":id}))
            }));
            let mut budget = DependencyReadBudget::default();
            let identifier = case["engagement"]["scope_policy_id"]
                .as_str()
                .filter(|id| !id.is_empty());
            let policy = match identifier {
                None => Ok(None),
                Some(id) => store
                    .conversation_dependency(
                        DependencyKind::ScopePolicy,
                        id,
                        &environment,
                        clock,
                        &mut budget,
                    )
                    .await
                    .map(Some),
            };
            let actual = match policy {
                Ok(policy) => {
                    if let Some(policy) = &policy
                        && case.get("expected_policy").is_some()
                    {
                        assert_eq!(
                            policy.payload(),
                            &case["expected_policy"],
                            "{} reopen{reopen}",
                            case["name"]
                        );
                        assert_eq!(
                            web_search_enabled(policy).unwrap(),
                            case["expected_web_search_enabled"].as_bool().unwrap()
                        );
                    }
                    let decision = if identifier.is_none() {
                        Ok(())
                    } else {
                        validate_provider_privacy(
                            case["engagement"]["id"].as_str().unwrap(),
                            policy.as_ref(),
                            case["provider_local"].as_bool().unwrap(),
                        )
                    };
                    decision.map_err(|error| ("ProviderPrivacyViolation", error.to_string()))
                }
                Err(Error::NotFound) => Err((
                    "ProviderPrivacyViolation",
                    ProviderPrivacyViolation::MissingPolicy.to_string(),
                )),
                Err(Error::WrappedRecord(_)) => Err((
                    "CorruptRecordError",
                    format!("record {} failed validation", identifier.unwrap()),
                )),
                Err(error) => panic!("{} unexpected read error {error:?}", case["name"]),
            };
            if case["expected"]["accepted"] == true {
                assert!(actual.is_ok(), "{} {actual:?}", case["name"]);
            } else {
                let (kind, detail) = actual.unwrap_err();
                assert_eq!(kind, case["expected"]["error"]["kind"]);
                assert_eq!(detail, case["expected"]["error"]["detail"]);
            }
            assert_eq!(
                *clocks.lock().unwrap(),
                *case["expected_clock_values"].as_array().unwrap(),
                "{}",
                case["name"]
            );
            assert_eq!(
                *lookups.lock().unwrap(),
                *case["expected_lookups"].as_array().unwrap(),
                "{}",
                case["name"]
            );
            assert_eq!(snapshot(&mut raw).await, before);
        }
        store.shutdown().await.unwrap();
        drop(store);
    }
    raw.close().await.unwrap();
}
#[tokio::test]
async fn scope_policy_reads_fail_closed_on_envelopes_and_budget() {
    let fixture = fixture();
    let temporary = tempfile::tempdir().unwrap();
    let path = temporary.path().join("nebula.db");
    support::database(&path).await;
    let mut raw = support::raw(&path).await;
    seed(&mut raw, &fixture).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let environment = environment();
    let clock: Arc<StateClock> = Arc::new(now);
    for column in [
        "engagement_id",
        "revision",
        "id",
        "created_at",
        "updated_at",
        "chat_session_id",
    ] {
        let sql = match column {
            "engagement_id" => {
                "UPDATE entities SET engagement_id='foreign-envelope' WHERE id='allow'"
            }
            "revision" => "UPDATE entities SET revision=2 WHERE id='allow'",
            "id" => {
                "UPDATE entities SET payload=json_set(payload,'$.id','foreign-id') WHERE id='allow'"
            }
            "created_at" => {
                "UPDATE entities SET created_at='1999-01-01 00:00:00.000000' WHERE id='allow'"
            }
            "updated_at" => {
                "UPDATE entities SET updated_at='1999-01-01 00:00:00.000000' WHERE id='allow'"
            }
            _ => "UPDATE entities SET chat_session_id='foreign-session' WHERE id='allow'",
        };
        sqlx::query(sql).execute(&mut raw).await.unwrap();
        assert!(
            matches!(
                store
                    .conversation_dependency(
                        DependencyKind::ScopePolicy,
                        "allow",
                        &environment,
                        clock.clone(),
                        &mut DependencyReadBudget::default()
                    )
                    .await,
                Err(Error::CorruptEnvelope)
            ),
            "{column}"
        );
        sqlx::query("DELETE FROM entities WHERE id='allow'")
            .execute(&mut raw)
            .await
            .unwrap();
        let only = json!({"initial_entity_rows":[fixture["initial_entity_rows"].as_array().unwrap().iter().find(|r|r["id"]=="allow").unwrap()]});
        seed(&mut raw, &only).await;
    }
    let mut budget = DependencyReadBudget::default();
    for _ in 0..67 {
        store
            .conversation_dependency(
                DependencyKind::ScopePolicy,
                "allow",
                &environment,
                clock.clone(),
                &mut budget,
            )
            .await
            .unwrap();
    }
    assert!(matches!(
        store
            .conversation_dependency(
                DependencyKind::ScopePolicy,
                "allow",
                &environment,
                clock.clone(),
                &mut budget
            )
            .await,
        Err(Error::ReadLimit)
    ));
    let mut payload: Value = serde_json::from_str(
        fixture["initial_entity_rows"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["id"] == "allow")
            .unwrap()["payload"]
            .as_str()
            .unwrap(),
    )
    .unwrap();
    payload["allowed_domains"] = json!(["x".repeat(65537)]);
    sqlx::query("UPDATE entities SET payload=? WHERE id='allow'")
        .bind(payload.to_string())
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        store
            .conversation_dependency(
                DependencyKind::ScopePolicy,
                "allow",
                &environment,
                clock,
                &mut DependencyReadBudget::default()
            )
            .await,
        Err(Error::ReadLimit)
    ));
    // A caller cannot turn a foreign dependency into an unrestricted policy.
    let other=StoredDependency::decode(DependencyKind::Engagement,serde_json::to_string(&json!({"id":"project","revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z","name":"Project"})).unwrap().as_bytes()).unwrap();
    assert_eq!(
        validate_provider_privacy("project", Some(&other), true),
        Err(ProviderPrivacyViolation::MissingPolicy)
    );
    assert!(matches!(
        web_search_enabled(&other),
        Err(RecordError::UnknownKind)
    ));
    store.shutdown().await.unwrap();
    raw.close().await.unwrap();
}
