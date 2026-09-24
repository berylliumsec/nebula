use nebula_assistant_domain::{
    model_validation::{InputOrigin, Model, ValidationReport, hydrate},
    records::{AssistantKind as Kind, MAX_RECORD_BYTES, RecordError, StoredAssistantRecord},
};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-model-validation.json"
    ))
    .unwrap()
}
fn model(value: &Value) -> Model {
    match value.as_str().unwrap() {
        "Entity" => Model::Entity,
        "ChatSchedule" => Model::ChatSchedule,
        other => panic!("unexpected model {other}"),
    }
}
fn canonical() -> Value {
    fixture()["vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == "ChatSchedule-canonical")
        .unwrap()["input"]
        .clone()
}
fn report(input: &Value) -> ValidationReport {
    match hydrate(
        Model::ChatSchedule,
        InputOrigin::RetainedJson,
        &serde_json::to_vec(input).unwrap(),
    ) {
        Err(RecordError::ModelValidation(report)) => report,
        result => panic!("expected model validation, got {result:?}"),
    }
}

#[test]
fn retained_entity_and_schedule_validation_matches_python_vectors() {
    let fixture = fixture();
    let cases = fixture["vectors"].as_array().unwrap();
    assert!(cases.len() >= 145, "do not silently shrink source coverage");
    for case in cases {
        let raw = case["raw_input"].as_str().unwrap();
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
        let origin = match case["input_origin"].as_str().unwrap() {
            "retained_json" => InputOrigin::RetainedJson,
            "writer_model_dump" => InputOrigin::WriterModelDump,
            other => panic!("unexpected origin {other}"),
        };
        let model = model(&case["model"]);
        let result = hydrate(model, origin, raw.as_bytes());
        if case["expected"]["accepted"] == true {
            assert_eq!(
                result.unwrap(),
                case["expected"]["payload"],
                "{}",
                case["name"]
            );
            if model == Model::ChatSchedule {
                let decoded = match origin {
                    InputOrigin::RetainedJson => StoredAssistantRecord::decode_persisted_direct(
                        Kind::Schedule,
                        raw.as_bytes(),
                    ),
                    InputOrigin::WriterModelDump => {
                        StoredAssistantRecord::decode_updated_direct(Kind::Schedule, raw.as_bytes())
                    }
                }
                .unwrap();
                assert_eq!(
                    decoded.payload(),
                    &case["expected"]["payload"],
                    "{}",
                    case["name"]
                );
            }
        } else {
            let Err(RecordError::ModelValidation(report)) = result else {
                panic!("{}: expected detailed report, got {result:?}", case["name"]);
            };
            assert_eq!(report.model_name(), model.name());
            assert_eq!(report.input_origin(), origin);
            for field in ["created_at", "updated_at", "next_run_at", "last_run_at"] {
                let typed = case["datetime_fields"]
                    .as_array()
                    .is_some_and(|fields| fields.iter().any(|value| value == field));
                assert_eq!(
                    report.is_datetime_input(field),
                    typed,
                    "{}: {field}",
                    case["name"]
                );
            }
            assert!(!report.is_empty());
            let encoded = report.to_json_bytes(MAX_RECORD_BYTES).unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&encoded).unwrap(),
                case["expected"]["errors"],
                "{}",
                case["name"]
            );
        }
        assert_eq!(
            serde_json::from_str::<Value>(raw).unwrap(),
            case["input"],
            "input unchanged"
        );
    }
    for case in fixture["factory_default_vectors"].as_array().unwrap() {
        assert_eq!(case["strict_retained_boundary"], "missing_canonical_fields");
        assert!(
            matches!(
                hydrate(
                    model(&case["model"]),
                    InputOrigin::RetainedJson,
                    case["raw_input"].as_str().unwrap().as_bytes()
                ),
                Err(RecordError::Shape(_))
            ),
            "{}",
            case["name"]
        );
    }

    // Direct source observations for ChatSchedule.enabled distinguish exact
    // signed-integer tokens from floats at the same apparent boundary.
    let numeric_boolean_input = canonical();
    for (raw, expected_kind) in [
        ("-9223372036854775808", "bool_parsing"),
        ("9223372036854775807", "bool_parsing"),
        ("-9223372036854775809", "bool_type"),
        ("9223372036854775808", "bool_type"),
        ("-9.223372036854776e18", "bool_type"),
        ("9.223372036854776e18", "bool_type"),
        ("-9.223372036854775e18", "bool_parsing"),
        ("9.223372036854775e18", "bool_parsing"),
        ("-9.223372036854778e18", "bool_type"),
        ("9.223372036854778e18", "bool_type"),
        (
            "100000000000000000000000000000000000000000000000000",
            "bool_type",
        ),
        ("2e20", "bool_type"),
        ("2", "bool_parsing"),
        ("1.5", "bool_type"),
    ] {
        let value: Value = serde_json::from_str(raw).unwrap();
        let mut input = numeric_boolean_input.clone();
        input["enabled"] = value.clone();
        let report = report(&input);
        let message = match expected_kind {
            "bool_parsing" => "Input should be a valid boolean, unable to interpret input",
            _ => "Input should be a valid boolean",
        };
        assert_eq!(
            serde_json::from_slice::<Value>(&report.to_json_bytes(MAX_RECORD_BYTES).unwrap())
                .unwrap(),
            json!([{"type": expected_kind, "loc": ["enabled"], "msg": message, "input": value}]),
            "enabled={raw}"
        );
    }
    for (raw, expected) in [("0", false), ("1", true), ("-0.0", false), ("1e0", true)] {
        let mut input = numeric_boolean_input.clone();
        input["enabled"] = serde_json::from_str(raw).unwrap();
        let hydrated = hydrate(
            Model::ChatSchedule,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap(),
        )
        .unwrap();
        assert_eq!(hydrated["enabled"], expected, "enabled={raw}");
    }
}

