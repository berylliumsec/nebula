#[path = "support/mutation_oracle.rs"]
mod mutation_oracle;

use chrono::{DateTime, Utc};
use nebula_assistant_transport::{Authentication, HttpConfig};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};

static CLOCKS: Mutex<VecDeque<DateTime<Utc>>> = Mutex::new(VecDeque::new());
static GENERATED_IDS: Mutex<VecDeque<String>> = Mutex::new(VecDeque::new());
static UUID_CALLS: AtomicUsize = AtomicUsize::new(0);
fn now() -> DateTime<Utc> {
    CLOCKS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected trusted clock sample")
}
fn new_id() -> String {
    UUID_CALLS.fetch_add(1, Ordering::SeqCst);
    GENERATED_IDS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected identity allocation")
}
fn stamp(value: &Value) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
}

#[tokio::test]
async fn python_goal_configuration_http_preserves_responses_clocks_and_durable_state() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../compatibility/python-goal-drafts.json"
    ))
    .unwrap();
    assert!(
        fixture.get("capture_phase").is_none(),
        "full HTTP oracle required"
    );
    mutation_oracle::run(
        &fixture,
        |fixture, case| {
            let ordinary = case.get("clock").unwrap_or(&fixture["clock"]);
            let mut clocks = CLOCKS.lock().unwrap();
            assert!(clocks.is_empty());
            // Authentication owns a separate source of current time. The remaining
            // samples bind the Python model/elapsed/writer observations exactly.
            clocks.push_back(stamp(ordinary));
            let mut model_index = 0;
            for label in case["expected_clock_calls"].as_array().unwrap() {
                let value = match label.as_str().unwrap() {
                    "model" => {
                        let value = case
                            .get("model_clock_values")
                            .and_then(|v| v.get(model_index))
                            .unwrap_or(ordinary);
                        model_index += 1;
                        value
                    }
                    "elapsed" => ordinary,
                    "writer" => case.get("writer_clock").unwrap_or(ordinary),
                    other => panic!("unreviewed clock authority {other}"),
                };
                clocks.push_back(stamp(value));
            }
            drop(clocks);
            *GENERATED_IDS.lock().unwrap() = case["generated_ids"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|v| v.as_str().unwrap().into())
                .collect();
            UUID_CALLS.store(0, Ordering::SeqCst);
            let mut config =
                HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
            config.clock = now;
            config.new_goal_id = new_id;
            config
        },
        |case| {
            assert_eq!(
                json!(UUID_CALLS.load(Ordering::SeqCst)),
                case["expected_uuid_calls"],
                "UUID guard timing: {}",
                case["name"]
            );
            assert!(
                CLOCKS.lock().unwrap().is_empty(),
                "missed trusted clock sample: {}",
                case["name"]
            );
        },
    )
    .await;
}
