use chrono::{DateTime, SecondsFormat, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyEnvironment, DependencyKind, StoredDependency},
    model_validation::{
        CreatedEntityDefaults, InputOrigin, Location, Model, TypedModelPath, hydrate,
        hydrate_fork_created, hydrate_harness_session,
    },
    records::{AssistantKind, MAX_RECORD_BYTES, RecordError, StoredAssistantRecord},
};
use serde_json::{Value, json};
use std::collections::VecDeque;

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-forks.json")).unwrap()
}
fn model(name: &str) -> Model {
    match name {
        "HarnessSession" => Model::HarnessSession,
        "ChatSession" => Model::ChatSession,
        "ChatMessage" => Model::ChatMessage,
        "ChatContentBlock" => Model::ChatContentBlock,
        "ChatCitation" => Model::ChatCitation,
        "ChatDecision" => Model::ChatDecision,
        "ChatGoal" => Model::ChatGoal,
        "ChatTokenUsage" => Model::ChatTokenUsage,
        _ => panic!("unexpected fixture model"),
    }
}
fn kind(model: Model) -> Option<AssistantKind> {
    match model {
        Model::ChatSession => Some(AssistantKind::Session),
        Model::ChatMessage => Some(AssistantKind::Message),
        Model::ChatDecision => Some(AssistantKind::Decision),
        Model::ChatGoal => Some(AssistantKind::Goal),
        _ => None,
    }
}
fn time(value: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value)
        .unwrap()
        .with_timezone(&Utc)
}
struct Factories {
    clock: VecDeque<DateTime<Utc>>,
    fallback: DateTime<Utc>,
    ids: VecDeque<String>,
    trace: Vec<Value>,
}
impl Factories {
    fn new(case: &Value) -> Self {
        Self {
            clock: case["model_clock_values"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|value| time(value.as_str().unwrap()))
                .collect(),
            fallback: time("2030-01-01T12:00:00Z"),
            ids: case["generated_ids"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|value| value.as_str().unwrap().to_owned())
                .collect(),
            trace: Vec::new(),
        }
    }
    fn uuid(&mut self) -> String {
        let value = self.ids.pop_front().expect("captured UUID output");
        self.trace.push(json!({"kind":"uuid","value":value}));
        value
    }
    fn defaults(&mut self, model: Model, input: &Value) -> CreatedEntityDefaults {
        let id = input.get("id").is_none().then(|| self.uuid());
        let created_at = if input.get("created_at").is_none() {
            self.now()
        } else {
            self.fallback
        };
        let updated_at = if input.get("updated_at").is_none() {
            self.now()
        } else {
            self.fallback
        };
        let last_activity_at = (model == Model::HarnessSession
            && input.get("last_activity_at").is_none())
        .then(|| self.now());
        CreatedEntityDefaults {
            id,
            created_at,
            updated_at,
            last_activity_at,
        }
    }
}
impl DependencyEnvironment for Factories {
    fn now(&mut self) -> DateTime<Utc> {
        let value = self.clock.pop_front().unwrap_or(self.fallback);
        self.trace.push(json!({"kind":"model","value":value.to_rfc3339_opts(if value.timestamp_subsec_micros()==0{SecondsFormat::Secs}else{SecondsFormat::Micros},false)}));
        value
    }
    fn expand_user(&mut self, _: &str) -> Result<String, RecordError> {
        panic!("fork contracts must not look up a host account")
    }
}
fn typed_paths(case: &Value) -> Vec<TypedModelPath> {
    case["typed_paths"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|entry| TypedModelPath {
            path: entry["path"]
                .as_array()
                .unwrap()
                .iter()
                .map(|part| match part {
                    Value::String(value) => Location::Field(value.clone()),
                    Value::Number(value) => Location::Index(value.as_u64().unwrap() as usize),
                    _ => panic!("invalid fixture path"),
                })
                .collect(),
            model: model(entry["model"].as_str().unwrap()),
        })
        .collect()
}
fn assert_case(case: &Value, result: Result<Value, RecordError>) {
    if case["expected"]["accepted"] == true {
        assert_eq!(
            result.unwrap_or_else(|error| panic!("{}: {error:?}", case["name"])),
            case["expected"]["payload"],
            "{}",
            case["name"]
        );
    } else {
        let Err(RecordError::ModelValidation(report)) = result else {
            panic!("{} expected report, got {result:?}", case["name"]);
        };
        assert_eq!(report.model_name(), case["model"].as_str().unwrap());
        assert_eq!(
            serde_json::from_slice::<Value>(&report.to_json_bytes(MAX_RECORD_BYTES).unwrap())
                .unwrap(),
            case["expected"]["errors"],
            "{}",
            case["name"]
        );
    }
}

