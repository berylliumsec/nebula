use nebula_assistant_domain::{
    model_validation::{InputOrigin, Model, completion::CompletionRequest, hydrate},
    records::{MAX_RECORD_BYTES, RecordError},
};
use serde_json::{Value, json};

#[test]
fn completion_requests_preserve_python_models_and_ordered_diagnostics() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-execution.json")).unwrap();
    let cases = fixture["request_vectors"].as_array().unwrap();
    assert!(cases.len() >= 35);
    for case in cases {
        let raw = case["raw_input"].as_str().unwrap();
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(), case["input"]);
        let result = hydrate(
            Model::ChatCompletionRequest,
            InputOrigin::RetainedJson,
            raw.as_bytes(),
        );
        if case["expected"]["accepted"] == true {
            assert_eq!(
                result.unwrap_or_else(|e| panic!("{}: {e:?}", case["name"])),
                case["expected"]["payload"],
                "{}",
                case["name"]
            );
            let request = CompletionRequest::parse(raw.as_bytes()).unwrap();
            assert_eq!(
                serde_json::to_value(request).unwrap(),
                case["expected"]["payload"]
            );
        } else {
            let Err(RecordError::ModelValidation(report)) = result else {
                panic!(
                    "{} expected validation report, got {result:?}",
                    case["name"]
                );
            };
            assert_eq!(report.model_name(), "ChatCompletionRequest");
            assert_eq!(
                serde_json::to_value(report).unwrap(),
                case["expected"]["errors"],
                "{}",
                case["name"]
            );
        }
    }
}

#[test]
fn completion_validation_bounds_shared_input_and_preserves_context_bytes() {
    let marker = "private-selected-context".repeat(1000);
    let raw = serde_json::to_vec(&json!({"messages":[{}, {}, {}, {}], "marker":marker})).unwrap();
    let Err(RecordError::ModelValidation(report)) = hydrate(
        Model::ChatCompletionRequest,
        InputOrigin::RetainedJson,
        &raw,
    ) else {
        panic!("invalid request accepted");
    };
    assert_eq!(report.len(), 9);
    assert!(report.retained_bytes() < raw.len() + 4000);
    assert!(!format!("{report:?} {report}").contains("private-selected-context"));
    assert!(matches!(
        report.to_json_bytes(512),
        Err(RecordError::TooLarge)
    ));
    let too_large = vec![b' '; MAX_RECORD_BYTES + 1];
    assert!(matches!(
        CompletionRequest::parse(&too_large),
        Err(RecordError::TooLarge)
    ));

    // Exact selected bytes are hashed, while labels retain NebulaModel trimming.
    use sha2::{Digest, Sha256};
    let context = " \tλ🌌\r\n";
    let body = json!({"provider_id":"p", "messages":[{"role":"user","content":" Hello "}],
        "context_attachments":[{"source_kind":" note ","source_label":" Label ","text":context,"sha256":format!("{:x}",Sha256::digest(context.as_bytes()))}]});
    let request = CompletionRequest::parse(&serde_json::to_vec(&body).unwrap()).unwrap();
    assert_eq!(request.fields["context_attachments"][0]["text"], context);
    assert_eq!(
        request.fields["context_attachments"][0]["source_label"],
        "Label"
    );
    assert_eq!(request.fields["messages"][0]["content"], "Hello");
}
