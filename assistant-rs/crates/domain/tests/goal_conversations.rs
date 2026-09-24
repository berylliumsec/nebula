use chrono::{DateTime, SecondsFormat, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyEnvironment, DependencyKind as Kind, StoredDependency},
    records::{AssistantKind, MAX_RECORD_BYTES, RecordError, StoredAssistantRecord},
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, VecDeque};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-goal-conversations.json"
    ))
    .unwrap()
}
fn time(value: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value)
        .unwrap()
        .with_timezone(&Utc)
}
struct Environment {
    times: VecDeque<DateTime<Utc>>,
    fallback: DateTime<Utc>,
    home: String,
    users: BTreeMap<String, String>,
    trace: Vec<Value>,
}
impl Environment {
    fn new(case: &Value) -> Self {
        Self {
            times: case["expected_factory_trace"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|event| event["kind"] == "model")
                .map(|event| time(event["value"].as_str().unwrap()))
                .collect(),
            fallback: time("2030-01-01T12:00:00Z"),
            home: case["environment"]["home"]
                .as_str()
                .unwrap_or("/fixture/home/operator")
                .into(),
            users: case["environment"]["users"].as_object().map_or_else(
                || [("fixture".into(), "/fixture/users/fixture".into())].into(),
                |users| {
                    users
                        .iter()
                        .map(|(name, home)| {
                            (
                                name.clone(),
                                home.as_str().expect("captured home string").into(),
                            )
                        })
                        .collect()
                },
            ),
            trace: Vec::new(),
        }
    }
}
impl DependencyEnvironment for Environment {
    fn now(&mut self) -> DateTime<Utc> {
        let value = self.times.pop_front().unwrap_or(self.fallback);
        let format = if value.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        };
        self.trace
            .push(json!({"kind":"model","value":value.to_rfc3339_opts(format,false)}));
        value
    }
    fn expand_user(&mut self, first_component: &str) -> Result<String, RecordError> {
        self.trace
            .push(json!({"kind":"home","input":first_component}));
        if first_component == "~" {
            return Ok(self.home.clone());
        }
        self.users
            .get(first_component.strip_prefix('~').unwrap_or_default())
            .cloned()
            .ok_or(RecordError::Invariant(
                "trusted fixture home is unavailable",
            ))
    }
}
fn provider() -> Value {
    json!({"id":"provider","revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z","name":"Fixture","provider_type":"opaque"})
}
fn project() -> Value {
    json!({"id":"project","revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z","name":"Fixture"})
}

#[test]
fn project_provider_hydration_matches_python_contextual_vectors() {
    let fixture = fixture();
    let vectors = fixture["dependency_vectors"].as_array().unwrap();
    assert!(vectors.len() >= 77);
    for case in vectors {
        let kind = Kind::try_from(case["kind"].as_str().unwrap()).unwrap();
        assert!(matches!(kind, Kind::Engagement | Kind::ProviderProfile));
        let raw = case["raw_input"].as_str().unwrap();
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
        let mut environment = Environment::new(case);
        let actual =
            StoredDependency::decode_with_environment(kind, raw.as_bytes(), &mut environment);
        if case["expected"]["accepted"] == true {
            let actual = actual.unwrap_or_else(|error| panic!("{}: {error:?}", case["name"]));
            assert_eq!(
                actual.payload(),
                &case["expected"]["payload"],
                "{}",
                case["name"]
            );
            assert_eq!(actual.kind(), kind);
        } else {
            assert!(
                actual.is_err(),
                "{}: malformed dependency must be rejected",
                case["name"]
            );
            assert!(
                !matches!(actual, Err(RecordError::Schema | RecordError::TooLarge)),
                "{}: ordinary model errors are not capacity/schema failures",
                case["name"]
            );
        }
        assert_eq!(
            json!(environment.trace),
            case["expected_factory_trace"],
            "{}: context observation order",
            case["name"]
        );
        assert!(
            environment.times.is_empty(),
            "{}: missing factory call",
            case["name"]
        );
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
    }
}