#[test]
fn retained_validation_reports_share_original_inputs_and_redact_debug() {
    let mut input = canonical();
    input["model"] = "do-not-print-retained-secret".into();
    input.as_object_mut().unwrap().remove("engagement_id");
    input.as_object_mut().unwrap().remove("session_id");
    let report = report(&input);
    assert_eq!(report.len(), 2);
    let issues: Vec<_> = report.issues().collect();
    assert!(
        std::ptr::eq(issues[0].input, issues[1].input),
        "missing errors share one immutable owner"
    );
    assert_eq!(issues[0].input, &input);
    assert_eq!(report.issues().len(), 2);
    assert!(!format!("{report:?} {report}").contains("do-not-print"));
    let wrapped = RecordError::ModelValidation(report.clone());
    assert!(!format!("{wrapped:?} {wrapped}").contains("do-not-print"));
    let encoded = report.to_json_bytes(MAX_RECORD_BYTES).unwrap();
    assert!(matches!(
        report.to_json_bytes(encoded.len() - 1),
        Err(RecordError::TooLarge)
    ));
    assert_eq!(report.to_json_bytes(encoded.len()).unwrap(), encoded);
    assert_eq!(serde_json::to_vec(&report).unwrap(), encoded);
    assert!(report.retained_bytes() >= serde_json::to_vec(&input).unwrap().len());

    let mut base = serde_json::to_string(&canonical()).unwrap();
    base.pop();
    base.push_str(",\"z_extra\":0,\"a_extra\":1,\"z_extra\":\"last-value-secret\"}");
    let Err(RecordError::ModelValidation(report)) = hydrate(
        Model::ChatSchedule,
        InputOrigin::RetainedJson,
        base.as_bytes(),
    ) else {
        panic!("expected extra errors");
    };
    let errors: Value =
        serde_json::from_slice(&report.to_json_bytes(MAX_RECORD_BYTES).unwrap()).unwrap();
    assert_eq!(errors[0]["loc"], json!(["z_extra"]));
    assert_eq!(errors[0]["input"], "last-value-secret");
    assert_eq!(errors[1]["loc"], json!(["a_extra"]));
}

