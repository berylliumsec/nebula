use chrono::{DateTime, FixedOffset, SecondsFormat};
use nebula_assistant_domain::dependencies::{DependencyKind as Kind, StoredDependency};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-catchup.json")).unwrap()
}

fn expected_payload(row: &Value) -> Value {
    let mut expected = row["payload"].clone();
    if expected["id"] == "approval-active" {
        // The oracle deliberately stores this row at -02:00. Python Entity
        // hydration returns these exact UTC instants; do not normalize actual.
        expected["created_at"] = "2020-01-01T00:00:00Z".into();
        expected["updated_at"] = "2020-01-01T00:00:30Z".into();
    }
    expected
}

#[test]
fn shared_dependency_records_preserve_python_fields_and_reject_incoherent_state() {
    let fixture = fixture();
    for kind in [Kind::Approval, Kind::HarnessInteraction, Kind::HarnessTurn] {
        let rows: Vec<_> = fixture["dependency_records"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["kind"] == kind.as_str())
            .collect();
        assert!(!rows.is_empty(), "{} is covered", kind.as_str());
        for row in &rows {
            let record =
                StoredDependency::decode(kind, &serde_json::to_vec(&row["payload"]).unwrap())
                    .unwrap();
            assert_eq!(record.payload(), &expected_payload(row));
        }
        let canonical = expected_payload(rows[0]);
        let mut offset_times = canonical.clone();
        for field in ["created_at", "updated_at"] {
            offset_times[field] = DateTime::parse_from_rfc3339(canonical[field].as_str().unwrap())
                .unwrap()
                .with_timezone(&FixedOffset::east_opt(3600).unwrap())
                .to_rfc3339_opts(SecondsFormat::AutoSi, false)
                .into();
        }
        let normalized =
            StoredDependency::decode(kind, &serde_json::to_vec(&offset_times).unwrap()).unwrap();
        assert_eq!(normalized.payload()["created_at"], canonical["created_at"]);
        assert_eq!(normalized.payload()["updated_at"], canonical["updated_at"]);
        for field in ["id", "created_at", "updated_at", "revision"] {
            let mut p = canonical.clone();
            p.as_object_mut().unwrap().remove(field);
            assert!(StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).is_err());
        }
        for changes in [
            json!({"status":"unknown"}),
            json!({"updated_at":"1900-01-01T00:00:00Z"}),
            json!({"revision":0}),
            json!({"unknown":true}),
        ] {
            let mut p = canonical.clone();
            p.as_object_mut()
                .unwrap()
                .extend(changes.as_object().unwrap().clone());
            assert!(StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).is_err());
        }
        let mut p = canonical;
        match kind {
            Kind::Approval => {
                p.as_object_mut().unwrap().remove("requested_at");
            }
            Kind::HarnessTurn => {
                p["origin"] = "analysis".into();
                p["run_id"] = "forbidden-owner".into();
            }
            Kind::HarnessInteraction => {
                p["contains_secret"] = true.into();
                p["response"] = json!({"secret":"must-not-persist"});
            }
            Kind::ToolCall
            | Kind::Artifact
            | Kind::NativeHookExecution
            | Kind::HarnessProfile
            | Kind::McpServerProfile
            | Kind::Engagement
            | Kind::ProviderProfile
            | Kind::ScopePolicy
            | Kind::HarnessSession => {
                unreachable!("covered by retained read dependency tests")
            }
        }
        assert!(StoredDependency::decode(kind, &serde_json::to_vec(&p).unwrap()).is_err());
    }
}

#[test]
fn shared_dependency_debug_redacts_payload_and_terminal_resolution_is_exact() {
    let fixture = fixture();
    let rows = fixture["dependency_records"].as_array().unwrap();
    let mut approval = rows.iter().find(|r| r["kind"] == "approvals").unwrap()["payload"].clone();
    approval["requested_at"] = "2030-01-01T13:00:00+01:00".into();
    approval["expires_at"] = "2040-01-01T13:00:00+01:00".into();
    approval["exact_request"] = json!({"secret":"private-value-must-not-print"});
    let record =
        StoredDependency::decode(Kind::Approval, &serde_json::to_vec(&approval).unwrap()).unwrap();
    assert_eq!(record.payload()["requested_at"], approval["requested_at"]);
    assert_eq!(record.payload()["expires_at"], approval["expires_at"]);
    assert!(!format!("{record:?}").contains("private-value"));
    approval["continuation"] = json!({"harness_turn_id":"fixture"});
    assert!(
        StoredDependency::decode(Kind::Approval, &serde_json::to_vec(&approval).unwrap()).is_err()
    );
    let mut question = rows
        .iter()
        .find(|r| r["kind"] == "harness_interactions")
        .unwrap()["payload"]
        .clone();
    for (status, resolved) in [
        ("pending", json!("2030-01-01T12:00:00Z")),
        ("answered", Value::Null),
    ] {
        question["status"] = status.into();
        question["resolved_at"] = resolved;
        assert!(
            StoredDependency::decode(
                Kind::HarnessInteraction,
                &serde_json::to_vec(&question).unwrap()
            )
            .is_err()
        );
    }
}
