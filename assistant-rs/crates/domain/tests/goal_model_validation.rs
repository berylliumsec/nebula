use chrono::{DateTime, Utc};
use nebula_assistant_domain::{
    model_validation::{InputOrigin, Location, Model, ValidationReport, hydrate},
    records::{AssistantKind as Kind, MAX_RECORD_BYTES, RecordError, StoredAssistantRecord},
};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-goal-drafts.json"
    ))
    .unwrap()
}
fn base() -> Value {
    fixture()["vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["name"] == "goal-defaults")
        .unwrap()["input"]
        .clone()
}
fn stamp(value: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value)
        .unwrap()
        .with_timezone(&Utc)
}
fn validation_report(value: &Value) -> ValidationReport {
    match hydrate(
        Model::ChatGoal,
        InputOrigin::RetainedJson,
        &serde_json::to_vec(value).unwrap(),
    ) {
        Err(RecordError::ModelValidation(report)) => report,
        other => panic!("expected structured error, got {other:?}"),
    }
}

fn assert_model_payload(model: Model, actual: &Value, expected: &Value, case: &Value) {
    if model != Model::ChatGoal {
        assert_eq!(actual, expected, "{case}");
        return;
    }
    let actual = actual.as_object().expect("hydrated Goal object");
    let expected = expected.as_object().expect("captured Goal object");
    assert_eq!(
        actual.keys().collect::<Vec<_>>(),
        expected.keys().collect::<Vec<_>>(),
        "{case}"
    );
    for (field, expected) in expected {
        let actual = &actual[field];
        if field == "elapsed_seconds" {
            // Python and serde_json may spell the same finite f64 as 1e-07 and
            // 1e-7. Compare this declared float's exact bits (including -0.0),
            // while retaining token-exact equality for every opaque number.
            let actual = actual.as_f64().expect("finite Goal elapsed number");
            let expected = expected.as_f64().expect("captured Goal elapsed number");
            assert!(actual.is_finite() && expected.is_finite(), "{case}");
            assert_eq!(actual.to_bits(), expected.to_bits(), "{case}: {field}");
        } else {
            assert_eq!(actual, expected, "{case}: {field}");
        }
    }
}

#[test]
fn retained_goal_and_usage_hydration_match_python_model_vectors() {
    let fixture = fixture();
    let vectors = fixture["vectors"].as_array().unwrap();
    assert!(vectors.len() >= 155);
    for case in vectors {
        let model = match case["model"].as_str().unwrap() {
            "ChatGoal" => Model::ChatGoal,
            "ChatTokenUsage" => Model::ChatTokenUsage,
            other => panic!("unexpected {other}"),
        };
        let origin = match case["input_origin"].as_str().unwrap() {
            "retained_json" => InputOrigin::RetainedJson,
            "writer_model_dump" => InputOrigin::WriterModelDump,
            other => panic!("unexpected {other}"),
        };
        let raw = case["raw_input"].as_str().unwrap();
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
        let actual = hydrate(model, origin, raw.as_bytes());
        if case["expected"]["nonfinite_paths"].is_array() {
            assert!(
                matches!(actual, Err(RecordError::Invariant(_))),
                "nonfinite retained JSON remains an explicit boundary, not a coerced null"
            );
        } else if case["expected"]["accepted"] == true {
            let actual = actual.unwrap_or_else(|error| panic!("{}: {error:?}", case["name"]));
            assert_model_payload(model, &actual, &case["expected"]["payload"], &case["name"]);
            if model == Model::ChatGoal {
                let actual = match origin {
                    InputOrigin::RetainedJson => {
                        StoredAssistantRecord::decode_persisted_direct(Kind::Goal, raw.as_bytes())
                    }
                    InputOrigin::WriterModelDump => {
                        StoredAssistantRecord::decode_updated_direct(Kind::Goal, raw.as_bytes())
                    }
                }
                .unwrap();
                assert_model_payload(
                    model,
                    actual.payload(),
                    &case["expected"]["payload"],
                    &case["name"],
                );
            }
        } else {
            let Err(RecordError::ModelValidation(actual)) = actual else {
                panic!("{}: {actual:?}", case["name"]);
            };
            assert_eq!(actual.model_name(), model.name());
            let errors: Value =
                serde_json::from_slice(&actual.to_json_bytes(MAX_RECORD_BYTES).unwrap()).unwrap();
            assert_eq!(errors, case["expected"]["errors"], "{}", case["name"]);
            for issue in actual.issues() {
                let source = issue
                    .input_path
                    .iter()
                    .fold(&case["input"], |value, at| match at {
                        Location::Field(key) => &value[key],
                        Location::Index(index) => &value[*index],
                    });
                assert_eq!(issue.input, source, "{}", case["name"]);
            }
        }
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
    }
    for case in fixture["nonfinite_model_observations"]
        .as_array()
        .into_iter()
        .flatten()
    {
        assert!(case["expected"]["nonfinite_paths"].is_array());
        assert!(
            matches!(
                hydrate(
                    Model::ChatGoal,
                    InputOrigin::RetainedJson,
                    case["raw_input"].as_str().unwrap().as_bytes()
                ),
                Err(RecordError::Invariant(_))
            ),
            "nonfinite remains a documented integrity boundary, not supported model parity"
        );
    }
}

