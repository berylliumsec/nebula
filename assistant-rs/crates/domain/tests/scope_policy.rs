use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyEnvironment, DependencyKind, StoredDependency},
    model_validation::{Model, hydrate_scope_policy},
    records::{MAX_RECORD_BYTES, RecordError},
};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-scope-policy.json"
    ))
    .unwrap()
}
struct Environment {
    clock_values: Vec<Value>,
}
impl DependencyEnvironment for Environment {
    fn now(&mut self) -> DateTime<Utc> {
        let time = DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
            + chrono::Duration::microseconds(self.clock_values.len() as i64);
        self.clock_values.push(
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
    }
    fn expand_user(&mut self, _: &str) -> Result<String, RecordError> {
        panic!("scope policy hydration must not resolve host accounts")
    }
}
#[test]
fn python_scope_policy_hydration_and_grant_clocks_match_source() {
    let fixture = fixture();
    assert_eq!(fixture["vectors"].as_array().unwrap().len(), 148);
    for case in fixture["vectors"].as_array().unwrap() {
        let model = if case["model"] == "ScopePolicy" {
            Model::ScopePolicy
        } else {
            Model::MissionGrant
        };
        let mut env = Environment {
            clock_values: vec![],
        };
        let raw = case["raw_input"].as_str().unwrap();
        let actual = hydrate_scope_policy(model, raw.as_bytes(), &mut env);
        assert_eq!(
            env.clock_values,
            case["expected_clock_values"].as_array().unwrap().as_slice(),
            "{}",
            case["name"]
        );
        if case["expected"]["accepted"] == true {
            assert_eq!(
                actual.unwrap_or_else(|e| panic!("{} {e:?}", case["name"])),
                case["expected"]["payload"],
                "{}",
                case["name"]
            );
            if model == Model::ScopePolicy {
                let mut env = Environment {
                    clock_values: vec![],
                };
                assert_eq!(
                    StoredDependency::decode_with_environment(
                        DependencyKind::ScopePolicy,
                        raw.as_bytes(),
                        &mut env
                    )
                    .unwrap()
                    .payload(),
                    &case["expected"]["payload"],
                    "{}",
                    case["name"]
                );
            }
        } else {
            let Err(RecordError::ModelValidation(report)) = actual else {
                panic!("{} expected validation report: {actual:?}", case["name"]);
            };
            assert_eq!(
                serde_json::from_slice::<Value>(&report.to_json_bytes(MAX_RECORD_BYTES).unwrap())
                    .unwrap(),
                case["expected"]["errors"],
                "{}",
                case["name"]
            );
            assert_eq!(report.model_name(), case["model"].as_str().unwrap());
        }
    }
}
#[test]
fn scope_policy_retained_boundaries_and_diagnostics_stay_bounded() {
    let fixture = fixture();
    for case in fixture["strict_retained_boundary_observations"]
        .as_array()
        .unwrap()
    {
        let mut env = Environment {
            clock_values: vec![],
        };
        assert!(matches!(
            StoredDependency::decode_with_environment(
                DependencyKind::ScopePolicy,
                case["raw_input"].as_str().unwrap().as_bytes(),
                &mut env
            ),
            Err(RecordError::Shape(_))
        ));
        assert!(env.clock_values.is_empty());
    }
    let mut value = fixture["vectors"][0]["input"].clone();
    value["allowed_domains"] = json!(["x".repeat(65537)]);
    let mut env = Environment {
        clock_values: vec![],
    };
    assert_eq!(
        hydrate_scope_policy(Model::ScopePolicy, value.to_string().as_bytes(), &mut env)
            .unwrap_err(),
        RecordError::TooLarge
    );
    value["allowed_domains"] = json!(["private-probe.invalid/secret"]);
    let error = hydrate_scope_policy(Model::ScopePolicy, value.to_string().as_bytes(), &mut env)
        .unwrap_err();
    assert!(!format!("{error:?} {error}").contains("private-probe"));
    value["allowed_domains"] = json!([]);
    value["grants"] = json!(vec![Value::Null; 10001]);
    assert_eq!(
        hydrate_scope_policy(Model::ScopePolicy, value.to_string().as_bytes(), &mut env)
            .unwrap_err(),
        RecordError::TooLarge
    );
    assert!(StoredDependency::decode(DependencyKind::ScopePolicy, b"not-json").is_err());
    assert!(env.clock_values.is_empty());
}