#[test]
fn retained_validation_reports_bound_retained_input_and_wire_amplification() {
    let mut input = canonical();
    input["model"] = "x".repeat(6 * 1024 * 1024).into();
    for field in ["engagement_id", "session_id", "provider_profile_id"] {
        input.as_object_mut().unwrap().remove(field);
    }
    let bytes = serde_json::to_vec(&input).unwrap();
    assert!(bytes.len() < MAX_RECORD_BYTES);
    assert!(
        matches!(
            hydrate(Model::ChatSchedule, InputOrigin::RetainedJson, &bytes),
            Err(RecordError::TooLarge)
        ),
        "three repeated whole-input wire errors exceed the limit"
    );

    let mut input = canonical();
    input["model"] = "x".repeat(9 * 1024 * 1024).into();
    input["next_run_at"] = "2030-01-01T12:00:00".into();
    let report = report(&input);
    assert!(report.retained_bytes() > 9 * 1024 * 1024);
    assert!(
        report.to_json_bytes(1024).unwrap().len() < 1024,
        "a field issue does not repeat the unrelated large field"
    );
    assert!(matches!(
        hydrate(
            Model::Entity,
            InputOrigin::RetainedJson,
            &vec![b' '; MAX_RECORD_BYTES + 1]
        ),
        Err(RecordError::TooLarge)
    ));

    let mut input = canonical();
    for i in 0..10_001 {
        input[format!("extra{i}")] = Value::Null;
    }
    assert!(matches!(
        hydrate(
            Model::ChatSchedule,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&input).unwrap()
        ),
        Err(RecordError::TooLarge)
    ));
}

#[test]
fn direct_schedule_diagnostics_preserve_wrapped_and_canonical_record_contracts() {
    let mut input = canonical();
    input["next_run_at"] = "2030-01-01T12:00:00".into();
    let bytes = serde_json::to_vec(&input).unwrap();
    assert!(matches!(
        StoredAssistantRecord::decode_persisted(Kind::Schedule, &bytes),
        Err(RecordError::Invariant(_))
    ));
    assert!(matches!(
        StoredAssistantRecord::decode_persisted_direct(Kind::Schedule, &bytes),
        Err(RecordError::ModelValidation(_))
    ));
    let mut input = fixture()["vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == "ChatSchedule-canonical")
        .unwrap()["expected"]["payload"]
        .clone();
    input["next_run_at"] = "2030-01-01T14:00:00+02:00".into();
    let bytes = serde_json::to_vec(&input).unwrap();
    assert_eq!(
        StoredAssistantRecord::decode(Kind::Schedule, &bytes)
            .unwrap()
            .payload(),
        &input,
        "canonical decode remains lossless"
    );
    assert_eq!(
        StoredAssistantRecord::decode_persisted(Kind::Schedule, &bytes).unwrap(),
        StoredAssistantRecord::decode_persisted_direct(Kind::Schedule, &bytes).unwrap()
    );
    for case in fixture()["vectors"].as_array().unwrap() {
        if case["name"] == "ChatSchedule-writer-model-after" {
            let raw = case["raw_input"].as_str().unwrap();
            let Err(RecordError::ModelValidation(report)) =
                StoredAssistantRecord::decode_updated_direct(Kind::Schedule, raw.as_bytes())
            else {
                panic!("expected writer report");
            };
            let issue = report.issues().next().unwrap();
            assert!(issue.loc.is_empty());
            assert!(
                issue.input["created_at"]
                    .as_str()
                    .unwrap()
                    .ends_with("+00:00")
            );
            assert_eq!(
                serde_json::from_slice::<Value>(&report.to_json_bytes(MAX_RECORD_BYTES).unwrap())
                    .unwrap(),
                case["expected"]["errors"]
            );
        }
    }
}
