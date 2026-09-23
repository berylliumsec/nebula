use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};
use serde_json::{Value, json};

fn decode(p: &Value) -> Result<StoredDependency, nebula_assistant_domain::records::RecordError> {
    StoredDependency::decode(
        DependencyKind::NativeHookExecution,
        &serde_json::to_vec(p).unwrap(),
    )
}

#[test]
fn native_hook_records_preserve_history_and_require_recorded_terminal_times() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-status.json")).unwrap();
    assert!(fixture.get("capture_pending").is_none());
    let hooks: Vec<_> = fixture["dependency_records"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["kind"] == "native_hook_executions")
        .collect();
    assert!(!hooks.is_empty());
    for hook in hooks {
        assert_eq!(
            decode(&hook["payload"]).unwrap().payload(),
            &hook["payload"]
        );
    }
    let canonical = decode(&json!({
        "id":"hook","revision":1,"created_at":"2026-09-23T12:00:00Z","updated_at":"2026-09-23T12:00:00Z",
        "engagement_id":"project","hook_id":"retained","hook_snapshot":{"opaque":"  private-hook-value  "},
        "event_name":"chat.turn.completed","started_at":"2026-09-23T13:00:00+01:00"
    })).unwrap();
    assert_eq!(canonical.payload()["owner_kind"], "chat");
    assert_eq!(canonical.payload()["status"], "running");
    assert!(canonical.payload()["completed_at"].is_null());
    assert!(!format!("{canonical:?}").contains("private-hook-value"));
    for field in ["id", "revision", "created_at", "updated_at", "started_at"] {
        let mut p = canonical.payload().clone();
        p.as_object_mut().unwrap().remove(field);
        assert!(decode(&p).is_err(), "missing {field}");
    }
    for status in [
        "complete",
        "failed",
        "timed_out",
        "interrupted",
        "reconciled",
    ] {
        let mut p = canonical.payload().clone();
        p["status"] = status.into();
        assert!(decode(&p).is_err(), "{status} requires completion");
        p["completed_at"] = "2026-09-23T13:01:00+01:00".into();
        assert_eq!(decode(&p).unwrap().payload(), &p);
    }
    let mut p = canonical.payload().clone();
    p["completed_at"] = "2026-09-23T12:01:00Z".into();
    assert!(decode(&p).is_err(), "running must not have completion");
    for changes in [
        json!({"status":"unknown"}),
        json!({"stdout":"x".repeat(65537)}),
        json!({"error":"x".repeat(1001)}),
        json!({"unexpected":true}),
    ] {
        let mut p = canonical.payload().clone();
        p.as_object_mut()
            .unwrap()
            .extend(changes.as_object().unwrap().clone());
        assert!(decode(&p).is_err());
    }
    let mut p = canonical.payload().clone();
    p["created_at"] = "2026-09-23T14:00:00+02:00".into();
    p["updated_at"] = "2026-09-23T14:00:00+02:00".into();
    p["started_at"] = "2026-09-23T12:00:00".into();
    p["owner_kind"] = "mission".into();
    p["chat_session_id"] = "historical-chat-reference".into();
    p["late_outcome"] = json!({"status":"complete","exit_code":0,"stdout":"","stderr":"","error":null,"observed_at":"2026-09-23T14:01:00+02:00"});
    p["reconciliation"] = json!({"opaque":[null,false,{"preserved":"  spaces  "}]});
    let decoded = decode(&p).unwrap();
    assert_eq!(decoded.payload()["created_at"], "2026-09-23T12:00:00Z");
    assert_eq!(decoded.payload()["updated_at"], "2026-09-23T12:00:00Z");
    assert_eq!(decoded.payload()["started_at"], p["started_at"]);
    assert_eq!(decoded.payload()["late_outcome"], p["late_outcome"]);
    assert_eq!(decoded.payload()["reconciliation"], p["reconciliation"]);
    p["late_outcome"]
        .as_object_mut()
        .unwrap()
        .remove("observed_at");
    assert!(
        decode(&p).is_err(),
        "cannot invent an observation timestamp on read"
    );
}