#[test]
fn goal_constructor_factories_preserve_original_inputs_and_default_precedence() {
    let constructor = json!({"id":"goal-created","engagement_id":"project","session_id":"session","objective":"  objective  ","completion_criteria":[" done "]});
    let raw = serde_json::to_vec(&constructor).unwrap();
    let created = stamp("2030-01-01T12:00:00Z");
    let updated = stamp("2030-01-01T12:00:01Z");
    let goal =
        StoredAssistantRecord::decode_created_direct(Kind::Goal, &raw, created, updated).unwrap();
    assert_eq!(goal.payload()["objective"], "objective");
    assert_eq!(goal.payload()["completion_criteria"], json!(["done"]));
    assert_eq!(goal.payload()["revision"], 1);
    assert_eq!(goal.payload()["created_at"], "2030-01-01T12:00:00Z");
    assert_eq!(goal.payload()["updated_at"], "2030-01-01T12:00:01Z");
    assert_eq!(
        goal.payload()["usage"],
        json!({"input_tokens":0,"output_tokens":0,"total_tokens":0})
    );
    let Err(RecordError::ModelValidation(report)) =
        StoredAssistantRecord::decode_created_direct(Kind::Goal, &raw, updated, created)
    else {
        panic!("backwards factories must fail model-after validation");
    };
    let issue = report.issues().next().unwrap();
    assert!(issue.loc.is_empty());
    assert_eq!(issue.input, &constructor);
    assert!(issue.input.get("revision").is_none());
    assert!(issue.input.get("created_at").is_none());
    assert!(issue.input.get("updated_at").is_none());
    assert_eq!(
        issue.msg,
        "Value error, updated_at cannot be earlier than created_at"
    );

    let mut explicit = constructor.clone();
    explicit["revision"] = 9.into();
    explicit["created_at"] = "2020-01-01T00:00:00+02:00".into();
    explicit["updated_at"] = "2021-01-01T00:00:00Z".into();
    let goal = StoredAssistantRecord::decode_created_direct(
        Kind::Goal,
        &serde_json::to_vec(&explicit).unwrap(),
        updated,
        created,
    )
    .unwrap();
    assert_eq!(goal.payload()["revision"], 9);
    assert_eq!(goal.payload()["created_at"], "2019-12-31T22:00:00Z");
    assert!(
        matches!(
            StoredAssistantRecord::decode_persisted_direct(Kind::Goal, &raw),
            Err(RecordError::Shape(_))
        ),
        "creation factories never leak into retained reads"
    );
}

