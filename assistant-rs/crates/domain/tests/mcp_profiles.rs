use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    records::RecordError,
};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-settings.json")).unwrap()
}

#[test]
fn retained_mcp_profiles_match_python_defaults_coercions_and_custom_validators() {
    let fixture = fixture();
    let vectors = fixture["mcp_profile_vectors"].as_array().unwrap();
    assert!(vectors.len() >= 70);
    for vector in vectors {
        let encoded = vector["raw_input"].as_str().map_or_else(
            || serde_json::to_vec(&vector["input"]).unwrap(),
            |raw| raw.as_bytes().to_vec(),
        );
        assert_eq!(
            serde_json::from_slice::<Value>(&encoded).unwrap(),
            vector["input"],
            "{} raw input",
            vector["name"]
        );
        let decoded = StoredDependency::decode(DependencyKind::McpServerProfile, &encoded);
        if vector["expected"]["accepted"] == true {
            assert_eq!(
                decoded
                    .unwrap_or_else(|error| panic!("{}: {error}", vector["name"]))
                    .payload(),
                &vector["expected"]["payload"],
                "{}",
                vector["name"]
            );
        } else {
            assert!(decoded.is_err(), "{}", vector["name"]);
        }
    }
}

#[test]
fn mcp_hydration_keeps_timestamp_and_secret_reference_boundaries() {
    let fixture = fixture();
    let mut payload = fixture["mcp_profile_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["expected"]["accepted"] == true)
        .unwrap()["expected"]["payload"]
        .clone();
    payload["created_at"] = "2020-01-01T02:00:00+02:00".into();
    payload["updated_at"] = "2020-01-01T03:00:00+02:00".into();
    payload["capabilities"]["checked_at"] = "2020-01-01T02:00:00+02:00".into();
    payload["metadata"] = json!({"secret":"do-not-render-this-value"});
    let record = StoredDependency::decode(
        DependencyKind::McpServerProfile,
        &serde_json::to_vec(&payload).unwrap(),
    )
    .unwrap();
    assert_eq!(record.payload()["created_at"], "2020-01-01T00:00:00Z");
    assert_eq!(record.payload()["updated_at"], "2020-01-01T01:00:00Z");
    assert_eq!(
        record.payload()["capabilities"]["checked_at"],
        payload["capabilities"]["checked_at"]
    );
    assert!(!format!("{record:?}").contains("do-not-render"));
    for field in ["id", "revision", "created_at", "updated_at"] {
        let mut missing = payload.clone();
        missing.as_object_mut().unwrap().remove(field);
        assert!(
            StoredDependency::decode(
                DependencyKind::McpServerProfile,
                &serde_json::to_vec(&missing).unwrap()
            )
            .is_err()
        );
    }
    payload["metadata"] = json!({"large":"x".repeat(16*1024*1024)});
    assert!(matches!(
        StoredDependency::decode(
            DependencyKind::McpServerProfile,
            &serde_json::to_vec(&payload).unwrap()
        ),
        Err(RecordError::TooLarge)
    ));
    payload["metadata"] = json!({"large":"x".repeat(16*1024*1024-256*1024)});
    payload["capabilities"]["tools"] = json!(vec![json!({"name":"t"}); 2_000]);
    let encoded = serde_json::to_vec(&payload).unwrap();
    assert!(encoded.len() < 16 * 1024 * 1024);
    assert!(
        matches!(
            StoredDependency::decode(DependencyKind::McpServerProfile, &encoded),
            Err(RecordError::TooLarge)
        ),
        "deterministic nested defaults count toward the hydrated record bound"
    );
}
