use chrono::{DateTime, SecondsFormat, Utc};
use nebula_assistant_domain::{
    model_validation::{
        CreatedEntityDefaults, InputOrigin, Location, Model, TypedModelPath, hydrate,
        hydrate_created_turn,
    },
    records::{MAX_RECORD_BYTES, RecordError, StoredAssistantRecord},
};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-turn-validation.json"
    ))
    .unwrap()
}

fn stamp(value: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value)
        .unwrap()
        .with_timezone(&Utc)
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
            panic!("{} expected model report, got {result:?}", case["name"]);
        };
        assert_eq!(report.model_name(), "ChatTurn");
        assert_eq!(
            serde_json::from_slice::<Value>(&report.to_json_bytes(MAX_RECORD_BYTES).unwrap())
                .unwrap(),
            case["expected"]["errors"],
            "{}",
            case["name"]
        );
        for issue in report.issues() {
            let expected = issue
                .input_path
                .iter()
                .fold(&case["input"], |value, at| match at {
                    Location::Field(key) => &value[key],
                    Location::Index(index) => &value[*index],
                });
            assert_eq!(issue.input, expected, "{}", case["name"]);
        }
    }
}

#[test]
fn turn_retained_and_writer_hydration_match_python_vectors() {
    let fixture = fixture();
    let cases = fixture["vectors"].as_array().unwrap();
    assert_eq!(cases.len(), 113);
    for case in cases {
        let raw = case["raw_input"].as_str().unwrap();
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
        let origin = match case["input_origin"].as_str().unwrap() {
            "retained_json" => InputOrigin::RetainedJson,
            "writer_model_dump" => InputOrigin::WriterModelDump,
            other => panic!("unexpected origin {other}"),
        };
        let result = hydrate(Model::ChatTurn, origin, raw.as_bytes());
        if case["expected"]["accepted"] == true {
            assert_eq!(
                StoredAssistantRecord::decode_execution_turn_direct(raw.as_bytes(), origin)
                    .unwrap()
                    .payload(),
                &case["expected"]["payload"],
                "{}",
                case["name"]
            );
        }
        if let Err(RecordError::ModelValidation(report)) = &result {
            for field in case["datetime_fields"].as_array().unwrap() {
                assert!(report.is_datetime_input(field.as_str().unwrap()));
            }
        }
        assert_case(case, result);
        assert!(
            case["expected_factory_trace"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
    }
}

#[test]
fn turn_constructor_defaults_and_typed_usage_match_python() {
    let fixture = fixture();
    let cases = fixture["constructor_vectors"].as_array().unwrap();
    assert_eq!(cases.len(), 10);
    for case in cases {
        let input = &case["input"];
        let mut trace = Vec::new();
        let id = input.get("id").is_none().then(|| {
            let id = case["generated_ids"][0].as_str().unwrap().to_owned();
            trace.push(json!({"kind":"uuid","value":id}));
            id
        });
        let mut clocks = case["model_clock_values"].as_array().unwrap().iter();
        let mut clock = |field: &str| {
            if input.get(field).is_some() {
                return stamp("2030-01-01T12:00:00Z");
            }
            let value = stamp(clocks.next().unwrap().as_str().unwrap());
            trace.push(json!({"kind":"model","value":value.to_rfc3339_opts(if value.timestamp_subsec_micros()==0 { SecondsFormat::Secs } else { SecondsFormat::Micros },false)}));
            value
        };
        let defaults = CreatedEntityDefaults {
            id,
            created_at: clock("created_at"),
            updated_at: clock("updated_at"),
            last_activity_at: None,
        };
        let typed: Vec<_> = case["typed_paths"]
            .as_array()
            .unwrap()
            .iter()
            .map(|entry| {
                assert_eq!(entry["path"], json!(["usage"]));
                assert_eq!(entry["model"], "ChatTokenUsage");
                TypedModelPath {
                    path: vec![Location::Field("usage".into())],
                    model: Model::ChatTokenUsage,
                }
            })
            .collect();
        let result = hydrate_created_turn(
            case["raw_input"].as_str().unwrap().as_bytes(),
            &defaults,
            &typed,
        );
        if case["expected"]["accepted"] == true {
            assert_eq!(
                StoredAssistantRecord::decode_execution_turn_created(
                    case["raw_input"].as_str().unwrap().as_bytes(),
                    &defaults,
                    &typed
                )
                .unwrap()
                .payload(),
                &case["expected"]["payload"],
                "{}",
                case["name"]
            );
        }
        if let Err(RecordError::ModelValidation(report)) = &result
            && !typed.is_empty()
        {
            assert_eq!(
                report.model_input_at(&[Location::Field("usage".into())]),
                Some(Model::ChatTokenUsage)
            );
        }
        assert_case(case, result);
        assert_eq!(trace, *case["expected_factory_trace"].as_array().unwrap());
    }
}

#[test]
fn turn_missing_retained_identity_never_runs_factories() {
    let fixture = fixture();
    let cases = fixture["strict_retained_boundary_vectors"]
        .as_array()
        .unwrap();
    assert_eq!(cases.len(), 4);
    for case in cases {
        // Python runs missing-field factories even for retained input. When
        // created_at is missing, the generated future date then fails its
        // chronology check. Our strict retained boundary rejects before any
        // factory, independently of that downstream Python outcome.
        assert_eq!(
            case["expected"]["accepted"],
            case["strict_retained_boundary"] != "created_at"
        );
        assert!(matches!(
            hydrate(
                Model::ChatTurn,
                InputOrigin::RetainedJson,
                case["raw_input"].as_str().unwrap().as_bytes()
            ),
            Err(RecordError::Shape("chat_turns"))
        ));
    }
}

#[test]
fn turn_validation_preserves_order_bigints_and_redacts_bounded_reports() {
    let fixture = fixture();
    let base = fixture["vectors"][0]["input"].clone();
    let raw = serde_json::to_string(&base).unwrap();
    let raw = format!(
        "{},\"usage\":{{\"z_extra\":\"DO-NOT-LOG-TURN\",\"a_extra\":1}},\"request_snapshot\":{{\" z \":1,\"z\":2,\"inner\":{{\" a \":3}}}}}}",
        &raw[..raw.len() - 1]
    );
    let Err(RecordError::ModelValidation(report)) =
        hydrate(Model::ChatTurn, InputOrigin::RetainedJson, raw.as_bytes())
    else {
        panic!("usage extras must produce a report");
    };
    assert_eq!(
        report
            .input_order_at(&[Location::Field("usage".into())])
            .unwrap(),
        &["z_extra", "a_extra"]
    );
    assert_eq!(
        report.issues().next().unwrap().loc,
        &[
            Location::Field("usage".into()),
            Location::Field("z_extra".into())
        ]
    );
    assert!(!format!("{report:?} {report}").contains("DO-NOT-LOG"));
    assert!(matches!(
        report.to_json_bytes(16),
        Err(RecordError::TooLarge)
    ));
    assert!(matches!(
        hydrate(
            Model::ChatTurn,
            InputOrigin::RetainedJson,
            &vec![b' '; MAX_RECORD_BYTES + 1]
        ),
        Err(RecordError::TooLarge)
    ));
    let mut input = base;
    input["request_snapshot"] = json!({"a":1});
    let raw = serde_json::to_string(&input).unwrap().replace(
        "\"request_snapshot\":{\"a\":1}",
        &format!("\"request_snapshot\":{{\"huge\":{}}}", "9".repeat(400)),
    );
    let hydrated = hydrate(Model::ChatTurn, InputOrigin::RetainedJson, raw.as_bytes()).unwrap();
    assert_eq!(
        hydrated["request_snapshot"]["huge"].to_string(),
        "9".repeat(400)
    );
    let invalid_defaults = CreatedEntityDefaults {
        id: None,
        created_at: stamp("2030-01-01T12:00:00Z"),
        updated_at: stamp("2030-01-01T12:00:00Z"),
        last_activity_at: Some(stamp("2030-01-01T12:00:00Z")),
    };
    assert!(matches!(
        hydrate_created_turn(raw.as_bytes(), &invalid_defaults, &[]),
        Err(RecordError::Invariant(_))
    ));
}
