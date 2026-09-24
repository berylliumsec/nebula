#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{
    Config, Error, SqliteAssistantStore, StateClock, execution::*,
};
use serde_json::{Value, json};
use sqlx::{Connection, Row, SqliteConnection};
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-execution.json")).unwrap()
}
fn now() -> DateTime<Utc> {
    "2030-01-01T12:00:00Z".parse().unwrap()
}
fn clock() -> Arc<StateClock> {
    Arc::new(now)
}
fn counted() -> (Arc<StateClock>, Arc<AtomicUsize>) {
    let calls = Arc::new(AtomicUsize::new(0));
    let c = calls.clone();
    (
        Arc::new(move || {
            c.fetch_add(1, Ordering::SeqCst);
            now()
        }),
        calls,
    )
}
fn revision(record: &ExecutionRecord) -> i64 {
    record.payload()["revision"].as_i64().unwrap()
}
fn id(record: &ExecutionRecord) -> &str {
    record.payload()["id"].as_str().unwrap()
}
fn record(kind: Kind, payload: Value) -> ExecutionRecord {
    let raw = payload.to_string();
    let record = if kind == Kind::Turn {
        StoredAssistantRecord::decode_persisted(kind, raw.as_bytes())
    } else {
        StoredAssistantRecord::decode_fork_persisted_direct(kind, raw.as_bytes())
    }
    .unwrap();
    ExecutionRecord::new(record, &raw).unwrap()
}
fn from_envelope(envelope: &Value) -> ExecutionRecord {
    record(
        Kind::try_from(envelope["kind"].as_str().unwrap()).unwrap(),
        envelope["payload"].clone(),
    )
}
fn phase<'a>(case: &'a Value, name: &str) -> &'a Value {
    case["expected_commit_phases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["phase"] == name)
        .unwrap()
}
fn after(phase: &Value, kind: Kind) -> Value {
    phase["changes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["after"]["kind"] == kind.as_str())
        .unwrap()["after"]
        .clone()
}
fn case(name: &str) -> Value {
    fixture()["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == name)
        .unwrap()
        .clone()
}
fn admission(case: &Value) -> TextAdmission {
    let p = phase(case, "admission");
    let session = after(p, Kind::Session);
    let session_change = p["changes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["after"]["kind"] == Kind::Session.as_str())
        .unwrap();
    let source = if session_change["before"].is_null() {
        {
            let mut p = session["payload"].clone();
            p["metadata"]
                .as_object_mut()
                .unwrap()
                .remove("message_count");
            p["metadata"]
                .as_object_mut()
                .unwrap()
                .remove("last_sequence");
            AdmissionSession::New(record(Kind::Session, p))
        }
    } else {
        AdmissionSession::Existing {
            id: session["payload"]["id"].as_str().unwrap().into(),
            expected_revision: session_change["before"]["payload"]["revision"]
                .as_i64()
                .unwrap(),
        }
    };
    let messages = p["changes"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["after"]["kind"] == Kind::Message.as_str())
        .map(|c| from_envelope(&c["after"]))
        .collect();
    TextAdmission {
        session: source,
        turn: from_envelope(&after(p, Kind::Turn)),
        messages,
        settings: TextSettings::default(),
        provider_profile_id: session["payload"]["provider_profile_id"]
            .as_str()
            .unwrap()
            .into(),
        resolved_model: session["payload"]["model"].as_str().unwrap().into(),
    }
}
fn renamed(mut input: TextAdmission, suffix: &str) -> TextAdmission {
    let old_session = input.turn.payload()["session_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let sid = format!("{old_session}-{suffix}");
    if let AdmissionSession::New(s) = input.session {
        let mut p = s.payload().clone();
        p["id"] = sid.clone().into();
        input.session = AdmissionSession::New(record(Kind::Session, p));
    }
    let mut p = input.turn.payload().clone();
    p["id"] = format!("{}-{suffix}", id(&input.turn)).into();
    p["session_id"] = sid.clone().into();
    input.turn = record(Kind::Turn, p);
    input.messages = input
        .messages
        .into_iter()
        .map(|m| {
            let mut p = m.payload().clone();
            p["id"] = format!("{}-{suffix}", id(&m)).into();
            p["session_id"] = sid.clone().into();
            record(Kind::Message, p)
        })
        .collect();
    input
}
async fn seed(path: &std::path::Path) {
    support::database(path).await;
    let mut db = support::raw(path).await;
    sqlx::query("DELETE FROM entities")
        .execute(&mut db)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut db)
        .await
        .unwrap();
    let f = fixture();
    for p in f["initial_entity_rows"].as_array().unwrap() {
        sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)").bind(p["id"].as_str()).bind(p["kind"].as_str()).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64()).bind(p["payload"].as_str()).bind(p["chat_session_id"].as_str()).bind(p["created_at"].as_str()).bind(p["updated_at"].as_str()).execute(&mut db).await.unwrap();
    }
    for p in f["initial_search_documents"].as_array().unwrap() {
        sqlx::query("INSERT INTO search_documents(id,project_id,resource_kind,resource_id,revision,label,description,breadcrumb,content,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)").bind(p["id"].as_str()).bind(p["project_id"].as_str()).bind(p["resource_kind"].as_str()).bind(p["resource_id"].as_str()).bind(p["revision"].as_i64()).bind(p["label"].as_str()).bind(p["description"].as_str()).bind(p["breadcrumb"].as_str()).bind(p["content"].as_str()).bind(p["updated_at"].as_str()).execute(&mut db).await.unwrap();
    }
    db.close().await.unwrap();
}
async fn rows(db: &mut SqliteConnection) -> BTreeMap<String, (String, i64, String)> {
    sqlx::query("SELECT id,kind,revision,payload FROM entities ORDER BY id")
        .fetch_all(db)
        .await
        .unwrap()
        .into_iter()
        .map(|r| {
            (
                r.get("id"),
                (r.get("kind"), r.get("revision"), r.get("payload")),
            )
        })
        .collect()
}
async fn assert_phase(
    db: &mut SqliteConnection,
    before: &BTreeMap<String, (String, i64, String)>,
    phase: &Value,
) {
    let actual = rows(db).await;
    let mut expected = before.clone();
    for change in phase["changes"].as_array().unwrap() {
        let p = &change["after"]["payload"];
        assert!(!p.is_null());
        expected.insert(
            p["id"].as_str().unwrap().into(),
            (
                change["after"]["kind"].as_str().unwrap().into(),
                p["revision"].as_i64().unwrap(),
                p.to_string(),
            ),
        );
    }
    assert_eq!(
        actual.keys().collect::<Vec<_>>(),
        expected.keys().collect::<Vec<_>>()
    );
    for (id, (kind, revision, raw)) in &actual {
        let e = &expected[id];
        assert_eq!((kind, revision), (&e.0, &e.1), "{id}");
        assert_eq!(
            serde_json::from_str::<Value>(raw).unwrap(),
            serde_json::from_str::<Value>(&e.2).unwrap(),
            "{id} phase {}",
            phase["phase"]
        );
        if !phase["changes"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["after"]["payload"]["id"] == id.as_str())
        {
            assert_eq!(raw, &e.2, "unrelated raw row {id}");
        }
    }
    for change in phase["search_changes"].as_array().unwrap() {
        let p = &change["after"];
        if p.is_null() {
            continue;
        }
        let row = sqlx::query("SELECT * FROM search_documents WHERE id=?")
            .bind(p["id"].as_str())
            .fetch_one(&mut *db)
            .await
            .unwrap();
        for field in [
            "id",
            "project_id",
            "resource_kind",
            "resource_id",
            "label",
            "description",
            "breadcrumb",
            "content",
            "updated_at",
        ] {
            assert_eq!(
                row.try_get::<Option<String>, _>(field).unwrap().as_deref(),
                p[field].as_str(),
                "search {field}"
            );
        }
        assert_eq!(
            row.get::<i64, _>("revision"),
            p["revision"].as_i64().unwrap()
        );
    }
}
fn claim_for(turn: &ExecutionRecord) -> (Claim, ExecutionFence) {
    let claim = Claim {
        worker_id: "worker".into(),
        claim_id: format!("claim-{}", id(turn)),
        claimed_at: now(),
    };
    let fence = ExecutionFence {
        turn_id: id(turn).into(),
        worker_id: claim.worker_id.clone(),
        claim_id: claim.claim_id.clone(),
    };
    (claim, fence)
}
fn answer_for(turn: &ExecutionRecord, session: &ExecutionRecord, suffix: &str) -> ExecutionRecord {
    let mut p =
        after(phase(&case("new-greeting-stream"), "answer"), Kind::Message)["payload"].clone();
    p["id"] = format!("answer-{suffix}").into();
    p["session_id"] = session.payload()["id"].clone();
    p["engagement_id"] = session.payload()["engagement_id"].clone();
    p["sequence"] = json!(
        session.payload()["metadata"]["last_sequence"]
            .as_u64()
            .unwrap()
            + 1
    );
    p["metadata"]["chat_turn_id"] = turn.payload()["id"].clone();
    record(Kind::Message, p)
}

#[tokio::test]
async fn python_execution_commit_phases_match_source_and_survive_each_reopen() {
    for name in ["new-greeting-stream", "existing-stream"] {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("nebula.db");
        seed(&path).await;
        let c = case(name);
        let mut store = SqliteAssistantStore::open(&path, Config::default())
            .await
            .unwrap();
        let mut db = support::raw(&path).await;
        let mut before = rows(&mut db).await;
        let (clock, calls) = counted();
        let admitted = store
            .admit_text(admission(&c), clock.clone())
            .await
            .unwrap();
        assert_phase(&mut db, &before, phase(&c, "admission")).await;
        let sid = id(&admitted.session).to_owned();
        let tid = id(&admitted.turn).to_owned();
        let mut turn = admitted.turn;
        let mut session = admitted.session;
        let expected_claim = after(phase(&c, "claim"), Kind::Turn);
        let cp = &expected_claim["payload"];
        let claim = Claim {
            worker_id: cp["execution_owner_id"].as_str().unwrap().into(),
            claim_id: cp["execution_claim_id"].as_str().unwrap().into(),
            claimed_at: cp["execution_claimed_at"]
                .as_str()
                .unwrap()
                .parse()
                .unwrap(),
        };
        let fence = ExecutionFence {
            turn_id: tid.clone(),
            worker_id: claim.worker_id.clone(),
            claim_id: claim.claim_id.clone(),
        };
        for phase_name in ["claim", "answer", "complete", "release"] {
            store.shutdown().await.unwrap();
            store = SqliteAssistantStore::open(&path, Config::default())
                .await
                .unwrap();
            before = rows(&mut db).await;
            match phase_name {
                "claim" => {
                    turn = store
                        .claim_text(&tid, revision(&turn), claim.clone(), clock.clone())
                        .await
                        .unwrap();
                }
                "answer" => {
                    let a = store
                        .append_text_answer(
                            fence.clone(),
                            revision(&turn),
                            revision(&session),
                            from_envelope(&after(phase(&c, "answer"), Kind::Message)),
                            clock.clone(),
                        )
                        .await
                        .unwrap();
                    turn = a.turn;
                    session = a.session;
                }
                "complete" => {
                    let answer = after(phase(&c, "answer"), Kind::Message);
                    turn = store
                        .complete_text(
                            fence.clone(),
                            revision(&turn),
                            answer["payload"]["id"].as_str().unwrap(),
                            answer["payload"]["usage"].clone(),
                            clock.clone(),
                        )
                        .await
                        .unwrap();
                }
                "release" => {
                    let ReleaseOutcome::Released(t) = store
                        .release_text(fence.clone(), revision(&turn), clock.clone())
                        .await
                        .unwrap()
                    else {
                        panic!("owner lost")
                    };
                    turn = t;
                }
                _ => unreachable!(),
            }
            assert_phase(&mut db, &before, phase(&c, phase_name)).await;
        }
        assert_eq!(
            calls.load(Ordering::SeqCst),
            if name == "existing-stream" { 6 } else { 5 }
        );
        assert!(matches!(
            store.text_recovery(&tid).await.unwrap().state,
            RecoveryState::Complete {
                claim_retained: false,
                ..
            }
        ));
        assert_eq!(
            id(&store.execution_record(Kind::Session, &sid).await.unwrap()),
            sid
        );
        store.shutdown().await.unwrap();
    }
}

#[tokio::test]
async fn execution_admission_and_claims_are_atomic_under_contention() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let mut db = support::raw(&path).await;
    let mut colliding = renamed(admission(&case("new-greeting-stream")), "collision");
    let mut p = colliding.messages[0].payload().clone();
    p["id"] = "existing".into();
    colliding.messages[0] = record(Kind::Message, p);
    let before = rows(&mut db).await;
    assert!(matches!(
        store.admit_text(colliding, clock()).await,
        Err(Error::AlreadyExists(_))
    ));
    assert_eq!(
        rows(&mut db).await,
        before,
        "late insert collision rolls back Session and search"
    );
    let a = admission(&case("existing-stream"));
    let mut b = a.clone();
    let mut p = b.turn.payload().clone();
    p["id"] = "competing-turn".into();
    b.turn = record(Kind::Turn, p);
    let mut p = b.messages[0].payload().clone();
    p["id"] = "competing-message".into();
    b.messages[0] = record(Kind::Message, p);
    let (left, right) = tokio::join!(store.admit_text(a, clock()), store.admit_text(b, clock()));
    assert_ne!(left.is_ok(), right.is_ok());
    let committed = left.or(right).unwrap();
    assert_eq!(
        store
            .text_snapshot("existing")
            .await
            .unwrap()
            .unfinished_turns
            .len(),
        1
    );
    let (first, fence) = claim_for(&committed.turn);
    let second = Claim {
        worker_id: "other-worker".into(),
        claim_id: "other-claim".into(),
        claimed_at: now(),
    };
    let (left, right) = tokio::join!(
        store.claim_text(id(&committed.turn), 1, first, clock()),
        store.claim_text(id(&committed.turn), 1, second, clock())
    );
    assert_ne!(left.is_ok(), right.is_ok());
    let winner = left.or(right).unwrap();
    let actual_fence = ExecutionFence {
        turn_id: id(&winner).into(),
        worker_id: winner.payload()["execution_owner_id"]
            .as_str()
            .unwrap()
            .into(),
        claim_id: winner.payload()["execution_claim_id"]
            .as_str()
            .unwrap()
            .into(),
    };
    let stale = ExecutionFence {
        worker_id: "stale".into(),
        ..fence
    };
    let (clock, calls) = counted();
    assert!(matches!(
        store
            .release_text(stale.clone(), revision(&winner), clock.clone())
            .await
            .unwrap(),
        ReleaseOutcome::NoLongerOwner(_)
    ));
    assert!(matches!(
        store
            .append_text_answer(
                stale,
                revision(&winner),
                revision(&committed.session),
                answer_for(&winner, &committed.session, "stale"),
                clock.clone()
            )
            .await,
        Err(Error::ExecutionConflict(_))
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    let stopped = store
        .settle_text(
            id(&winner),
            revision(&winner),
            TerminalAction::Stop,
            clock.clone(),
        )
        .await
        .unwrap();
    assert_eq!(stopped.payload()["status"], "cancelled");
    assert!(matches!(
        store
            .append_text_answer(
                actual_fence,
                revision(&winner),
                revision(&committed.session),
                answer_for(&winner, &committed.session, "late"),
                clock.clone()
            )
            .await,
        Err(Error::ExecutionConflict(_))
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn execution_recovery_preserves_saved_answers_and_newer_operator_settings() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let mut store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let input = renamed(admission(&case("new-greeting-stream")), "recovery");
    let admitted = store.admit_text(input, clock()).await.unwrap();
    let tid = id(&admitted.turn).to_owned();
    let sid = id(&admitted.session).to_owned();
    assert_eq!(
        store.text_recovery(&tid).await.unwrap().state,
        RecoveryState::Unclaimed
    );
    store.shutdown().await.unwrap();
    store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let (claim, fence) = claim_for(&admitted.turn);
    let claimed = store.claim_text(&tid, 1, claim, clock()).await.unwrap();
    assert_eq!(
        store.text_recovery(&tid).await.unwrap().state,
        RecoveryState::ClaimedUncertain
    );
    let initial = store.settings_session(&sid).await.unwrap();
    let mut metadata = initial.record.payload()["metadata"].clone();
    metadata["reasoning_effort"] = "high".into();
    metadata["opaque"] = json!({"kept":[true,1,"value"]});
    store
        .patch_session_settings(
            &sid,
            revision(&admitted.session).to_string(),
            json!({"title":"Changed while running","metadata":metadata})
                .as_object()
                .unwrap()
                .clone(),
            initial.raw_payload,
            clock(),
        )
        .await
        .unwrap();
    let session = store.execution_record(Kind::Session, &sid).await.unwrap();
    let answer = answer_for(&claimed, &session, "recovery");
    let saved = store
        .append_text_answer(
            fence.clone(),
            revision(&claimed),
            revision(&session),
            answer.clone(),
            clock(),
        )
        .await
        .unwrap();
    assert_eq!(saved.session.payload()["title"], "Changed while running");
    assert_eq!(
        saved.session.payload()["metadata"]["reasoning_effort"],
        "high"
    );
    assert_eq!(
        saved.session.payload()["metadata"]["opaque"],
        metadata["opaque"]
    );
    store.shutdown().await.unwrap();
    store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let recovery = store.text_recovery(&tid).await.unwrap();
    assert_eq!(
        recovery.state,
        RecoveryState::SavedAnswer {
            message_id: id(&answer).into()
        }
    );
    assert_eq!(recovery.answer.unwrap().payload(), answer.payload());
    let (clock, calls) = counted();
    assert!(matches!(
        store
            .append_text_answer(
                fence.clone(),
                revision(&saved.turn),
                revision(&saved.session),
                answer_for(&saved.turn, &saved.session, "duplicate"),
                clock.clone()
            )
            .await,
        Err(Error::ExecutionUncertain(_))
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    let mut bad_usage = answer.payload()["usage"].clone();
    bad_usage["total_tokens"] = 123.into();
    assert!(matches!(
        store
            .complete_text(
                fence.clone(),
                revision(&saved.turn),
                id(&answer),
                bad_usage,
                clock.clone()
            )
            .await,
        Err(Error::ExecutionUncertain(_))
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    let complete = store
        .complete_text(
            fence.clone(),
            revision(&saved.turn),
            id(&answer),
            answer.payload()["usage"].clone(),
            clock.clone(),
        )
        .await
        .unwrap();
    assert!(matches!(
        store.text_recovery(&tid).await.unwrap().state,
        RecoveryState::Complete {
            claim_retained: true,
            ..
        }
    ));
    store.shutdown().await.unwrap();
    store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let no_op = store
        .complete_text(
            fence.clone(),
            1,
            id(&answer),
            answer.payload()["usage"].clone(),
            clock.clone(),
        )
        .await
        .unwrap();
    assert_eq!(revision(&complete), revision(&no_op));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(matches!(
        store
            .release_text(fence.clone(), revision(&complete), clock.clone())
            .await
            .unwrap(),
        ReleaseOutcome::Released(_)
    ));
    assert!(matches!(
        store.release_text(fence, 1, clock.clone()).await.unwrap(),
        ReleaseOutcome::NoLongerOwner(_)
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    // An interrupted claim is evidence to show the operator, not permission to
    // invoke a model. The writer only changes retained state.
    let other = store
        .admit_text(
            renamed(admission(&case("new-greeting-stream")), "interrupted"),
            clock.clone(),
        )
        .await
        .unwrap();
    let (claim, fence) = claim_for(&other.turn);
    let claimed = store
        .claim_text(id(&other.turn), 1, claim, clock.clone())
        .await
        .unwrap();
    let interrupted = store
        .settle_text(
            id(&claimed),
            revision(&claimed),
            TerminalAction::Interrupt {
                fence: Some(fence),
                cause: InterruptCause::Restart,
                observed_at: now(),
            },
            clock.clone(),
        )
        .await
        .unwrap();
    assert_eq!(
        interrupted.payload()["request_snapshot"]["recovery"]["cause"],
        "core_restart"
    );
    assert!(interrupted.payload()["execution_claim_id"].is_null());
    assert_eq!(
        store.text_recovery(id(&interrupted)).await.unwrap().state,
        RecoveryState::Interrupted
    );
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn execution_terminal_notes_are_atomic_and_never_duplicate_retained_answers() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let admitted = store
        .admit_text(
            renamed(admission(&case("new-greeting-stream")), "outcome"),
            clock(),
        )
        .await
        .unwrap();
    let (claim, fence) = claim_for(&admitted.turn);
    let claimed = store
        .claim_text(id(&admitted.turn), 1, claim, clock())
        .await
        .unwrap();
    let failed = store
        .settle_text(
            id(&claimed),
            revision(&claimed),
            TerminalAction::Fail {
                fence: fence.clone(),
                error: "fixture provider failure".into(),
            },
            clock(),
        )
        .await
        .unwrap();
    assert_eq!(failed.payload()["execution_claim_id"], fence.claim_id);
    let ReleaseOutcome::Released(failed) = store
        .release_text(fence, revision(&failed), clock())
        .await
        .unwrap()
    else {
        panic!("matching release")
    };
    let mut p = answer_for(&failed, &admitted.session, "outcome")
        .payload()
        .clone();
    p["finish_reason"] = "interrupted".into();
    p["metadata"] = json!({"kind":"turn_outcome","chat_turn_id":id(&failed),"turn_status":"failed","interrupted":true,"tool_call_ids":[],"tool_results":[]});
    let note = record(Kind::Message, p);
    let (clock, calls) = counted();
    let result = store
        .append_text_outcome(
            id(&failed),
            revision(&failed),
            revision(&admitted.session),
            note.clone(),
            clock.clone(),
        )
        .await
        .unwrap();
    let OutcomeCommit::Written { session, .. } = result else {
        panic!("first note")
    };
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(matches!(
        store
            .append_text_outcome(
                id(&failed),
                revision(&failed),
                1,
                note.clone(),
                clock.clone()
            )
            .await
            .unwrap(),
        OutcomeCommit::AlreadyRecorded
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    // Replaced history remains authoritative for deduplication, exactly as the
    // source include_replaced=True outcome lookup.
    let mut db = support::raw(&path).await;
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.metadata.retracted_at','replaced') WHERE id=?")
        .bind(id(&note)).execute(&mut db).await.unwrap();
    assert!(matches!(
        store
            .append_text_outcome(
                id(&failed),
                revision(&failed),
                revision(&session),
                note,
                clock.clone()
            )
            .await
            .unwrap(),
        OutcomeCommit::AlreadyRecorded
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let stopped = store
        .settle_text(
            id(&failed),
            revision(&failed),
            TerminalAction::Stop,
            clock.clone(),
        )
        .await
        .unwrap();
    assert_eq!(stopped.payload()["status"], "cancelled");
    let stopped_again = store
        .settle_text(id(&failed), 1, TerminalAction::Stop, clock.clone())
        .await
        .unwrap();
    assert_eq!(revision(&stopped), revision(&stopped_again));
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    store.shutdown().await.unwrap();
}

async fn until(mut predicate: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !predicate() {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
}
#[tokio::test]
async fn execution_limits_and_cancelled_admission_preserve_acknowledged_state() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let config = Config {
        writer_capacity: 1,
        ..Config::default()
    };
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    let mut many = renamed(admission(&case("new-greeting-stream")), "many");
    let template = many.messages[0].payload().clone();
    many.messages = (1..=200)
        .map(|sequence| {
            let mut p = template.clone();
            p["id"] = format!("batch-{sequence}").into();
            p["sequence"] = sequence.into();
            record(Kind::Message, p)
        })
        .collect();
    let mut oversized = many.clone();
    let mut p = template.clone();
    p["id"] = "batch-201".into();
    p["sequence"] = 201.into();
    oversized.messages.push(record(Kind::Message, p));
    assert!(matches!(
        store.admit_text(oversized, clock()).await,
        Err(Error::TransactionLimit)
    ));
    let accepted = store.admit_text(many, clock()).await.unwrap();
    assert_eq!(accepted.messages.len(), 200);
    assert_eq!(accepted.session.payload()["metadata"]["last_sequence"], 200);
    let mut unsupported = renamed(admission(&case("new-greeting-stream")), "unsupported");
    let mut p = unsupported.turn.payload().clone();
    p["request_snapshot"]["model_request"]["tools"] = json!([{"name":"not-supported"}]);
    unsupported.turn = record(Kind::Turn, p);
    assert!(matches!(
        store.admit_text(unsupported, clock()).await,
        Err(Error::ExecutionUnsupported(_))
    ));
    let mut db = support::raw(&path).await;
    let lock = db.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let input = renamed(admission(&case("new-greeting-stream")), "cancelled-caller");
    let tid = id(&input.turn).to_owned();
    let first = {
        let store = store.clone();
        tokio::spawn(async move { store.admit_text(input, clock()).await })
    };
    until(|| {
        store.admission().available_bytes < config.queued_bytes
            && store.admission().available_queue_entries == 1
    })
    .await;
    let input2 = renamed(admission(&case("new-greeting-stream")), "queued-caller");
    let tid2 = id(&input2.turn).to_owned();
    let second = {
        let store = store.clone();
        tokio::spawn(async move { store.admit_text(input2, clock()).await })
    };
    until(|| store.admission().available_queue_entries == 0).await;
    assert!(matches!(
        store
            .admit_text(
                renamed(admission(&case("new-greeting-stream")), "full"),
                clock()
            )
            .await,
        Err(Error::Capacity)
    ));
    first.abort();
    second.abort();
    assert!(first.await.unwrap_err().is_cancelled());
    assert!(second.await.unwrap_err().is_cancelled());
    lock.commit().await.unwrap();
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        reopened.text_recovery(&tid).await.unwrap().state,
        RecoveryState::Unclaimed
    );
    assert_eq!(
        reopened.text_recovery(&tid2).await.unwrap().state,
        RecoveryState::Unclaimed
    );
    reopened.shutdown().await.unwrap();
}

#[tokio::test]
async fn execution_result_leases_bound_replies_reads_and_capacity_rollback() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let mut input = renamed(admission(&case("new-greeting-stream")), "leased");
    let mut p = input.turn.payload().clone();
    p["request_snapshot"]["opaque"] = "x".repeat(32 * 1024).into();
    input.turn = record(Kind::Turn, p);
    let admitted = store.admit_text(input, clock()).await.unwrap();
    let tid = id(&admitted.turn).to_owned();
    let sid = id(&admitted.session).to_owned();
    let turn_bytes = serde_json::to_vec(admitted.turn.payload()).unwrap().len()
        + admitted.turn.raw_payload().len();
    drop(admitted);
    store.shutdown().await.unwrap();
    let config = Config {
        execution_result_bytes: 2 * turn_bytes,
        ..Config::default()
    };
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    let first = store.execution_record(Kind::Turn, &tid).await.unwrap();
    assert_eq!(
        store.admission().available_execution_result_bytes,
        turn_bytes
    );
    let clones: Vec<_> = (0..64).map(|_| first.clone()).collect();
    assert!(std::ptr::eq(first.payload(), clones[0].payload()));
    assert!(std::ptr::eq(
        first.raw_payload().as_ptr(),
        clones[0].raw_payload().as_ptr()
    ));
    assert_eq!(
        store.admission().available_execution_result_bytes,
        turn_bytes
    );
    let second = store.execution_record(Kind::Turn, &tid).await.unwrap();
    assert_eq!(store.admission().available_execution_result_bytes, 0);
    assert!(matches!(
        store.execution_record(Kind::Turn, &tid).await,
        Err(Error::ExecutionResultCapacity)
    ));
    assert!(matches!(
        store.text_recovery(&tid).await,
        Err(Error::ExecutionResultCapacity)
    ));
    assert!(matches!(
        store.text_snapshot(&sid).await,
        Err(Error::ExecutionResultCapacity)
    ));
    assert_eq!(store.admission().available_reads, config.read_capacity);
    let mut db = support::raw(&path).await;
    let before = rows(&mut db).await;
    let (claim, fence) = claim_for(&first);
    assert!(matches!(
        store.claim_text(&tid, 1, claim.clone(), clock()).await,
        Err(Error::ExecutionResultCapacity)
    ));
    assert_eq!(
        rows(&mut db).await,
        before,
        "failed result reservation rolls back tentative claim and revision"
    );
    drop(second);
    drop(first);
    assert_eq!(
        store.admission().available_execution_result_bytes,
        turn_bytes,
        "clones retain the original lease"
    );
    drop(clones);
    assert_eq!(
        store.admission().available_execution_result_bytes,
        config.execution_result_bytes
    );
    let recovery = store.text_recovery(&tid).await.unwrap();
    let available = store.admission().available_execution_result_bytes;
    assert!(available < turn_bytes);
    let retained_turn = recovery.turn.clone();
    drop(recovery);
    assert_eq!(
        store.admission().available_execution_result_bytes,
        available,
        "a grouped result retains its complete lease until the final member drops"
    );
    drop(retained_turn);
    assert_eq!(
        store.admission().available_execution_result_bytes,
        config.execution_result_bytes
    );
    let snapshot = store.text_snapshot(&sid).await.unwrap();
    assert!(!snapshot.messages.is_empty());
    assert!(store.admission().available_execution_result_bytes < turn_bytes);
    drop(snapshot);
    assert_eq!(
        store.admission().available_execution_result_bytes,
        config.execution_result_bytes
    );
    // The spawned call can finish before its consumer observes the reply. Its
    // retained data still owns credits after queued-write/read permits return.
    let completed = {
        let store = store.clone();
        let tid = tid.clone();
        tokio::spawn(async move { store.claim_text(&tid, 1, claim, clock()).await })
    };
    until(|| completed.is_finished() && store.admission().available_bytes == config.queued_bytes)
        .await;
    assert!(store.admission().available_execution_result_bytes < turn_bytes);
    let claimed = completed.await.unwrap().unwrap();
    let charged =
        config.execution_result_bytes - store.admission().available_execution_result_bytes;
    let foreign = ExecutionFence {
        worker_id: "other-worker".into(),
        ..fence
    };
    assert!(
        matches!(
            store
                .release_text(foreign, revision(&claimed), clock())
                .await,
            Err(Error::ExecutionResultCapacity)
        ),
        "even a no-op reply cannot escape the aggregate result budget"
    );
    let mut another = renamed(admission(&case("new-greeting-stream")), "reply-full");
    let mut p = another.turn.payload().clone();
    p["request_snapshot"]["opaque"] = "y".repeat(32 * 1024).into();
    another.turn = record(Kind::Turn, p);
    let before = rows(&mut db).await;
    let search_before: Vec<(String, i64)> =
        sqlx::query_as("SELECT id,revision FROM search_documents ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap();
    assert!(matches!(
        store.admit_text(another, clock()).await,
        Err(Error::ExecutionResultCapacity)
    ));
    assert_eq!(rows(&mut db).await, before);
    let search_after: Vec<(String, i64)> =
        sqlx::query_as("SELECT id,revision FROM search_documents ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap();
    assert_eq!(
        search_after, search_before,
        "capacity refusal leaves no new Session search document"
    );
    let clone = claimed.clone();
    drop(claimed);
    assert_eq!(
        store.admission().available_execution_result_bytes,
        config.execution_result_bytes - charged
    );
    drop(clone);
    assert_eq!(
        store.admission().available_execution_result_bytes,
        config.execution_result_bytes
    );
    store.shutdown().await.unwrap();
    for bytes in [0, 256 * 1024 * 1024 + 1] {
        assert!(matches!(
            SqliteAssistantStore::open(
                &path,
                Config {
                    execution_result_bytes: bytes,
                    ..Config::default()
                }
            )
            .await,
            Err(Error::InvalidBounds)
        ));
    }
}

#[tokio::test]
async fn execution_turn_reads_hydrate_historical_fields_and_preserve_error_boundaries() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let admitted = store
        .admit_text(
            renamed(admission(&case("new-greeting-stream")), "legacy"),
            clock(),
        )
        .await
        .unwrap();
    let turn_id = id(&admitted.turn).to_owned();
    let canonical = admitted.turn.payload().clone();
    let mut legacy = canonical.clone();
    legacy["revision"] = "1".into();
    legacy["tools_enabled"] = "false".into();
    legacy["next_step"] = "0".into();
    legacy["provider_profile_id"] =
        format!(" {} ", legacy["provider_profile_id"].as_str().unwrap()).into();
    legacy["usage"] = json!({"input_tokens":"0", "output_tokens":false, "total_tokens":"0"});
    let mut db = support::raw(&path).await;
    sqlx::query("UPDATE entities SET payload=? WHERE id=?")
        .bind(legacy.to_string())
        .bind(&turn_id)
        .execute(&mut db)
        .await
        .unwrap();
    let read = store.execution_record(Kind::Turn, &turn_id).await.unwrap();
    assert_eq!(read.payload(), &canonical);
    assert_eq!(
        serde_json::from_str::<Value>(read.raw_payload()).unwrap(),
        canonical
    );
    let (claim, _) = claim_for(&read);
    let claimed = store.claim_text(&turn_id, 1, claim, clock()).await.unwrap();
    assert_eq!(revision(&claimed), 2);
    assert_eq!(claimed.payload()["tools_enabled"], false);
    assert_eq!(claimed.payload()["next_step"], 0);

    let mut corrupt = claimed.payload().clone();
    corrupt["status"] = "invalid".into();
    sqlx::query("UPDATE entities SET payload=? WHERE id=?")
        .bind(corrupt.to_string())
        .bind(&turn_id)
        .execute(&mut db)
        .await
        .unwrap();
    assert!(matches!(
        store.execution_record(Kind::Turn, &turn_id).await,
        Err(Error::WrappedRecord(
            nebula_assistant_domain::records::RecordError::ModelValidation(_)
        ))
    ));
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT payload FROM entities WHERE id=?")
            .bind(&turn_id)
            .fetch_one(&mut db)
            .await
            .unwrap(),
        corrupt.to_string()
    );
    store.shutdown().await.unwrap();
}
