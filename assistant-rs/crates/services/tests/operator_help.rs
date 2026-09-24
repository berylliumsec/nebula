use nebula_assistant_services::operator_help::{self, Error, MAX_QUERY_BYTES, MAX_QUERY_COUNT};
use serde_json::{Number, Value, json};
fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-operator-help.json"
    ))
    .unwrap()
}
fn queries(case: &Value) -> Vec<String> {
    case["queries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().into())
        .collect()
}
#[test]
fn packaged_help_corpus_and_search_match_python() {
    let fixture = fixture();
    assert_eq!(operator_help::CORPUS_ID, fixture["corpus_id"]);
    assert_eq!(operator_help::CORPUS_SHA256, fixture["corpus_sha256"]);
    let articles = operator_help::operator_help_articles().unwrap();
    assert_eq!(articles.len(), 14);
    assert_eq!(serde_json::to_value(articles).unwrap(), fixture["articles"]);
    assert_eq!(fixture["search_vectors"].as_array().unwrap().len(), 37);
    for case in fixture["search_vectors"].as_array().unwrap() {
        assert_eq!(
            serde_json::to_value(
                operator_help::search_operator_help(
                    &queries(case),
                    case["limit"].as_u64().unwrap() as usize
                )
                .unwrap()
            )
            .unwrap(),
            case["expected"],
            "{}",
            case["name"]
        );
    }
}
#[test]
fn packaged_help_budget_and_prompt_projection_match_python() {
    let fixture = fixture();
    assert_eq!(fixture["projection_vectors"].as_array().unwrap().len(), 17);
    for case in fixture["projection_vectors"].as_array().unwrap() {
        let projection = operator_help::prepare_operator_help(
            &queries(case),
            case["token_budget"].as_number().unwrap(),
        )
        .unwrap();
        assert_eq!(
            serde_json::to_value(projection).unwrap(),
            case["expected"],
            "{}",
            case["name"]
        );
    }
    for case in fixture["budget_vectors"].as_array().unwrap() {
        assert_eq!(
            operator_help::operator_help_budget(case["target_input_tokens"].as_number().unwrap())
                .unwrap(),
            *case["expected"].as_number().unwrap()
        );
    }
}
#[test]
fn packaged_help_bounds_and_redaction_are_explicit() {
    assert_eq!(
        operator_help::search_operator_help(&["x".repeat(MAX_QUERY_BYTES + 1)], 8).unwrap_err(),
        Error::Capacity
    );
    assert_eq!(
        operator_help::search_operator_help(&vec!["".into(); MAX_QUERY_COUNT + 1], 8).unwrap_err(),
        Error::Capacity
    );
    assert!(
        operator_help::search_operator_help(&["x".repeat(MAX_QUERY_BYTES + 1)], 0)
            .unwrap()
            .is_empty()
    );
    let many_terms = std::iter::once("provider".to_owned())
        .chain((0..10001).map(|i| format!("distinct{i}")))
        .collect::<Vec<_>>()
        .join(" ");
    assert_eq!(
        operator_help::search_operator_help(&[many_terms], 8).unwrap_err(),
        Error::Capacity
    );
    assert_eq!(
        operator_help::prepare_operator_help(&["provider".into()], &Number::from_f64(1.5).unwrap())
            .unwrap_err(),
        Error::InvalidBudget
    );
    let oversized: Number = format!("1{}", "0".repeat(4300)).parse().unwrap();
    assert_eq!(
        operator_help::operator_help_budget(&oversized).unwrap_err(),
        Error::Capacity
    );
    let projection = operator_help::prepare_operator_help(
        &["provider private-query-do-not-log failed".into()],
        &100000.into(),
    )
    .unwrap();
    assert!(!format!("{projection:?}").contains("private-query"));
    let encoded = serde_json::to_value(&projection).unwrap();
    assert!(
        !encoded["instruction_suffix"]
            .as_str()
            .unwrap()
            .contains("private-query")
    );
    assert!(
        projection
            .citations
            .iter()
            .all(|c| c.source_id.starts_with("nebula-help:")
                && c.artifact_id.is_none()
                && c.page.is_none())
    );
    let no_help = operator_help::prepare_operator_help(&["Hello".into()], &100000.into()).unwrap();
    assert_eq!(
        serde_json::to_value(no_help).unwrap(),
        json!({"chunks":[],"citations":[],"instruction_suffix":"","estimated_tokens":0})
    );
}
