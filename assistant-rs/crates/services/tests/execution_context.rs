use nebula_assistant_domain::records::{MAX_RECORD_BYTES, RecordError};
use nebula_assistant_services::execution_context::{self as context, Error};
use serde_json::{Value, json};
use std::collections::BTreeSet;

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-execution-context.json"
    ))
    .unwrap()
}
fn error(error: Error) -> Value {
    match error {
        Error::HistoryConflict(detail) => json!({"kind":"ChatHistoryConflict","detail":detail}),
        Error::Capacity(detail) => json!({"kind":"ContextCapacityError","detail":detail}),
        Error::Source { kind, detail } => json!({"kind":kind,"detail":detail}),
        Error::ContextValidation(errors) => json!({"kind":"ValidationError","errors":errors}),
        Error::Validation(RecordError::ModelValidation(report)) => {
            json!({"kind":"ValidationError","errors":serde_json::from_slice::<Value>(&report.to_json_bytes(MAX_RECORD_BYTES).unwrap()).unwrap()})
        }
        other => panic!("unexpected oracle error {other:?}"),
    }
}
fn compare(case: &Value, result: context::Result<Value>) {
    let actual = match result {
        Ok(value) => json!({"accepted":true,"value":value}),
        Err(e) => json!({"accepted":false,"error":error(e)}),
    };
    assert_eq!(actual, case["expected"], "{}", case["name"]);
}
#[test]
fn python_context_limits_and_catalog_match_source() {
    let fixture = fixture();
    let cases = fixture["limit_vectors"].as_array().unwrap();
    assert_eq!(cases.len(), 48);
    for case in cases {
        let required: BTreeSet<_> = case["required_parameters"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect();
        compare(
            case,
            context::resolve_context_limits(
                &case["profile"],
                case["model"].as_str(),
                case["requested_output_tokens"].as_number(),
                &required,
            )
            .map(|v| serde_json::to_value(v).unwrap()),
        );
    }
    let known = fixture["known_model_vectors"].as_array().unwrap();
    assert_eq!(known.len(), 12);
    for case in known {
        assert_eq!(
            serde_json::to_value(context::known_model_limits(case["model"].as_str()).unwrap())
                .unwrap(),
            case["expected"],
            "{}",
            case["model"]
        );
    }
    let data: Value =
        serde_json::from_str(include_str!("../src/execution_context/static_data.json")).unwrap();
    assert_eq!(
        data, fixture["static_data"],
        "production lookup data must match captured Python catalog/digit rules"
    );
}
#[test]
fn python_text_estimates_match_source() {
    let fixture = fixture();
    let cases = fixture["estimate_vectors"].as_array().unwrap();
    assert_eq!(cases.len(), 9);
    for case in cases {
        let result = match case["operation"].as_str().unwrap() {
            "tokens" => context::estimate_text_tokens(
                case["text"].as_str().unwrap(),
                case["message_count"].as_u64().unwrap() as usize,
            ),
            "messages" => context::estimate_text_messages(
                case["messages"].as_array().unwrap(),
                case["instructions"].as_str().unwrap(),
            ),
            "request" => context::estimate_text_request(&case["request"]),
            other => panic!("unknown estimate {other}"),
        }
        .unwrap();
        assert_eq!(json!(result), case["expected"], "{}", case["name"]);
    }
    // Reuse the full HTTP oracle's actual dispatched request, rather than a
    // hand-reconstructed approximation of its no-tool prefix and metadata.
    let execution: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-execution.json")).unwrap();
    let case = execution["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "existing-stream")
        .unwrap();
    let request = &case["expected_provider_requests"][0]["request"];
    assert_eq!(
        context::estimate_text_request(request).unwrap(),
        context::estimate_text_messages(
            request["messages"].as_array().unwrap(),
            request["instructions"].as_str().unwrap()
        )
        .unwrap()
    );
}
#[test]
fn python_visible_history_merge_and_joins_match_source() {
    let fixture = fixture();
    let cases = fixture["history_vectors"].as_array().unwrap();
    assert_eq!(cases.len(), 27);
    for case in cases {
        let original = case.clone();
        let stored = case["stored"].as_array().unwrap();
        let incoming = case["incoming"].as_array().unwrap();
        let visible = context::visible_history(stored).unwrap();
        assert_eq!(
            json!(visible.iter().map(|m| m["id"].clone()).collect::<Vec<_>>()),
            case["visible_ids"],
            "{}",
            case["name"]
        );
        let result=context::merge_text_history(stored,incoming).and_then(|merged|{
            let invalid_expected = case["name"].as_str().unwrap().starts_with("context-invalid") || case["name"] == "context-partially-invalid";
            assert_eq!(!merged.invalid_context_message_ids.is_empty(), invalid_expected);
            let models=context::text_model_messages(&merged.history)?;
            Ok(json!({"history":merged.history,"new_messages":merged.new_messages,"model_messages":models}))
        });
        compare(case, result);
        assert_eq!(*case, original, "pure preparation cannot mutate its inputs");
    }
    for case in fixture["join_vectors"].as_array().unwrap() {
        assert_eq!(
            json!(
                context::join_text_assistant_messages(case["messages"].as_array().unwrap())
                    .unwrap()
            ),
            case["expected"],
            "{}",
            case["name"]
        );
    }
    let invalid = cases
        .iter()
        .find(|c| c["name"] == "context-invalid-hash")
        .unwrap();
    assert_eq!(
        context::stored_model_text(&invalid["stored"][0]).unwrap(),
        ("Question".into(), true)
    );
}
#[test]
fn text_preparation_bounds_and_unsupported_projections_are_explicit() {
    assert!(matches!(
        context::estimate_text_tokens(&"x".repeat(context::MAX_PREPARATION_BYTES + 1), 0),
        Err(Error::Boundary(_))
    ));
    assert!(matches!(
        context::visible_history(&vec![Value::Null; context::MAX_HISTORY_MESSAGES + 1]),
        Err(Error::Boundary(_))
    ));
    assert!(matches!(
        context::join_text_assistant_messages(&[
            json!({"role":"user","content":[{"type":"image"}]})
        ]),
        Err(Error::Unsupported(_))
    ));
    assert!(matches!(
        context::estimate_text_request(&json!({"messages":[],"tools":[{"name":"tool"}]})),
        Err(Error::Unsupported(_))
    ));
    let fixture = fixture();
    let case = &fixture["history_vectors"][0];
    let mut incoming = case["incoming"].as_array().unwrap().clone();
    incoming[0]["content_blocks"] = json!([{"type":"image","artifact_id":"opaque"}]);
    assert!(matches!(
        context::merge_text_history(&[], &incoming),
        Err(Error::Unsupported(_))
    ));
    // Multiple individually small histories must not bypass aggregate scratch
    // bounds. Refusal does not alter either borrowed input.
    let mut many = fixture["history_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "single-new")
        .unwrap()["stored"]
        .as_array()
        .unwrap()
        .clone();
    many[0]["content"] = "sensitive-marker".repeat(6000).into();
    let messages = vec![many[0].clone(); 100];
    let original = messages.clone();
    assert!(matches!(
        context::merge_text_history(&messages, &[]),
        Err(Error::Boundary(_))
    ));
    assert_eq!(messages, original);
    assert!(!format!("{:?}", Error::Capacity("secret-canary".into())).contains("secret-canary"));
}
