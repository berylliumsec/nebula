use nebula_assistant_domain::tool_receipt::{
    MAX_BYTES, ReceiptError, ToolReceipt, bounded, exact_integer, hashable, python_equal,
    python_int, python_unique, serialize_model_result,
};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-recovery.json")).unwrap()
}

#[test]
fn recorded_receipt_codec_matches_python_coercion_and_rejection_vectors() {
    let fixture = fixture();
    let vectors = fixture["receipt_vectors"].as_array().unwrap();
    assert!(vectors.len() >= 129);
    let mut accepted = 0;
    let mut nonfinite = 0;
    for vector in vectors {
        let before = vector["input"].clone();
        let actual = ToolReceipt::decode(&vector["input"]);
        if vector["expected"]["accepted"] == true {
            let receipt = actual.unwrap_or_else(|e| panic!("{}: {e:?}", vector["name"]));
            assert_eq!(
                receipt.payload(),
                &vector["expected"]["payload"],
                "{}",
                vector["name"]
            );
            assert_eq!(
                receipt.serialize_model_result().unwrap(),
                vector["expected"]["serialized"],
                "{}",
                vector["name"]
            );
            let paths: Vec<_> = receipt
                .nonfinite_paths()
                .iter()
                .map(|n| json!({"path":n.path,"value":n.value}))
                .collect();
            assert_eq!(
                json!(paths),
                vector["expected"]["nonfinite_paths"],
                "{}",
                vector["name"]
            );
            nonfinite += paths.len();
            accepted += 1;
            assert_eq!(format!("{receipt:?}"), "ToolReceipt { redacted }");
        } else {
            assert!(
                matches!(actual, Err(ReceiptError::Invalid)),
                "{} must reject invalid receipts",
                vector["name"]
            );
        }
        assert_eq!(
            vector["input"], before,
            "Codec must not modify retained data"
        );
    }
    assert!(accepted >= 71 && accepted < vectors.len());
    assert!(nonfinite >= 3);
}

#[test]
fn recorded_result_serialization_matches_python_and_enforces_byte_bounds() {
    let fixture = fixture();
    let vectors = fixture["serialization_vectors"].as_array().unwrap();
    assert_eq!(vectors.len(), 13);
    for vector in vectors {
        let actual = serialize_model_result(&vector["input"]).unwrap();
        assert_eq!(actual, vector["expected"], "{}", vector["name"]);
        assert!(actual.len() <= 8192);
    }
    // JSON quotes count toward the input limit; Unicode limits use bytes.
    let at_limit = json!("x".repeat(MAX_BYTES - 2));
    assert_eq!(bounded(&at_limit).unwrap(), MAX_BYTES);
    let summary: Value = serde_json::from_str(&serialize_model_result(&at_limit).unwrap()).unwrap();
    assert_eq!(summary["original_bytes"], MAX_BYTES);
    assert_eq!(summary["status"], "incomplete");
    let too_large = json!("x".repeat(MAX_BYTES - 1));
    assert_eq!(bounded(&too_large), Err(ReceiptError::TooLarge));
    assert_eq!(
        serialize_model_result(&too_large),
        Err(ReceiptError::TooLarge)
    );
    assert!(matches!(
        ToolReceipt::decode(&too_large),
        Err(ReceiptError::TooLarge)
    ));
}

#[test]
fn recorded_python_numbers_preserve_precision_hashability_and_integer_rules() {
    let big: Value = serde_json::from_str("1000000000000000000000000000001").unwrap();
    assert_eq!(
        python_int(&big).unwrap().to_string(),
        "1000000000000000000000000000001"
    );
    assert_eq!(
        python_int(&json!("\u{a0}-١_٢\u{2003}"))
            .unwrap()
            .to_string(),
        "-12"
    );
    assert_eq!(python_int(&json!(-1.9)).unwrap().to_string(), "-1");
    assert_eq!(python_int(&json!(true)).unwrap().to_string(), "1");
    assert!(python_int(&json!("1.0")).is_err());
    assert!(python_int(&json!(null)).is_err());
    assert!(exact_integer(&json!(1.0)).is_none());
    assert!(exact_integer(&json!(true)).is_none());
    assert!(python_equal(
        &json!({"x":[true, 1.0]}),
        &json!({"x":[1, true]})
    ));
    assert!(!python_equal(
        &json!(9007199254740993_u64),
        &json!(9007199254740992.0)
    ));
    assert!(!python_equal(&big, &json!(1e30)));
    assert_eq!(
        python_unique([
            json!(true),
            json!(1),
            json!(1.0),
            json!(false),
            json!(-0.0),
            json!(0),
            json!(null),
            json!(null),
            json!("1")
        ])
        .unwrap(),
        vec![json!(true), json!(false), json!(null), json!("1")]
    );
    assert!(hashable(&json!([])).is_err());
    assert!(hashable(&json!({})).is_err());
    for sign in ["", "-"] {
        let accepted: Value = serde_json::from_str(&format!("{sign}{}", "1".repeat(4300))).unwrap();
        let rejected: Value = serde_json::from_str(&format!("{sign}{}", "1".repeat(4301))).unwrap();
        assert!(exact_integer(&accepted).is_some());
        assert!(exact_integer(&rejected).is_none());
    }
}