#[test]
fn fork_models_match_python_hydration_and_validation_vectors() {
    let fixture = fixture();
    let cases = fixture["model_vectors"].as_array().unwrap();
    assert!(cases.len() >= 74);
    for case in cases {
        let raw = case["raw_input"].as_str().unwrap();
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
        let model = model(case["model"].as_str().unwrap());
        let mut environment = Factories::new(case);
        let result = if model == Model::HarnessSession {
            hydrate_harness_session(raw.as_bytes(), &mut environment)
        } else {
            hydrate(model, InputOrigin::RetainedJson, raw.as_bytes())
        };
        assert_case(case, result);
        assert_eq!(
            &environment.trace,
            case["expected_factory_trace"].as_array().unwrap(),
            "{}",
            case["name"]
        );
        if case["expected"]["accepted"] == true
            && let Some(kind) = kind(model)
        {
            let record =
                StoredAssistantRecord::decode_fork_persisted_direct(kind, raw.as_bytes()).unwrap();
            assert_eq!(record.payload(), &case["expected"]["payload"]);
        }
        assert_eq!(
            serde_json::from_str::<Value>(raw).unwrap(),
            case["input"],
            "unchanged raw input"
        );
    }
    for case in fixture["strict_retained_boundary_vectors"]
        .as_array()
        .unwrap()
    {
        let mut environment = Factories::new(case);
        let model = model(case["model"].as_str().unwrap());
        let raw = case["raw_input"].as_str().unwrap().as_bytes();
        let result = if model == Model::HarnessSession {
            hydrate_harness_session(raw, &mut environment)
        } else {
            hydrate(model, InputOrigin::RetainedJson, raw)
        };
        assert!(
            matches!(result, Err(RecordError::Shape(_))),
            "{}",
            case["name"]
        );
        assert!(
            environment.trace.is_empty(),
            "strict missing-base boundary never invents factories"
        );
    }
}

#[test]
fn fork_constructors_preserve_factory_inputs_and_nested_model_provenance() {
    let fixture = fixture();
    let cases = fixture["constructor_vectors"].as_array().unwrap();
    assert!(cases.len() >= 8);
    for case in cases {
        let model = model(case["model"].as_str().unwrap());
        let raw = case["raw_input"].as_str().unwrap().as_bytes();
        assert_eq!(serde_json::from_slice::<Value>(raw).unwrap(), case["input"]);
        let mut factories = Factories::new(case);
        let defaults = factories.defaults(model, &case["input"]);
        let typed = typed_paths(case);
        let result = hydrate_fork_created(model, raw, &defaults, &typed);
        if let Err(RecordError::ModelValidation(report)) = &result {
            for entry in &typed {
                assert_eq!(report.model_input_at(&entry.path), Some(entry.model));
            }
            if report.issues().any(|issue| issue.loc.is_empty()) {
                let issue = report.issues().find(|issue| issue.loc.is_empty()).unwrap();
                assert_eq!(issue.input, &case["input"]);
                assert!(issue.input.get("created_at").is_none());
                assert!(issue.input.get("revision").is_none());
            }
        }
        assert_case(case, result);
        assert_eq!(
            &factories.trace,
            case["expected_factory_trace"].as_array().unwrap(),
            "{}",
            case["name"]
        );
        if case["expected"]["accepted"] == true {
            let payload = if model == Model::HarnessSession {
                StoredDependency::decode_harness_session_created(raw, &defaults)
                    .unwrap()
                    .payload()
                    .clone()
            } else {
                StoredAssistantRecord::decode_fork_created(
                    kind(model).unwrap(),
                    raw,
                    &defaults,
                    &typed,
                )
                .unwrap()
                .into_payload()
            };
            assert_eq!(payload, case["expected"]["payload"]);
        }
    }
}