#[test]
fn dependency_context_is_lazy_ordered_bounded_and_never_implicit() {
    let mut p = provider();
    let mut environment = Environment::new(&Value::Null);
    let canonical = StoredDependency::decode_with_environment(
        Kind::ProviderProfile,
        &serde_json::to_vec(&p).unwrap(),
        &mut environment,
    )
    .unwrap();
    assert!(environment.trace.is_empty());
    assert!(
        StoredDependency::decode(
            Kind::ProviderProfile,
            &serde_json::to_vec(canonical.payload()).unwrap()
        )
        .is_ok()
    );
    p["capability_verifications"] = json!({"model":{"model":"model","status":"verified"}});
    let raw = serde_json::to_vec(&p).unwrap();
    assert!(matches!(
        StoredDependency::decode(Kind::ProviderProfile, &raw),
        Err(RecordError::Invariant(_))
    ));
    let record =
        StoredDependency::decode_with_environment(Kind::ProviderProfile, &raw, &mut environment)
            .unwrap();
    assert_eq!(environment.trace.len(), 1);
    assert_eq!(record.payload()["capabilities"]["tool_calling"], true);
    assert_eq!(
        record.payload()["capabilities"]["parallel_tool_calls"],
        false
    );
    assert!(
        serde_json::from_slice::<Value>(&raw).unwrap()["capability_verifications"]["model"]
            .get("checked_at")
            .is_none()
    );

    let mut p = project();
    p["workspace_path"] = "~fixture/a/../b//./".into();
    let raw = serde_json::to_vec(&p).unwrap();
    assert!(StoredDependency::decode(Kind::Engagement, &raw).is_err());
    let mut environment = Environment::new(&Value::Null);
    let record =
        StoredDependency::decode_with_environment(Kind::Engagement, &raw, &mut environment)
            .unwrap();
    assert_eq!(
        record.payload()["workspace_path"],
        "/fixture/users/fixture/a/../b"
    );
    assert_eq!(
        environment.trace,
        vec![json!({"kind":"home","input":"~fixture"})]
    );
    p["workspace_path"] = "~".into();
    environment.home = "/fixture/home ".into();
    let record = StoredDependency::decode_with_environment(
        Kind::Engagement,
        &serde_json::to_vec(&p).unwrap(),
        &mut environment,
    )
    .unwrap();
    assert_eq!(
        record.payload()["workspace_path"],
        "/fixture/home ",
        "home expansion is after typed trimming"
    );
    environment.home = "//".into();
    let record = StoredDependency::decode_with_environment(
        Kind::Engagement,
        &serde_json::to_vec(&p).unwrap(),
        &mut environment,
    )
    .unwrap();
    assert_eq!(record.payload()["workspace_path"], "//");
    environment.home = format!("/{}", "h".repeat(5000));
    let record = StoredDependency::decode_with_environment(
        Kind::Engagement,
        &serde_json::to_vec(&p).unwrap(),
        &mut environment,
    )
    .unwrap();
    assert_eq!(
        record.payload()["workspace_path"],
        environment.home,
        "4096 limit applies before expansion"
    );

    let mut p = provider();
    p["metadata"] = json!({"secret":"DO-NOT-PRINT-PROVIDER"});
    let record =
        StoredDependency::decode(Kind::ProviderProfile, &serde_json::to_vec(&p).unwrap()).unwrap();
    assert!(!format!("{record:?}").contains("DO-NOT-PRINT"));
    for case in fixture()["strict_retained_boundary_vectors"]
        .as_array()
        .unwrap()
    {
        let kind = Kind::try_from(case["kind"].as_str().unwrap()).unwrap();
        let mut environment = Environment::new(&Value::Null);
        assert!(
            StoredDependency::decode_with_environment(
                kind,
                case["raw_input"].as_str().unwrap().as_bytes(),
                &mut environment
            )
            .is_err()
        );
        assert!(
            environment.trace.is_empty(),
            "strict missing retained base fields do not invent factories"
        );
    }
}

