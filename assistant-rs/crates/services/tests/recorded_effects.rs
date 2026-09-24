use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    records::{AssistantKind, StoredAssistantRecord},
    tool_receipt::{MAX_BYTES, ToolReceipt},
};
use nebula_assistant_services::{Error, recorded_effects};
use nebula_assistant_storage::entities::Error as StorageError;
use serde_json::{Map, Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-recovery.json")).unwrap()
}
fn turn(p: &Value) -> StoredAssistantRecord {
    StoredAssistantRecord::decode_persisted(AssistantKind::Turn, &serde_json::to_vec(p).unwrap())
        .unwrap()
}
fn dependency(kind: DependencyKind, p: &Value) -> StoredDependency {
    StoredDependency::decode(kind, &serde_json::to_vec(p).unwrap()).unwrap()
}
fn dependencies(vector: &Value, kind: DependencyKind) -> Vec<StoredDependency> {
    vector["dependencies"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["kind"] == kind.as_str())
        .map(|r| dependency(kind, &r["payload"]))
        .collect()
}
fn applied(p: &Value, changes: &Map<String, Value>) -> Value {
    let mut after = p.clone();
    after.as_object_mut().unwrap().extend(changes.clone());
    after
}
fn without_commit_fields(p: &Value) -> Value {
    let mut p = p.clone();
    p.as_object_mut().unwrap().remove("updated_at");
    p.as_object_mut().unwrap().remove("revision");
    p
}
fn requested(p: &Value, field: &str) -> Vec<String> {
    if p["status"] != "interrupted" {
        return vec![];
    }
    p["request_snapshot"]["recovery"][field]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|id| id.as_str().map(str::to_owned))
        .collect()
}

