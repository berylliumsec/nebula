use chrono::{DateTime, Duration, Utc};
use nebula_assistant_domain::{
    dependencies::{DependencyKind as Dependency, StoredDependency},
    records::{AssistantKind as Kind, StoredAssistantRecord},
    session_state::{ConnectionState, StateError, StateInputs, digest, progress_requests, project},
};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-state.json")).unwrap()
}
fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn dependency(kind: Dependency, payload: &Value) -> StoredDependency {
    StoredDependency::decode(kind, &serde_json::to_vec(payload).unwrap()).unwrap()
}
fn inputs(session_id: &str) -> StateInputs {
    let f = fixture();
    let records = f["initial_records"].as_array().unwrap();
    let session = records
        .iter()
        .find(|r| r["kind"] == "chat_sessions" && r["payload"]["id"] == session_id)
        .unwrap();
    let session = StoredAssistantRecord::decode_persisted(
        Kind::Session,
        &serde_json::to_vec(&session["payload"]).unwrap(),
    )
    .unwrap();
    let p = session.payload();
    let mut turns: Vec<_> = records
        .iter()
        .filter(|r| {
            r["kind"] == "chat_turns"
                && r["payload"]["session_id"] == session_id
                && r["payload"]["engagement_id"] == p["engagement_id"]
        })
        .map(|r| {
            StoredAssistantRecord::decode_persisted(
                Kind::Turn,
                &serde_json::to_vec(&r["payload"]).unwrap(),
            )
            .unwrap()
        })
        .collect();
    turns.sort_by(|a, b| {
        (
            b.payload()["created_at"].as_str(),
            b.payload()["id"].as_str(),
        )
            .cmp(&(
                a.payload()["created_at"].as_str(),
                a.payload()["id"].as_str(),
            ))
    });
    let harness_ids: HashSet<_> = turns
        .iter()
        .filter_map(|r| r.payload()["harness_turn_id"].as_str())
        .collect();
    let deps = f["dependency_records"].as_array().unwrap();
    let approvals = deps
        .iter()
        .filter(|r| {
            r["kind"] == "approvals"
                && r["payload"]["chat_session_id"] == session_id
                && r["payload"]["engagement_id"] == p["engagement_id"]
        })
        .map(|r| dependency(Dependency::Approval, &r["payload"]))
        .collect();
    let questions = deps
        .iter()
        .filter(|r| {
            r["kind"] == "harness_interactions"
                && r["payload"]["chat_session_id"] == session_id
                && r["payload"]["engagement_id"] == p["engagement_id"]
        })
        .map(|r| dependency(Dependency::HarnessInteraction, &r["payload"]))
        .collect();
    let harnesses = deps
        .iter()
        .filter(|r| {
            r["kind"] == "harness_turns"
                && r["payload"]["engagement_id"] == p["engagement_id"]
                && r["payload"]["id"]
                    .as_str()
                    .is_some_and(|id| harness_ids.contains(id))
        })
        .map(|r| dependency(Dependency::HarnessTurn, &r["payload"]))
        .collect();
    let profile = deps
        .iter()
        .find(|r| r["kind"] == "harnesses" && r["payload"]["id"] == p["harness_profile_id"])
        .map(|r| dependency(Dependency::HarnessProfile, &r["payload"]));
    StateInputs {
        session,
        turns,
        approvals,
        questions,
        harnesses,
        profile,
        progress: HashMap::new(),
    }
}

#[test]
fn harness_profile_decoder_matches_python_retained_validation_vectors() {
    let f = fixture();
    let vectors = f["profile_vectors"].as_array().unwrap();
    assert_eq!(vectors.len(), 85);
    let mut accepted = 0;
    for vector in vectors {
        let result = StoredDependency::decode(
            Dependency::HarnessProfile,
            &serde_json::to_vec(&vector["input"]).unwrap(),
        );
        if vector["expected"]["accepted"] == true {
            let record = result.unwrap_or_else(|e| panic!("{}: {e:?}", vector["name"]));
            assert_eq!(
                record.payload(),
                &vector["expected"]["payload"],
                "{}",
                vector["name"]
            );
            assert_eq!(record.kind(), Dependency::HarnessProfile);
            accepted += 1;
        } else {
            assert!(result.is_err(), "{} must be rejected", vector["name"]);
        }
    }
    assert_eq!(accepted, 35);
    let mut minimal = vectors[0]["input"].clone();
    for field in ["id", "created_at", "updated_at", "revision"] {
        let mut missing = minimal.clone();
        missing.as_object_mut().unwrap().remove(field);
        assert!(
            StoredDependency::decode(
                Dependency::HarnessProfile,
                &serde_json::to_vec(&missing).unwrap()
            )
            .is_err()
        );
    }
    minimal["metadata"] = json!({"private":"Never expose this retained metadata"});
    let record = dependency(Dependency::HarnessProfile, &minimal);
    assert!(!format!("{record:?}").contains("Never expose"));
}

