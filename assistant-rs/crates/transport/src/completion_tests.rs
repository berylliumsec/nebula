use super::*;
use nebula_assistant_domain::model_validation::completion::CompletionRequest;

#[tokio::test]
async fn completion_decode_matches_python_request_errors_and_root_shapes() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-execution.json")).unwrap();
    for case in fixture["request_vectors"].as_array().unwrap() {
        let result = decode::<CompletionRequest>(
            Body::from(case["raw_input"].as_str().unwrap().to_owned()),
            16 * 1024 * 1024,
        )
        .await;
        if case["expected"]["accepted"] == true {
            let Ok(request) = result else {
                panic!("{} rejected", case["name"]);
            };
            assert_eq!(
                serde_json::to_value(request).unwrap(),
                case["expected"]["payload"],
                "{}",
                case["name"]
            );
        } else {
            let Err(error) = result else {
                panic!("{} accepted", case["name"]);
            };
            let response = error.response("<generated>", None);
            assert_eq!(
                response.status().as_u16(),
                case["expected_http"]["status"].as_u64().unwrap() as u16
            );
            let mut body: Value = serde_json::from_slice(
                &to_bytes(response.into_body(), MAX_RESPONSE_BYTES)
                    .await
                    .unwrap(),
            )
            .unwrap();
            body["error_id"] = "<generated>".into();
            assert_eq!(body, case["expected_http"]["body"], "{}", case["name"]);
        }
    }
    for (raw, kind) in [
        ("", "missing"),
        ("null", "missing"),
        ("[]", "model_attributes_type"),
        ("3", "model_attributes_type"),
    ] {
        let Err(error) = decode::<CompletionRequest>(Body::from(raw), 1024).await else {
            panic!("non-object accepted");
        };
        let response = error.response("root", None);
        assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let body: Value = serde_json::from_slice(
            &to_bytes(response.into_body(), MAX_RESPONSE_BYTES)
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(body["detail"][0]["type"], kind);
        assert_eq!(body["detail"][0]["loc"], serde_json::json!(["body"]));
    }
}