#[test]
fn goal_nested_validation_preserves_paths_ownership_order_and_bounds() {
    let mut input = base();
    input["usage"] = json!({"input_tokens":-1,"output_tokens":null});
    input["completion_evidence"] = json!([{}, "DO-NOT-LOG-GOAL", null]);
    let report = validation_report(&input);
    let issues: Vec<_> = report.issues().collect();
    assert_eq!(
        issues[0].loc,
        &[
            Location::Field("usage".into()),
            Location::Field("input_tokens".into())
        ]
    );
    assert_eq!(issues[0].input_path, issues[0].loc);
    assert_eq!(issues[0].input, &json!(-1));
    assert_eq!(
        issues[2].loc,
        &[
            Location::Field("completion_evidence".into()),
            Location::Index(1)
        ]
    );
    assert!(!format!("{report:?} {report}").contains("DO-NOT-LOG"));
    let raw = serde_json::to_string(&base()).unwrap();
    let raw = format!(
        "{},\"usage\":{{\"z\":1,\"a\":2}},\"metadata\":{{\" z \":1,\"z\":2}}}}",
        &raw[..raw.len() - 1]
    );
    let Err(RecordError::ModelValidation(report)) =
        hydrate(Model::ChatGoal, InputOrigin::RetainedJson, raw.as_bytes())
    else {
        panic!("usage extras expected");
    };
    assert_eq!(
        report
            .input_order_at(&[Location::Field("usage".into())])
            .unwrap(),
        &["z", "a"]
    );
    assert_eq!(
        report.issues().next().unwrap().loc,
        &[Location::Field("usage".into()), Location::Field("z".into())]
    );

    let raw = serde_json::to_string(&base()).unwrap();
    let raw = format!(
        "{},\"z_extra\":{{\"z\":[{{\"q\":1,\"b\":2}}],\"a\":3}},\"usage\":{{\"extra\":[{{\"z\":1,\"a\":2}}]}}}}",
        &raw[..raw.len() - 1]
    );
    let Err(RecordError::ModelValidation(report)) =
        hydrate(Model::ChatGoal, InputOrigin::RetainedJson, raw.as_bytes())
    else {
        panic!("unknown Goal and nested usage fields must retain their input order");
    };
    assert_eq!(
        report.input_order_at(&[Location::Field("z_extra".into())]),
        Some(["z".into(), "a".into()].as_slice())
    );
    assert_eq!(
        report.input_order_at(&[
            Location::Field("z_extra".into()),
            Location::Field("z".into()),
            Location::Index(0)
        ]),
        Some(["q".into(), "b".into()].as_slice())
    );
    assert_eq!(
        report.input_order_at(&[
            Location::Field("usage".into()),
            Location::Field("extra".into()),
            Location::Index(0)
        ]),
        Some(["z".into(), "a".into()].as_slice())
    );
    let Err(RecordError::ModelValidation(report)) = hydrate(
        Model::ChatTokenUsage,
        InputOrigin::RetainedJson,
        br#"{"extra":[{"z":{"q":1,"b":2},"a":3}]}"#,
    ) else {
        panic!("standalone usage extras require the same ordered input metadata");
    };
    assert_eq!(
        report.input_order_at(&[Location::Field("extra".into()), Location::Index(0)]),
        Some(["z".into(), "a".into()].as_slice())
    );

    let raw = serde_json::to_string(&base()).unwrap();
    let mut raw = raw[..raw.len() - 1].to_owned();
    for _ in 0..4_000 {
        raw.push_str(r#", "extra":{"z":{"discarded":1},"a":2}"#);
    }
    raw.push_str(r#", "extra":{"a":2,"z":[{"q":3,"b":4}]}}"#);
    let Err(RecordError::ModelValidation(report)) =
        hydrate(Model::ChatGoal, InputOrigin::RetainedJson, raw.as_bytes())
    else {
        panic!("overwritten duplicate subtrees must release their metadata budget");
    };
    assert_eq!(report.len(), 1);
    assert_eq!(
        report.input_order_at(&[Location::Field("extra".into())]),
        Some(["a".into(), "z".into()].as_slice())
    );
    assert!(
        report
            .input_order_at(&[Location::Field("extra".into()), Location::Field("z".into())])
            .is_none()
    );
    assert_eq!(
        report.input_order_at(&[
            Location::Field("extra".into()),
            Location::Field("z".into()),
            Location::Index(0)
        ]),
        Some(["q".into(), "b".into()].as_slice())
    );

    let mut input = base();
    input["extra"] = json!(vec![json!({}); 10_001]);
    assert!(matches!(
        hydrate(
            Model::ChatGoal,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap()
        ),
        Err(RecordError::TooLarge)
    ));
    input["extra"] = json!({"x".repeat(1024*1024):vec![json!({});16]});
    assert!(matches!(
        hydrate(
            Model::ChatGoal,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap()
        ),
        Err(RecordError::TooLarge)
    ));

    let mut input = base();
    input["metadata"] = json!({"history":"x".repeat(6*1024*1024)});
    for name in ["engagement_id", "session_id", "objective"] {
        input.as_object_mut().unwrap().remove(name);
    }
    assert!(matches!(
        hydrate(
            Model::ChatGoal,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap()
        ),
        Err(RecordError::TooLarge)
    ));
    let mut input = base();
    input["linked_turn_ids"] = json!(vec![Value::Null; 10_000]);
    let report = validation_report(&input);
    assert_eq!(report.len(), 10_000);
    input["usage"] = json!({"total_tokens":-1});
    assert!(matches!(
        hydrate(
            Model::ChatGoal,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap()
        ),
        Err(RecordError::TooLarge)
    ));
}

#[test]
fn wrapped_goal_hydration_accepts_coercions_and_sanitizes_model_errors() {
    let mut input = base();
    input["current_step"] = "3".into();
    input["usage"] = json!({"total_tokens":" 20 "});
    input["active_since"] = "2030-01-01T14:00:00+02:00".into();
    let raw = serde_json::to_vec(&input).unwrap();
    let wrapped = StoredAssistantRecord::decode_persisted(Kind::Goal, &raw).unwrap();
    let direct = StoredAssistantRecord::decode_persisted_direct(Kind::Goal, &raw).unwrap();
    assert_eq!(wrapped, direct);
    assert_eq!(wrapped.payload()["current_step"], 3);
    assert_eq!(
        wrapped.payload()["active_since"],
        "2030-01-01T14:00:00+02:00"
    );
    let canonical = serde_json::to_vec(wrapped.payload()).unwrap();
    assert_eq!(
        StoredAssistantRecord::decode(Kind::Goal, &canonical).unwrap(),
        wrapped
    );
    input["objective"] = Value::Null;
    input["metadata"] = json!({"secret":"DO-NOT-LOG-GOAL"});
    let raw = serde_json::to_vec(&input).unwrap();
    let error = StoredAssistantRecord::decode_persisted(Kind::Goal, &raw).unwrap_err();
    assert!(matches!(error, RecordError::Shape("chat_goals")));
    assert!(!format!("{error:?} {error}").contains("DO-NOT-LOG"));
    assert!(matches!(
        StoredAssistantRecord::decode_persisted_direct(Kind::Goal, &raw),
        Err(RecordError::ModelValidation(_))
    ));
    for field in ["id", "revision", "created_at", "updated_at"] {
        let mut missing = base();
        missing.as_object_mut().unwrap().remove(field);
        assert!(matches!(
            StoredAssistantRecord::decode_persisted(
                Kind::Goal,
                &serde_json::to_vec(&missing).unwrap()
            ),
            Err(RecordError::Shape(_))
        ));
    }
}
