use nebula_assistant_domain::dependencies::{DependencyKind as Kind, StoredDependency};
use serde_json::{Value, json};

#[test]
fn retained_result_dependencies_preserve_python_records_and_reject_invalid_envelopes() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-results.json")).unwrap();
    assert!(fixture.get("capture_pending").is_none());
    for kind in [Kind::ToolCall, Kind::Artifact] {
        let rows: Vec<_> = fixture["dependency_records"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["kind"] == kind.as_str())
            .collect();
        assert!(!rows.is_empty());
        for row in &rows {
            let record =
                StoredDependency::decode(kind, &serde_json::to_vec(&row["payload"]).unwrap())
                    .unwrap();
            assert_eq!(record.payload(), &row["payload"]);
        }
        let canonical = rows[0]["payload"].clone();
        for field in ["id", "revision", "created_at", "updated_at"] {
            let mut p = canonical.clone();
            p.as_object_mut().unwrap().remove(field);
            assert!(StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).is_err());
        }
        for changes in [
            json!({"revision":0}),
            json!({"updated_at":"1900-01-01T00:00:00Z"}),
            json!({"unexpected":true}),
        ] {
            let mut p = canonical.clone();
            p.as_object_mut()
                .unwrap()
                .extend(changes.as_object().unwrap().clone());
            assert!(StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).is_err());
        }
        let mut p = canonical.clone();
        p["metadata"] = json!({"private":"retained-private-value", "opaque":[false,null,{"x":"  keep spaces  "}]});
        if kind == Kind::ToolCall {
            // Existing ToolCalls permit incomplete historical origin references.
            // Reading them neither invents ownership nor authorizes execution.
            p["origin"] = "chat".into();
            p["chat_session_id"] = Value::Null;
            p["started_at"] = "2030-01-01T13:00:00+01:00".into();
        } else {
            // Path validity belongs to artifact access, not record hydration.
            p["storage_path"] = "../unavailable".into();
        }
        let decoded = StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap();
        assert_eq!(decoded.payload(), &p);
        assert!(!format!("{decoded:?}").contains("retained-private-value"));
        let mut invalid = canonical;
        if kind == Kind::ToolCall {
            invalid["status"] = "unknown".into();
        } else {
            invalid["sha256"] = "not-a-digest".into();
        }
        assert!(StoredDependency::decode(kind, &serde_json::to_vec(&invalid).unwrap()).is_err());
    }
}
