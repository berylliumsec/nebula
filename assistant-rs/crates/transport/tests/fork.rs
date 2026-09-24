#[path = "support/mutation_oracle.rs"]
mod mutation_oracle;

use chrono::{DateTime, SecondsFormat, Timelike, Utc};
use nebula_assistant_transport::{Authentication, HttpConfig};
use serde_json::{Value, json};
use std::{collections::VecDeque, sync::Mutex};

static CLOCKS: Mutex<VecDeque<(DateTime<Utc>, bool)>> = Mutex::new(VecDeque::new());
static IDS: Mutex<VecDeque<String>> = Mutex::new(VecDeque::new());
static TRACE: Mutex<Vec<Value>> = Mutex::new(Vec::new());

fn stamp(value: &Value) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value.as_str().unwrap())
        .unwrap()
        .with_timezone(&Utc)
}
fn clock_event(value: DateTime<Utc>) -> Value {
    let precision = if value.nanosecond() == 0 {
        SecondsFormat::Secs
    } else {
        SecondsFormat::Micros
    };
    json!({"kind":"clock","value":value.to_rfc3339_opts(precision, false)})
}
fn now() -> DateTime<Utc> {
    let (value, traced) = CLOCKS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected fork clock sample");
    if traced {
        TRACE.lock().unwrap().push(clock_event(value));
    }
    value
}
fn new_id() -> String {
    let value = IDS
        .lock()
        .unwrap()
        .pop_front()
        .expect("unexpected fork identity allocation");
    TRACE
        .lock()
        .unwrap()
        .push(json!({"kind":"uuid","value":value}));
    value
}

#[tokio::test]
async fn python_fork_http_preserves_both_backends_committed_prefixes_and_factory_order() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-forks.json")).unwrap();
    assert!(fixture.get("capture_phase").is_none());
    let cases = fixture["cases"].as_array().expect("complete HTTP capture");
    assert!(cases.len() >= 40, "retain complete fork behavior coverage");
    assert!(cases.iter().all(|case| case["method"] == "POST"));
    mutation_oracle::run(
        &fixture,
        |fixture, case| {
            let mut clocks = CLOCKS.lock().unwrap();
            assert!(clocks.is_empty());
            // Authentication samples current time separately. Production uses
            // one trusted model/writer clock; compare its order against UUIDs.
            clocks.push_back((stamp(case.get("clock").unwrap_or(&fixture["clock"])), false));
            for event in case["expected_factory_trace"].as_array().unwrap() {
                match event["kind"].as_str().unwrap() {
                    "model" | "writer" => clocks.push_back((stamp(&event["value"]), true)),
                    "uuid" => {}
                    other => panic!("unreviewed factory observation {other}"),
                }
            }
            drop(clocks);
            *IDS.lock().unwrap() = case["generated_ids"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|value| value.as_str().unwrap().into())
                .collect();
            TRACE.lock().unwrap().clear();
            let mut config =
                HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
            config.clock = now;
            config.new_fork_id = new_id;
            config
        },
        |case| {
            assert!(
                CLOCKS.lock().unwrap().is_empty(),
                "missed clock: {}",
                case["name"]
            );
            let expected: Vec<_> = case["expected_factory_trace"]
                .as_array()
                .unwrap()
                .iter()
                .map(|event| match event["kind"].as_str().unwrap() {
                    "model" | "writer" => clock_event(stamp(&event["value"])),
                    "uuid" => event.clone(),
                    other => panic!("unreviewed factory observation {other}"),
                })
                .collect();
            assert_eq!(
                *TRACE.lock().unwrap(),
                expected,
                "factory order: {}",
                case["name"]
            );
        },
    )
    .await;
}
