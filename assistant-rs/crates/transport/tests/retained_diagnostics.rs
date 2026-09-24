#[path = "../src/errors/retained.rs"]
mod retained;

use nebula_assistant_domain::{
    model_validation::{InputOrigin, Model, hydrate},
    records::RecordError,
};
use serde_json::{Value, json};

#[test]
fn retained_exception_previews_match_python_and_bound_unicode_tail() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../compatibility/python-model-validation.json"
    ))
    .unwrap();
    assert_eq!(
        fixture["pydantic_version"], "2.13.4",
        "preview URL version is part of the captured contract"
    );
    // No supported vector is silently skipped. Factory observations are a
    // separate documented strict-retained boundary, not renderer inputs.
    let exclusions: &[(&str, &str)] = &[];
    let mut matched = 0;
    let mut accepted = 0;
    let mut excluded = Vec::new();
    for vector in fixture["vectors"].as_array().unwrap() {
        let name = vector["name"].as_str().unwrap();
        if let Some((_, reason)) = exclusions.iter().find(|(excluded, _)| *excluded == name) {
            excluded.push((name, *reason));
            continue;
        }
        let model = match vector["model"].as_str().unwrap() {
            "Entity" => Model::Entity,
            "ChatSchedule" => Model::ChatSchedule,
            other => panic!("unreviewed model {other}"),
        };
        let origin = match vector["input_origin"].as_str().unwrap() {
            "retained_json" => InputOrigin::RetainedJson,
            "writer_model_dump" => InputOrigin::WriterModelDump,
            other => panic!("unreviewed input origin {other}"),
        };
        match hydrate(
            model,
            origin,
            vector["raw_input"].as_str().unwrap().as_bytes(),
        ) {
            Ok(_) => {
                assert_eq!(vector["expected"]["accepted"], true, "{name}");
                accepted += 1;
            }
            Err(RecordError::ModelValidation(report)) => {
                assert_eq!(vector["expected"]["accepted"], false, "{name}");
                let preview = retained::exception_prefix(&report);
                assert!(preview.chars().count() <= 300, "{name}");
                assert_eq!(
                    preview,
                    vector["expected"]["exception_preview"].as_str().unwrap(),
                    "{name}"
                );
                matched += 1;
            }
            Err(error) => panic!("unexpected retained error for {name}: {error:?}"),
        }
    }
    assert_eq!(excluded, exclusions);
    assert_eq!(matched, 98);
    assert_eq!(accepted, 47);
    assert_eq!(
        fixture["factory_default_vectors"].as_array().unwrap().len(),
        8
    );

    let mut input = fixture["vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["name"] == "ChatSchedule-canonical")
        .unwrap()["input"]
        .clone();
    // A multi-megabyte string is streamed into a fixed-size head/tail sink.
    // The marker in its middle must not affect classification; its tail must.
    input["paused_by"] = format!(
        "{}MIDDLE-OMITTED{} rate limit",
        "😀".repeat(300_000),
        "🦀".repeat(300_000)
    )
    .into();
    let RecordError::ModelValidation(report) = hydrate(
        Model::ChatSchedule,
        InputOrigin::RetainedJson,
        &serde_json::to_vec(&input).unwrap(),
    )
    .unwrap_err() else {
        panic!("expected literal issue")
    };
    let preview = retained::exception_prefix(&report);
    assert!(preview.contains("rate limit"));
    assert!(!preview.contains("MIDDLE-OMITTED"));
    assert!(preview.chars().count() <= 300);
    // Whole-string truncation happens after repr quoting and escaping. Quotes,
    // control characters and scalar boundaries must remain valid UTF-8.
    input["paused_by"] = json!("'\u{00a0}\u{0001}\"\n");
    let RecordError::ModelValidation(report) = hydrate(
        Model::ChatSchedule,
        InputOrigin::RetainedJson,
        &serde_json::to_vec(&input).unwrap(),
    )
    .unwrap_err() else {
        panic!("expected literal issue")
    };
    let preview = retained::exception_prefix(&report);
    assert!(preview.contains("\\xa0\\x01"));
    assert!(preview.contains("\\n"));
}

#[test]
fn goal_exception_previews_preserve_nested_input_locations_and_order() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../compatibility/python-goal-drafts.json"
    ))
    .unwrap();
    let mut matched = 0;
    for vector in fixture["vectors"].as_array().unwrap() {
        if vector["expected"]["accepted"] == true {
            continue;
        }
        let model = match vector["model"].as_str().unwrap() {
            "ChatGoal" => Model::ChatGoal,
            "ChatTokenUsage" => Model::ChatTokenUsage,
            other => panic!("unreviewed model {other}"),
        };
        let origin = match vector["input_origin"].as_str().unwrap() {
            "retained_json" => InputOrigin::RetainedJson,
            "writer_model_dump" => InputOrigin::WriterModelDump,
            other => panic!("unreviewed input origin {other}"),
        };
        let Err(RecordError::ModelValidation(report)) = hydrate(
            model,
            origin,
            vector["raw_input"].as_str().unwrap().as_bytes(),
        ) else {
            panic!("expected model validation for {}", vector["name"]);
        };
        assert_eq!(
            retained::exception_prefix(&report),
            vector["expected"]["exception_preview"].as_str().unwrap(),
            "{}",
            vector["name"]
        );
        matched += 1;
    }
    assert!(matched > 50, "complete rejected Goal/usage corpus required");
}

