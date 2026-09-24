use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use serde_json::{Value, json};

fn payload(kind: Kind) -> Value {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-storage.json")).unwrap();
    fixture["entities"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["kind"] == kind.as_str())
        .unwrap()["payload"]
        .clone()
}

#[test]
fn persisted_schedule_times_match_python_utc_hydration_without_changing_other_offsets() {
    for (next, last, expected_next, expected_last) in [
        (
            "2026-09-23T14:01:00+02:00",
            Value::Null,
            "2026-09-23T12:01:00Z",
            Value::Null,
        ),
        (
            "2026-09-23T10:01:00-02:00",
            json!("2026-09-23T14:00:01.123456+02:00"),
            "2026-09-23T12:01:00Z",
            json!("2026-09-23T12:00:01.123456Z"),
        ),
        (
            "2026-09-23T14:01:00.000001+02:00",
            json!("2026-09-23T10:00:00-02:00"),
            "2026-09-23T12:01:00.000001Z",
            json!("2026-09-23T12:00:00Z"),
        ),
    ] {
        let mut p = payload(Kind::Schedule);
        p["next_run_at"] = next.into();
        p["last_run_at"] = last;
        let bytes = serde_json::to_vec(&p).unwrap();
        assert_eq!(
            StoredAssistantRecord::decode(Kind::Schedule, &bytes)
                .unwrap()
                .payload(),
            &p
        );
        let actual = StoredAssistantRecord::decode_persisted(Kind::Schedule, &bytes).unwrap();
        let mut expected = p.clone();
        expected["next_run_at"] = expected_next.into();
        expected["last_run_at"] = expected_last;
        assert_eq!(actual.payload(), &expected);
        assert_eq!(
            serde_json::from_slice::<Value>(&bytes).unwrap(),
            p,
            "hydration never changes input bytes"
        );
    }
    for field in ["next_run_at", "last_run_at"] {
        let mut p = payload(Kind::Schedule);
        p[field] = "2026-09-23T12:01:00".into();
        assert!(
            StoredAssistantRecord::decode_persisted(
                Kind::Schedule,
                &serde_json::to_vec(&p).unwrap()
            )
            .is_err()
        );
    }
    let mut legacy = payload(Kind::Schedule);
    legacy.as_object_mut().unwrap().remove("last_run_at");
    assert!(
        StoredAssistantRecord::decode_persisted(
            Kind::Schedule,
            &serde_json::to_vec(&legacy).unwrap()
        )
        .unwrap()
        .payload()["last_run_at"]
            .is_null()
    );
    legacy.as_object_mut().unwrap().remove("next_run_at");
    assert!(
        StoredAssistantRecord::decode_persisted(
            Kind::Schedule,
            &serde_json::to_vec(&legacy).unwrap()
        )
        .is_err()
    );
    for (kind, field) in [
        (Kind::Goal, "active_since"),
        (Kind::ReadCursor, "through_at"),
        (Kind::Subagent, "started_at"),
    ] {
        let mut p = payload(kind);
        p[field] = "2026-09-23T14:00:00+02:00".into();
        assert_eq!(
            StoredAssistantRecord::decode_persisted(kind, &serde_json::to_vec(&p).unwrap())
                .unwrap()
                .payload()[field],
            p[field]
        );
    }
}
