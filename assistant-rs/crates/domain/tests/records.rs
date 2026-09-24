use nebula_assistant_domain::records::{
    AssistantKind, MAX_RECORD_BYTES, RecordError, StoredAssistantRecord,
};
use serde_json::{Value, json};

fn cases() -> Vec<Value> {
    serde_json::from_str::<Value>(include_str!("../../../compatibility/python-records.json"))
        .unwrap()["cases"]
        .as_array()
        .unwrap()
        .clone()
}

fn decode(kind: &str, payload: &Value) -> Result<StoredAssistantRecord, RecordError> {
    StoredAssistantRecord::decode(
        AssistantKind::try_from(kind).unwrap(),
        &serde_json::to_vec(payload).unwrap(),
    )
}

#[test]
fn canonical_python_records_round_trip_without_losing_metadata() {
    let mut kinds = std::collections::HashSet::new();
    let mut count = 0;
    for case in cases().into_iter().filter(|case| case["valid"] == true) {
        let kind = case["kind"].as_str().unwrap();
        let record = decode(kind, &case["payload"])
            .unwrap_or_else(|error| panic!("{}: {error}", case["name"]));
        assert_eq!(record.kind().as_str(), kind);
        assert_eq!(record.payload(), &case["payload"], "{}", case["name"]);
        assert_eq!(record.into_payload(), case["payload"], "{}", case["name"]);
        kinds.insert(kind.to_owned());
        count += 1;
    }
    assert_eq!(kinds.len(), AssistantKind::ALL.len());
    assert_eq!(count, 52);
}

#[test]
fn corrupt_records_match_python_rejection_cases() {
    let mut count = 0;
    for case in cases().into_iter().filter(|case| case["valid"] == false) {
        assert!(
            decode(case["kind"].as_str().unwrap(), &case["payload"]).is_err(),
            "{}",
            case["name"]
        );
        count += 1;
    }
    assert_eq!(count, 95);
}

#[test]
fn retracted_history_remains_present_but_is_not_current() {
    for case in cases().into_iter().filter(|case| case["valid"] == true) {
        let record = decode(case["kind"].as_str().unwrap(), &case["payload"]).unwrap();
        assert_eq!(
            record.is_replaced_message(),
            case["name"] == "message:replaced",
            "{}",
            case["name"]
        );
    }
    let mut message = cases()
        .into_iter()
        .find(|case| case["name"] == "message:replaced")
        .unwrap()["payload"]
        .clone();
    for value in [json!(1), json!(["retained"]), json!({"at":"retained"})] {
        message["metadata"]["retracted_at"] = value;
        assert!(
            decode("chat_messages", &message)
                .unwrap()
                .is_replaced_message()
        );
    }
}

#[test]
fn omitted_factory_fields_are_not_invented_on_read() {
    let mut payload = cases()[0]["payload"].clone();
    for field in ["id", "created_at", "updated_at", "revision"] {
        let original = payload.as_object_mut().unwrap().remove(field).unwrap();
        assert!(decode(cases()[0]["kind"].as_str().unwrap(), &payload).is_err());
        payload[field] = original;
    }
}

#[test]
fn malformed_or_oversized_records_fail_without_echoing_content() {
    assert_eq!(
        AssistantKind::try_from("missions"),
        Err(RecordError::UnknownKind)
    );
    assert_eq!(
        StoredAssistantRecord::decode(AssistantKind::Message, b"{secret"),
        Err(RecordError::Json)
    );
    assert_eq!(
        StoredAssistantRecord::decode(AssistantKind::Message, &vec![b' '; MAX_RECORD_BYTES + 1]),
        Err(RecordError::TooLarge)
    );
    let mut payload = cases()[0]["payload"].clone();
    payload["revision"] = json!("private-credential-sentinel");
    let error = decode(cases()[0]["kind"].as_str().unwrap(), &payload).unwrap_err();
    assert!(!error.to_string().contains("private-credential-sentinel"));
}

#[test]
fn shared_validators_accept_concurrent_readers() {
    let handles: Vec<_> = (0..16)
        .map(|_| {
            std::thread::spawn(|| {
                for case in cases().into_iter().filter(|case| case["valid"] == true) {
                    decode(case["kind"].as_str().unwrap(), &case["payload"]).unwrap();
                }
            })
        })
        .collect();
    for handle in handles {
        handle.join().unwrap();
    }
}

#[test]
fn legacy_defaults_match_python_without_inventing_identity() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-storage.json")).unwrap();
    let cases = fixture["legacy_records"].as_array().unwrap();
    assert_eq!(cases.len(), 13);
    for case in cases {
        let kind = AssistantKind::try_from(case["kind"].as_str().unwrap()).unwrap();
        let normalized = StoredAssistantRecord::decode_persisted(
            kind,
            &serde_json::to_vec(&case["payload"]).unwrap(),
        )
        .unwrap();
        assert_eq!(normalized.payload(), &case["normalized"], "{kind:?}");
        let mut offset_payload = case["normalized"].clone();
        for field in ["created_at", "updated_at"] {
            offset_payload[field] =
                chrono::DateTime::parse_from_rfc3339(offset_payload[field].as_str().unwrap())
                    .unwrap()
                    .with_timezone(&chrono::FixedOffset::east_opt(7200).unwrap())
                    .to_rfc3339_opts(chrono::SecondsFormat::AutoSi, false)
                    .into();
        }
        let mut expected = case["normalized"].clone();
        if kind == AssistantKind::ReadCursor {
            let through = json!("2026-09-23T14:00:00+02:00");
            offset_payload["through_at"] = through.clone();
            expected["through_at"] = through;
        }
        let bytes = serde_json::to_vec(&offset_payload).unwrap();
        assert_eq!(
            StoredAssistantRecord::decode(kind, &bytes)
                .unwrap()
                .payload(),
            &offset_payload
        );
        assert_eq!(
            StoredAssistantRecord::decode_persisted(kind, &bytes)
                .unwrap()
                .payload(),
            &expected
        );
        for field in ["id", "revision", "created_at", "updated_at"] {
            let mut missing = case["payload"].clone();
            missing.as_object_mut().unwrap().remove(field);
            assert!(
                StoredAssistantRecord::decode_persisted(
                    kind,
                    &serde_json::to_vec(&missing).unwrap()
                )
                .is_err()
            );
        }
    }
}