#[test]
fn session_constructor_matches_python_inputs_defaults_and_clock_failures() {
    let fixture = fixture();
    let vectors = fixture["constructor_vectors"].as_array().unwrap();
    assert!(vectors.len() >= 12);
    for case in vectors {
        let raw = case["raw_input"].as_str().unwrap();
        let mut environment = Environment::new(case);
        let created = environment.now();
        let updated = environment.now();
        let actual = StoredAssistantRecord::decode_created_direct(
            AssistantKind::Session,
            raw.as_bytes(),
            created,
            updated,
        );
        if case["expected"]["accepted"] == true {
            let actual = actual.unwrap_or_else(|error| panic!("{}: {error:?}", case["name"]));
            assert_eq!(
                actual.payload(),
                &case["expected"]["payload"],
                "{}",
                case["name"]
            );
            assert_eq!(
                StoredAssistantRecord::decode(
                    AssistantKind::Session,
                    &serde_json::to_vec(actual.payload()).unwrap()
                )
                .unwrap(),
                actual
            );
        } else {
            let Err(RecordError::ModelValidation(report)) = actual else {
                panic!("{}: {actual:?}", case["name"]);
            };
            assert_eq!(report.model_name(), "ChatSession");
            assert_eq!(
                serde_json::from_slice::<Value>(&report.to_json_bytes(MAX_RECORD_BYTES).unwrap())
                    .unwrap(),
                case["expected"]["errors"],
                "{}",
                case["name"]
            );
            for issue in report.issues().filter(|issue| issue.loc.is_empty()) {
                assert_eq!(issue.input, &case["input"]);
                assert!(issue.input.get("created_at").is_none());
                assert!(issue.input.get("updated_at").is_none());
                assert!(issue.input.get("revision").is_none());
            }
        }
        assert_eq!(
            json!(environment.trace),
            case["expected_factory_trace"],
            "{}",
            case["name"]
        );
        assert!(environment.times.is_empty());
    }
}

#[test]
fn contextual_dependency_and_constructor_bounds_preserve_read_contracts() {
    let mut p = provider();
    p["model_allowlist"] = json!(vec!["model"; 10_001]);
    assert!(matches!(
        StoredDependency::decode(Kind::ProviderProfile, &serde_json::to_vec(&p).unwrap()),
        Err(RecordError::TooLarge)
    ));
    let mut p = provider();
    p["metadata"] = json!({"padding":""});
    let empty = serde_json::to_vec(&p).unwrap().len();
    p["metadata"]["padding"] = "x".repeat(MAX_RECORD_BYTES - empty - 32).into();
    let raw = serde_json::to_vec(&p).unwrap();
    assert!(raw.len() < MAX_RECORD_BYTES);
    assert!(
        matches!(
            StoredDependency::decode(Kind::ProviderProfile, &raw),
            Err(RecordError::TooLarge)
        ),
        "default expansion cannot exceed retained output budget"
    );
    let huge: Value = serde_json::from_str(&format!("1{}", "0".repeat(100))).unwrap();
    for (kind, mut p) in [
        (Kind::Engagement, project()),
        (Kind::ProviderProfile, provider()),
    ] {
        p["revision"] = huge.clone();
        let record = StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap();
        assert_eq!(
            record.payload()["revision"],
            huge,
            "SQL envelope bounds do not redefine the pure model integer contract"
        );
    }
    let raw = br#"{"id":"session","engagement_id":"project","title":"Title","provider_profile_id":"provider","model":"fixture"}"#;
    assert!(StoredAssistantRecord::decode_persisted(AssistantKind::Session, raw).is_err());
    assert!(StoredAssistantRecord::decode_persisted_direct(AssistantKind::Session, raw).is_err());
    let now = time("2030-01-01T12:00:00Z");
    let record =
        StoredAssistantRecord::decode_created_direct(AssistantKind::Session, raw, now, now)
            .unwrap();
    let mut corrupt = record.into_payload();
    corrupt["provider_profile_id"] = Value::Null;
    assert!(
        matches!(
            StoredAssistantRecord::decode_persisted(
                AssistantKind::Session,
                &serde_json::to_vec(&corrupt).unwrap()
            ),
            Err(RecordError::Invariant(_))
        ),
        "retained Session reads keep their previous sanitized surface"
    );
}