#[test]
fn recorded_tool_reductions_match_python_phase_vectors_and_errors() {
    let fixture = fixture();
    let vectors = fixture["tool_repair_vectors"].as_array().unwrap();
    assert_eq!(vectors.len(), 64);
    let mut adopted = 0;
    let mut errors = 0;
    for vector in vectors {
        let record = turn(&vector["turn"]["payload"]);
        let before = record.payload().clone();
        let deps = dependencies(vector, DependencyKind::ToolCall);
        let mut looked_up = Vec::new();
        let actual = recorded_effects::tools(&record, |id| {
            looked_up.push(id.to_owned());
            Ok(deps.iter().find(|d| d.payload()["id"] == id))
        });
        assert_eq!(
            looked_up,
            requested(&before, "unknown_tool_call_ids"),
            "{}",
            vector["name"]
        );
        if let Some(expected) = vector["expected"].get("error") {
            match actual {
                Err(Error::LegacyValueError(detail)) => {
                    assert_eq!(expected["type"], "ValueError", "{}", vector["name"]);
                    assert_eq!(expected["detail"], detail, "{}", vector["name"]);
                }
                Err(Error::LegacyUnhandled) => {
                    assert_eq!(expected["type"], "TypeError", "{}", vector["name"])
                }
                _ => panic!("{}: expected recorded history error", vector["name"]),
            }
            errors += 1;
        } else {
            let changes = actual.unwrap_or_else(|e| panic!("{}: {e:?}", vector["name"]));
            let after = changes
                .as_ref()
                .map_or_else(|| before.clone(), |changes| applied(&before, changes));
            assert_eq!(
                without_commit_fields(&after),
                without_commit_fields(&vector["expected"]["turn"]["payload"]),
                "{}",
                vector["name"]
            );
            if let Some(changes) = changes {
                assert_eq!(
                    vector["expected"]["changes"].as_array().unwrap().len(),
                    1,
                    "{}",
                    vector["name"]
                );
                let allowed = [
                    "tool_call_ids",
                    "tool_history",
                    "next_step",
                    "execution_tool_calls",
                    "artifact_queries",
                    "error",
                    "request_snapshot",
                ];
                assert!(changes.keys().all(|k| allowed.contains(&k.as_str())));
                adopted += 1;
            } else {
                assert_eq!(
                    vector["expected"]["changes"],
                    json!([]),
                    "{}",
                    vector["name"]
                );
            }
        }
        assert_eq!(
            record.payload(),
            &before,
            "Reducers must not mutate retained records"
        );
    }
    assert!(adopted >= 10);
    assert_eq!(errors, 5);
    for vector in fixture["serialization_vectors"].as_array().unwrap() {
        assert_eq!(
            recorded_effects::result_summary(&vector["input"]).unwrap(),
            vector["result_summary"],
            "{}",
            vector["name"]
        );
    }
    for vector in fixture["receipt_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|v| v["expected"]["accepted"] == true)
    {
        let receipt = ToolReceipt::decode(&vector["input"]).unwrap();
        assert_eq!(
            recorded_effects::result_summary(receipt.payload()).unwrap(),
            vector["expected"]["result_summary"],
            "{}",
            vector["name"]
        );
    }
}

#[test]
fn recorded_hook_reductions_match_python_and_preserve_atomic_conflict_inputs() {
    let fixture = fixture();
    let vectors = fixture["hook_repair_vectors"].as_array().unwrap();
    assert_eq!(vectors.len(), 59);
    let mut late = 0;
    let mut conflicts = 0;
    for vector in vectors {
        let record = turn(&vector["turn"]["payload"]);
        let before = record.payload().clone();
        let deps = dependencies(vector, DependencyKind::NativeHookExecution);
        let dependency_before: Vec<_> = deps.iter().map(|d| d.payload().clone()).collect();
        let mut looked_up = Vec::new();
        let actual = recorded_effects::hooks(&record, |id| {
            looked_up.push(id.to_owned());
            Ok(deps.iter().find(|d| d.payload()["id"] == id))
        });
        assert_eq!(
            looked_up,
            requested(&before, "unknown_hook_execution_ids"),
            "{}",
            vector["name"]
        );
        if vector["expected"]["error"]["type"] == "ChatHistoryConflict" {
            // Python's conflict occurs in the atomic writer, after pure reduction.
            // Retain both original revisions so a writer cannot silently dedupe.
            let reduction = actual.unwrap().unwrap();
            assert!(
                reduction
                    .late_hooks
                    .windows(2)
                    .any(|pair| pair[0].id == pair[1].id
                        && pair[0].expected_revision == pair[1].expected_revision)
            );
            assert_eq!(vector["expected"]["changes"], json!([]));
            conflicts += 1;
        } else if vector["expected"].get("error").is_some() {
            assert!(
                matches!(actual, Err(Error::LegacyStorageUnhandled)),
                "{}",
                vector["name"]
            );
            assert_eq!(vector["expected"]["error"]["type"], "TypeError");
        } else {
            let reduction = actual.unwrap_or_else(|e| panic!("{}: {e:?}", vector["name"]));
            let after = reduction
                .as_ref()
                .map_or_else(|| before.clone(), |r| applied(&before, &r.turn_changes));
            assert_eq!(
                without_commit_fields(&after),
                without_commit_fields(&vector["expected"]["turn"]["payload"]),
                "{}",
                vector["name"]
            );
            if let Some(reduction) = reduction {
                assert_eq!(
                    vector["expected"]["changes"].as_array().unwrap().len(),
                    1 + reduction.late_hooks.len(),
                    "{}",
                    vector["name"]
                );
                for patch in reduction.late_hooks {
                    let dep = deps.iter().find(|d| d.payload()["id"] == patch.id).unwrap();
                    assert_eq!(dep.payload()["revision"], patch.expected_revision);
                    let expected = vector["expected"]["changes"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|c| c["after"]["payload"]["id"] == patch.id)
                        .unwrap();
                    assert_eq!(
                        without_commit_fields(&applied(dep.payload(), &patch.changes)),
                        without_commit_fields(&expected["after"]["payload"]),
                        "{}",
                        vector["name"]
                    );
                    assert_eq!(patch.changes.len(), 6);
                    late += 1;
                }
            } else {
                assert_eq!(
                    vector["expected"]["changes"],
                    json!([]),
                    "{}",
                    vector["name"]
                );
            }
        }
        assert_eq!(record.payload(), &before);
        assert_eq!(
            deps.iter().map(|d| d.payload().clone()).collect::<Vec<_>>(),
            dependency_before
        );
    }
    assert_eq!(conflicts, 2);
    assert!(late >= 2);
}

#[test]
fn recorded_reductions_are_idempotent_precise_and_bound_expanded_history() {
    let fixture = fixture();
    let vector = fixture["tool_repair_vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["name"] == "adopt-complete-pending-turn")
        .unwrap();
    let mut p = vector["turn"]["payload"].clone();
    let big: Value = serde_json::from_str("1000000000000000000000000000000").unwrap();
    p["next_step"] = big.clone();
    p["execution_tool_calls"] = big.clone();
    p["artifact_queries"] = big.clone();
    let call = dependency(
        DependencyKind::ToolCall,
        &vector["dependencies"][0]["payload"],
    );
    // Two receipt references produce one history entry and one counter increment.
    p["request_snapshot"]["recovery"]["unknown_tool_call_ids"] =
        json!([call.payload()["id"], call.payload()["id"]]);
    p["request_snapshot"]["recovery"]["recorded_tool_result_ids"] =
        json!([true, 1, 1.0, false, 0, -0.0, null, null]);
    let source = turn(&p);
    let changes = recorded_effects::tools(&source, |_| Ok(Some(&call)))
        .unwrap()
        .unwrap();
    assert_eq!(
        changes["execution_tool_calls"].to_string(),
        "1000000000000000000000000000001"
    );
    assert_eq!(changes["artifact_queries"], big);
    assert_eq!(changes["next_step"], big);
    assert_eq!(changes["tool_history"].as_array().unwrap().len(), 1);
    assert_eq!(
        changes["request_snapshot"]["recovery"]["recorded_tool_result_ids"],
        json!([true, false, null, call.payload()["id"]])
    );
    let after = turn(&applied(&p, &changes));
    assert!(
        recorded_effects::tools(&after, |_| panic!("Cleared references must not be reread"))
            .unwrap()
            .is_none()
    );
    assert_eq!(source.payload(), &p);

    // Each retained input fits the record bound; their expanded history does not.
    let mut first = call.payload().clone();
    first["arguments"] = json!({"padding":"x".repeat(MAX_BYTES/2)});
    let mut second = first.clone();
    second["id"] = json!("second-recorded-call");
    second["result"]["tool_call_id"] = second["id"].clone();
    second["metadata"]["provider_call_id"] = json!("second-model-call");
    p["request_snapshot"]["recovery"]["unknown_tool_call_ids"] = json!([first["id"], second["id"]]);
    let first = dependency(DependencyKind::ToolCall, &first);
    let second = dependency(DependencyKind::ToolCall, &second);
    let source = turn(&p);
    assert!(matches!(
        recorded_effects::tools(&source, |id| Ok(Some(if first.payload()["id"] == id {
            &first
        } else {
            &second
        }))),
        Err(Error::Storage(StorageError::ReadLimit))
    ));
    assert_eq!(
        source.payload(),
        &p,
        "Rejected expansion leaves source unchanged"
    );
}
