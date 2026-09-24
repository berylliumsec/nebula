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
