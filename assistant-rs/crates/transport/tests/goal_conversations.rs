#[path = "support/mutation_oracle.rs"]
mod mutation_oracle;

use chrono::{DateTime, SecondsFormat, Timelike, Utc};
use nebula_assistant_domain::records::RecordError;
use nebula_assistant_storage::entities::ConversationDependencies;
use nebula_assistant_transport::{Authentication, HttpConfig};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::Duration,
};

static CLOCKS: Mutex<VecDeque<(DateTime<Utc>, bool)>> = Mutex::new(VecDeque::new());
static IDS: Mutex<VecDeque<String>> = Mutex::new(VecDeque::new());
static TRACE: Mutex<Vec<Value>> = Mutex::new(Vec::new());

fn stamp(value: &Value) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
}
fn now() -> DateTime<Utc> {
    let (value, model) = CLOCKS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected trusted clock sample");
    if model {
        let precision = if value.nanosecond() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        };
        TRACE.lock().unwrap().push(json!({
            "kind":"model", "value":value.to_rfc3339_opts(precision, false)
        }));
    }
    value
}
fn new_id() -> String {
    let value = IDS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected identity allocation");
    TRACE
        .lock()
        .unwrap()
        .push(json!({"kind":"uuid","value":value}));
    value
}

#[tokio::test]
async fn python_goal_conversation_http_preserves_atomic_state_and_factory_order() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../compatibility/python-goal-conversations.json"
    ))
    .unwrap();
    assert!(fixture.get("capture_phase").is_none());
    mutation_oracle::run(
        &fixture,
        |fixture, case| {
            let ordinary = case.get("clock").unwrap_or(&fixture["clock"]);
            let mut clocks = CLOCKS.lock().unwrap();
            assert!(clocks.is_empty());
            clocks.push_back((stamp(ordinary), false)); // Authentication only.
            for (index, label) in case["expected_clock_calls"]
                .as_array()
                .unwrap()
                .iter()
                .enumerate()
            {
                assert_eq!(label, "model", "unexpected atomic-create clock authority");
                let value = case
                    .get("model_clock_values")
                    .and_then(|values| values.get(index))
                    .unwrap_or(ordinary);
                clocks.push_back((stamp(value), true));
            }
            drop(clocks);
            *IDS.lock().unwrap() = case["generated_ids"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|value| value.as_str().unwrap().into())
                .collect();
            TRACE.lock().unwrap().clear();
            let environment = case
                .get("environment")
                .unwrap_or(&fixture["environment"])
                .clone();
            let dependencies = ConversationDependencies::with_resolver(
                4,
                Duration::from_secs(5),
                Arc::new(move |component| {
                    TRACE
                        .lock()
                        .unwrap()
                        .push(json!({"kind":"home","input":component}));
                    let value = if component == "~" {
                        environment["home"].as_str()
                    } else {
                        component
                            .strip_prefix('~')
                            .and_then(|user| environment["users"][user].as_str())
                    };
                    value
                        .map(str::to_owned)
                        .ok_or(RecordError::Invariant("fixture home unavailable"))
                }),
            )
            .unwrap()
            .with_lookup_observer(Arc::new(|kind, id| {
                TRACE
                    .lock()
                    .unwrap()
                    .push(json!({"kind":"lookup","entity_kind":kind.as_str(),"id":id}));
            }));
            let mut config =
                HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
            config.clock = now;
            config.new_goal_id = new_id;
            config.conversation_dependencies = dependencies;
            config
        },
        |case| {
            assert!(
                CLOCKS.lock().unwrap().is_empty(),
                "missed clock: {}",
                case["name"]
            );
            let trace = TRACE.lock().unwrap();
            let uuid_calls = trace.iter().filter(|event| event["kind"] == "uuid").count();
            assert_eq!(
                json!(uuid_calls),
                case["expected_uuid_calls"],
                "identity guards: {}",
                case["name"]
            );
            let home_calls: Vec<_> = trace
                .iter()
                .filter(|event| event["kind"] == "home")
                .map(|event| event["input"].clone())
                .collect();
            assert_eq!(
                json!(home_calls),
                case["expected_home_calls"],
                "home authority: {}",
                case["name"]
            );
            let lookups: Vec<_> = trace
                .iter()
                .filter(|event| event["kind"] == "lookup")
                .map(|event| json!({"kind":event["entity_kind"],"id":event["id"]}))
                .collect();
            assert_eq!(
                json!(lookups),
                case["expected_lookup_calls"],
                "lookup order: {}",
                case["name"]
            );
            assert_eq!(
                json!(*trace),
                case["expected_factory_trace"],
                "factory order: {}",
                case["name"]
            );
        },
    )
    .await;
}