#[test]
fn fork_exception_previews_preserve_direct_and_typed_constructor_inputs() {
    use chrono::{DateTime, Utc};
    use nebula_assistant_domain::{
        dependencies::DependencyEnvironment,
        model_validation::{
            CreatedEntityDefaults, Location, TypedModelPath, hydrate_fork_created,
            hydrate_harness_session,
        },
    };
    use std::collections::VecDeque;

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
            other => panic!("unreviewed model {other}"),
        }
    }
    fn time(value: &Value) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(value.as_str().unwrap())
            .unwrap()
            .with_timezone(&Utc)
    }
    struct Clock(VecDeque<DateTime<Utc>>);
    impl DependencyEnvironment for Clock {
        fn now(&mut self) -> DateTime<Utc> {
            self.0.pop_front().expect("uncaptured factory")
        }
        fn expand_user(&mut self, _: &str) -> Result<String, RecordError> {
            panic!("fork diagnostic hydration cannot resolve host accounts")
        }
    }
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-forks.json")).unwrap();
    let fallback = time(&fixture["clock"]);
    let mut previews = 0;
    let mut typed_previews = 0;
    for case in fixture["model_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fixture["constructor_vectors"].as_array().unwrap())
    {
        let model = model(case["model"].as_str().unwrap());
        let raw = case["raw_input"].as_str().unwrap().as_bytes();
        let mut clock = Clock(
            case["expected_factory_trace"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|event| event["kind"] == "model")
                .map(|event| time(&event["value"]))
                .collect(),
        );
        let result = if case["input_origin"] == "constructor" {
            let input = &case["input"];
            let defaults = CreatedEntityDefaults {
                id: input
                    .get("id")
                    .is_none()
                    .then(|| case["generated_ids"][0].as_str().unwrap().to_owned()),
                created_at: if input.get("created_at").is_none() {
                    clock.now()
                } else {
                    fallback
                },
                updated_at: if input.get("updated_at").is_none() {
                    clock.now()
                } else {
                    fallback
                },
                last_activity_at: (model == Model::HarnessSession
                    && input.get("last_activity_at").is_none())
                .then(|| clock.now()),
            };
            let typed: Vec<_> = case["typed_paths"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|entry| TypedModelPath {
                    model: match entry["model"].as_str().unwrap() {
                        "ChatContentBlock" => Model::ChatContentBlock,
                        "ChatCitation" => Model::ChatCitation,
                        "ChatTokenUsage" => Model::ChatTokenUsage,
                        other => panic!("unreviewed typed model {other}"),
                    },
                    path: entry["path"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|part| match part {
                            Value::String(value) => Location::Field(value.clone()),
                            Value::Number(value) => {
                                Location::Index(value.as_u64().unwrap().try_into().unwrap())
                            }
                            _ => panic!("invalid typed path"),
                        })
                        .collect(),
                })
                .collect();
            hydrate_fork_created(model, raw, &defaults, &typed)
        } else if model == Model::HarnessSession {
            hydrate_harness_session(raw, &mut clock)
        } else {
            hydrate(model, InputOrigin::RetainedJson, raw)
        };
        assert!(clock.0.is_empty(), "missed factory: {}", case["name"]);
        if case["expected"]["accepted"] == true {
            assert!(result.is_ok(), "{}: {result:?}", case["name"]);
            continue;
        }
        let Err(RecordError::ModelValidation(report)) = result else {
            panic!("expected detailed error: {}: {result:?}", case["name"]);
        };
        let preview = retained::exception_prefix(&report);
        assert!(preview.chars().count() <= 300);
        assert_eq!(
            preview, case["expected"]["exception_preview"],
            "{}",
            case["name"]
        );
        previews += 1;
        if case.get("typed_paths").is_some() {
            typed_previews += 1;
            assert!(preview.contains("total_tokens=3)"));
        }
    }
    assert!(previews >= 40);
    assert!(typed_previews >= 1);
}

#[test]
fn completion_exception_previews_preserve_python_order_and_value_errors() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-execution.json")).unwrap();
    for case in fixture["request_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["expected"]["accepted"] == false)
    {
        let Err(RecordError::ModelValidation(report)) = hydrate(
            Model::ChatCompletionRequest,
            InputOrigin::RetainedJson,
            case["raw_input"].as_str().unwrap().as_bytes(),
        ) else {
            panic!("expected report");
        };
        assert_eq!(
            retained::request_exception_prefix(&report),
            case["expected"]["request_exception_preview"]
                .as_str()
                .unwrap(),
            "{}",
            case["name"]
        );
        assert_eq!(
            retained::exception_prefix(&report),
            case["expected"]["exception_preview"].as_str().unwrap(),
            "{}",
            case["name"]
        );
    }
}
