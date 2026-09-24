#[path = "support/mutation_oracle.rs"]
mod mutation_oracle;

use chrono::{DateTime, Utc};
use nebula_assistant_transport::{Authentication, HttpConfig};
use serde_json::{Value, json};
use std::sync::{
    Mutex,
    atomic::{AtomicI64, AtomicUsize, Ordering},
};

static CLOCK: AtomicI64 = AtomicI64::new(0);
static GENERATED_ID: Mutex<String> = Mutex::new(String::new());
static UUID_CALLS: AtomicUsize = AtomicUsize::new(0);
fn generated_id() -> String {
    UUID_CALLS.fetch_add(1, Ordering::SeqCst);
    GENERATED_ID.lock().unwrap().clone()
}
fn now() -> DateTime<Utc> {
    DateTime::from_timestamp_micros(CLOCK.load(Ordering::SeqCst)).unwrap()
}
#[tokio::test]
async fn python_settings_http_oracle_preserves_mutations_search_and_partial_commits() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-settings.json")).unwrap();
    mutation_oracle::run(
        &fixture,
        |fixture, case| {
            CLOCK.store(
                DateTime::parse_from_rfc3339(
                    case.get("writer_clock")
                        .or_else(|| case.get("clock"))
                        .unwrap_or(&fixture["clock"])
                        .as_str()
                        .unwrap(),
                )
                .unwrap()
                .timestamp_micros(),
                Ordering::SeqCst,
            );
            *GENERATED_ID.lock().unwrap() = case["generated_ids"]
                .get(0)
                .and_then(Value::as_str)
                .unwrap_or("00000000-0000-4000-8000-000000000000")
                .into();
            UUID_CALLS.store(0, Ordering::SeqCst);
            let mut config =
                HttpConfig::new(Authentication::new("fixture-core".into(), true).unwrap());
            config.clock = now;
            config.new_schedule_id = generated_id;
            config
        },
        |case| {
            if let Some(expected) = case.get("expected_uuid_calls") {
                assert_eq!(
                    json!(UUID_CALLS.load(Ordering::SeqCst)),
                    *expected,
                    "UUID guard timing: {}",
                    case["name"]
                );
            }
        },
    )
    .await;
}