#[test]
fn session_state_digest_matches_python_ascii_vectors_and_resource_bounds() {
    // Generated with CPython3.12 json.dumps(sort_keys=True,separators=(',',':'))
    // and hashlib.sha256. DEL, short escapes and astral surrogate pairs are
    // deliberately independent of Rust's JSON serializer behavior.
    let vectors = [
        (
            json!({}),
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        ),
        (
            json!({"b":false,"a":null,"zero":0}),
            "162d8ace65496292324af29316b5bec022f30b62e589655bac50eb5dd3a12955",
        ),
        (
            json!({"control":"\0\u{8}\u{c}\n\r\t\u{1f}\u{7f}"}),
            "3e3bab13507c3131d31f48d8daba1787d61db605143a3ce9bbe5c96838615fc4",
        ),
        (
            json!({"z":"β🦀","a":"\"\\/\u{2028}"}),
            "bea5e2485c33f4850b0e2ab2bf489cfcc61e41dc219a2606bd2f76b5ae3f6b17",
        ),
        (
            serde_json::from_str::<Value>(
                r#"{"n":100000000000000000000000000000000000,"f":[1.0,-0.0,1e-5,1e16]}"#,
            )
            .unwrap(),
            "b696307bc8592275e6ffc8ec18bd62ca7add105edd4750656877418161cb2295",
        ),
        (
            json!({"a":["é","\u{10000}"],"β":{"🦀":"x"}}),
            "10d302064716def26f900de8714ace77cb261afc682de9e09d505f2e73d7efea",
        ),
    ];
    for (value, expected) in vectors {
        assert_eq!(digest(&value).unwrap(), expected);
    }
    let f = fixture();
    for watermark in f["initial_watermarks"].as_array().unwrap() {
        let input = inputs(watermark["session_id"].as_str().unwrap());
        assert_eq!(
            digest(&project(&input, now(), ConnectionState::Unknown).unwrap()).unwrap(),
            watermark["digest"]
        );
    }
    const LIMIT: usize = 16 * 1024 * 1024;
    assert_eq!(
        digest(&Value::String("x".repeat(LIMIT - 2))).unwrap().len(),
        64,
        "Exactly16MiB including JSON quotes is allowed"
    );
    assert_eq!(
        digest(&Value::String("x".repeat(LIMIT - 1))),
        Err(StateError::TooLarge)
    );
    assert_eq!(
        digest(&Value::String("β".repeat(LIMIT / 6 + 1))),
        Err(StateError::TooLarge),
        "ASCII-escape expansion has its own bound"
    );
}

#[test]
fn session_state_projection_preserves_expiry_progress_precedence_and_bounds() {
    let mut state = inputs("sequence");
    assert_eq!(
        project(&state, now(), ConnectionState::Connected).unwrap()["execution"],
        "waiting_approval"
    );
    let original = state.approvals[0].payload().clone();
    let mut expiry = original.clone();
    expiry["expires_at"] = "2030-01-01T12:00:00.000000001Z".into();
    state.approvals[0] = dependency(Dependency::Approval, &expiry);
    let expired = project(&state, now(), ConnectionState::Connected).unwrap();
    assert_eq!(
        expired["pending"],
        json!([]),
        "Python datetime hydration discards sub-microsecond precision"
    );
    assert_eq!(expired["execution"], "running");
    expiry["expires_at"] = "2030-01-01T12:00:00.000001001Z".into();
    state.approvals[0] = dependency(Dependency::Approval, &expiry);
    assert_eq!(
        project(
            &state,
            now() + Duration::nanoseconds(1),
            ConnectionState::Unknown
        )
        .unwrap()["pending"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        project(
            &state,
            now() + Duration::microseconds(1),
            ConnectionState::Unknown
        )
        .unwrap()["pending"],
        json!([])
    );
    expiry["expires_at"] = "2030-01-01T12:00:00".into();
    state.approvals[0] = dependency(Dependency::Approval, &expiry);
    assert_eq!(progress_requests(&state, now()), Err(StateError::Invalid));
    assert_eq!(
        project(&state, now(), ConnectionState::Unknown),
        Err(StateError::Invalid)
    );
    expiry["status"] = "approved".into();
    state.approvals[0] = dependency(Dependency::Approval, &expiry);
    assert!(
        project(&state, now(), ConnectionState::Unknown).is_ok(),
        "A decided approval never compares its naive expiry"
    );

    let mut progress = inputs("continuation-delivered-not_required");
    let requests = progress_requests(&progress, now()).unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].after_sequence, 10);
    assert_eq!(
        project(&progress, now(), ConnectionState::Unknown).unwrap()["execution"],
        "continuing"
    );
    progress
        .progress
        .insert(requests[0].approval_id.clone(), Some(11));
    let observed = project(&progress, now(), ConnectionState::Unknown).unwrap();
    assert_eq!(observed["execution"], "running");
    assert_eq!(observed["decisions"][0]["progress_sequence"], 11);
    let mut approval = progress.approvals[0].payload().clone();
    approval["continuation"]["progress_after_sequence"] =
        serde_json::from_str("9223372036854775808").unwrap();
    progress.approvals[0] = dependency(Dependency::Approval, &approval);
    assert_eq!(
        progress_requests(&progress, now()),
        Err(StateError::Invalid)
    );
    approval["continuation"]["adapter_status"] = "unknown".into();
    progress.approvals[0] = dependency(Dependency::Approval, &approval);
    assert!(progress_requests(&progress, now()).unwrap().is_empty());
    assert_eq!(
        project(&progress, now(), ConnectionState::Unknown).unwrap()["execution"],
        "interrupted"
    );

    let mut pending = inputs("pending");
    let mut question = pending
        .questions
        .iter()
        .find(|q| q.payload()["id"] == "pending-question")
        .unwrap()
        .payload()
        .clone();
    question["prompt"] = "x".repeat(4000).into();
    question["contains_secret"] = false.into();
    pending.questions = vec![dependency(Dependency::HarnessInteraction, &question); 5000];
    assert_eq!(
        project(&pending, now(), ConnectionState::Unknown),
        Err(StateError::TooLarge),
        "Expanded pending text is bounded before collecting a partial response"
    );
    let mut many = inputs("sequence");
    many.turns = vec![many.turns[0].clone(); 10_000];
    assert_eq!(
        project(&many, now(), ConnectionState::Unknown),
        Err(StateError::TooLarge)
    );
}