#[test]
fn fork_harness_reads_are_contextual_lazy_and_wrap_model_errors() {
    let fixture = fixture();
    for case in fixture["model_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| case["model"] == "HarnessSession")
    {
        let mut environment = Factories::new(case);
        let raw = case["raw_input"].as_str().unwrap().as_bytes();
        let result = StoredDependency::decode_with_environment(
            DependencyKind::HarnessSession,
            raw,
            &mut environment,
        );
        assert_eq!(
            &environment.trace,
            case["expected_factory_trace"].as_array().unwrap()
        );
        if case["expected"]["accepted"] == true {
            assert_eq!(result.unwrap().payload(), &case["expected"]["payload"]);
        } else {
            assert!(
                matches!(result, Err(RecordError::Shape("harness_sessions"))),
                "{}",
                case["name"]
            );
        }
        if case["input"].get("last_activity_at").is_none() {
            assert!(matches!(
                StoredDependency::decode(DependencyKind::HarnessSession, raw),
                Err(RecordError::Shape(_))
            ));
        }
    }
    let mut record = fixture["model_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == "HarnessSession-defaults")
        .unwrap()["input"]
        .clone();
    record["metadata"] = json!({"opaque":"do-not-print-fork-secret"});
    record["last_activity_at"] = "2030-01-01T11:00:00-02:00".into();
    let actual = StoredDependency::decode(
        DependencyKind::HarnessSession,
        &serde_json::to_vec(&record).unwrap(),
    )
    .unwrap();
    assert_eq!(
        actual.payload()["last_activity_at"],
        record["last_activity_at"]
    );
    assert!(!format!("{actual:?}").contains("do-not-print"));
    assert_eq!(actual.payload()["external_session_id"], Value::Null);
}

#[test]
fn fork_model_bounds_and_direct_read_extensions_are_explicit() {
    let fixture = fixture();
    let case = fixture["model_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == "message-sequence-coercion")
        .unwrap();
    let raw = case["raw_input"].as_str().unwrap().as_bytes();
    assert!(StoredAssistantRecord::decode_persisted(AssistantKind::Message, raw).is_err());
    assert_eq!(
        StoredAssistantRecord::decode_fork_persisted_direct(AssistantKind::Message, raw)
            .unwrap()
            .payload(),
        &case["expected"]["payload"]
    );

    let mut input = case["input"].clone();
    input["content"] = "\u{1c}\u{1f}".into();
    input["role"] = "user".into();
    assert!(matches!(
        hydrate(
            Model::ChatMessage,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap()
        ),
        Err(RecordError::ModelValidation(_))
    ));
    input["role"] = "assistant".into();
    input["metadata"] = json!({"opaque":"x".repeat(MAX_RECORD_BYTES)});
    assert!(matches!(
        hydrate(
            Model::ChatMessage,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap()
        ),
        Err(RecordError::TooLarge)
    ));

    input = case["input"].clone();
    input["citations"] = json!(vec![
        json!({"source_id":"s","name":"n","chunk_id":"c","excerpt":"e"});
        10_001
    ]);
    assert!(matches!(
        hydrate(
            Model::ChatMessage,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap()
        ),
        Err(RecordError::TooLarge)
    ));
    input = case["input"].clone();
    input.as_object_mut().unwrap().remove("engagement_id");
    input["z_secret"] = "do-not-print-fork-secret".into();
    let Err(RecordError::ModelValidation(report)) = hydrate(
        Model::ChatMessage,
        InputOrigin::RetainedJson,
        &serde_json::to_vec(&input).unwrap(),
    ) else {
        panic!("expected report");
    };
    assert!(!format!("{report:?} {report}").contains("do-not-print"));
    assert_eq!(report.issues().next().unwrap().input, &input);
    let encoded = report.to_json_bytes(MAX_RECORD_BYTES).unwrap();
    assert!(matches!(
        report.to_json_bytes(encoded.len() - 1),
        Err(RecordError::TooLarge)
    ));

    let constructor = json!({"id":"message","engagement_id":"project","session_id":"session","sequence":1,"role":"assistant"});
    let defaults = CreatedEntityDefaults {
        id: None,
        created_at: time("2030-01-01T12:00:00Z"),
        updated_at: time("2030-01-01T12:00:00Z"),
        last_activity_at: None,
    };
    let typed = vec![TypedModelPath {
        path: vec![Location::Field("metadata".into())],
        model: Model::ChatSession,
    }];
    assert!(matches!(
        hydrate_fork_created(
            Model::ChatMessage,
            &serde_json::to_vec(&constructor).unwrap(),
            &defaults,
            &typed
        ),
        Err(RecordError::Invariant(_))
    ));
    let many = vec![
        TypedModelPath {
            path: vec![Location::Field("usage".into())],
            model: Model::ChatTokenUsage
        };
        10_001
    ];
    assert!(matches!(
        hydrate_fork_created(
            Model::ChatMessage,
            &serde_json::to_vec(&constructor).unwrap(),
            &defaults,
            &many
        ),
        Err(RecordError::TooLarge)
    ));
}
