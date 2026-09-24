use nebula_assistant_domain::{
    records::{MAX_RECORD_BYTES, RecordError},
    retained_json::repair_turn_json,
};
use serde_json::{Value, json};

#[test]
fn repaired_turn_json_preserves_opaque_order_through_history_reordering_and_bounds() {
    let raw = r#"{"revision":1,"tool_history":[{"step":9,"status":"waiting_callback","results_url":{"z":1,"a":[true,"α"]}},{"step":2,"name":"saved"}],"request_snapshot":{"z":0,"a":1}}"#;
    let mut next: Value = serde_json::from_str(raw).unwrap();
    next["revision"] = 2.into();
    next["request_snapshot"]["recovery"] = json!({"unknown_tool_call_ids":[]});
    next["tool_history"].as_array_mut().unwrap().reverse();
    next["tool_history"]
        .as_array_mut()
        .unwrap()
        .push(json!({"step":10,"status":"complete"}));
    let output = repair_turn_json(raw, &next).unwrap();
    assert_eq!(serde_json::from_str::<Value>(&output).unwrap(), next);
    assert!(output.contains(r#""results_url":{"z":1,"a":[true,"α"]}"#));
    assert!(output.find(r#""name":"saved""#).unwrap() < output.find(r#""results_url""#).unwrap());

    let mut hook_only: Value = serde_json::from_str(raw).unwrap();
    hook_only["revision"] = 3.into();
    let preserved = repair_turn_json(raw, &hook_only).unwrap();
    assert!(preserved.contains(r#""request_snapshot":{"z":0,"a":1}"#));
    assert!(preserved.contains(r#"[{"step":9,"status":"waiting_callback","results_url":{"z":1,"a":[true,"α"]}},{"step":2,"name":"saved"}]"#));

    let defaults = json!({"tool_history":[],"revision":2});
    assert_eq!(
        serde_json::from_str::<Value>(&repair_turn_json(r#"{"revision":1}"#, &defaults).unwrap())
            .unwrap(),
        defaults
    );
    assert_eq!(
        repair_turn_json(raw, &json!({"content":"x".repeat(MAX_RECORD_BYTES)})),
        Err(RecordError::TooLarge)
    );
    assert_eq!(repair_turn_json("[1]", &next), Err(RecordError::Json));

    let duplicates = r#"{"tool_history":[{"step":1,"status":"waiting_callback","process_id":{"a":1,"z":2}},{"step":1,"status":"waiting_callback","process_id":{"z":2,"a":1}}]}"#;
    let mut changed: Value = serde_json::from_str(duplicates).unwrap();
    changed["tool_history"]
        .as_array_mut()
        .unwrap()
        .push(json!({"step":2,"status":"complete"}));
    let retained = repair_turn_json(duplicates, &changed).unwrap();
    let first = retained.find(r#""process_id":{"a":1,"z":2}"#).unwrap();
    let second = retained.find(r#""process_id":{"z":2,"a":1}"#).unwrap();
    assert!(first < second);
}
