#[allow(dead_code)]
#[path = "../../storage/tests/support/mod.rs"]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, RecordError, StoredAssistantRecord};
use nebula_assistant_runtime::{Work, admission::*};
use nebula_assistant_storage::entities::{
    Config, Error as StorageError, SqliteAssistantStore, StateClock, execution::*,
};
use serde_json::Value;
use sqlx::{Connection, Row};
use std::{sync::Arc, time::Duration};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-execution.json")).unwrap()
}
fn now() -> DateTime<Utc> {
    "2030-01-01T12:00:00Z".parse().unwrap()
}
fn clock() -> Arc<StateClock> {
    Arc::new(now)
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

fn work(input: &TextAdmission) -> Work {
    Work {
        id: input.turn.payload()["id"].as_str().unwrap().into(),
        project_id: input.turn.payload()["engagement_id"]
            .as_str()
            .unwrap()
            .into(),
        parent_group: input.turn.payload()["session_id"].as_str().unwrap().into(),
        session_id: input.turn.payload()["session_id"].as_str().unwrap().into(),
    }
}
fn submit(handle: &AdmissionHandle, input: TextAdmission) -> Result<AdmissionWaiter, SubmitError> {
    let work = work(&input);
    let bytes = admission_bytes(&input, &work).unwrap();
    let lease = handle.try_reserve_bytes(bytes)?;
    handle.try_submit(input, work, lease)
}
async fn count(path: &std::path::Path, id: &str) -> i64 {
    let mut db = support::raw(path).await;
    sqlx::query("SELECT count(*) AS n FROM entities WHERE id=?")
        .bind(id)
        .fetch_one(&mut db)
        .await
        .unwrap()
        .get("n")
}

#[tokio::test]
async fn caller_drop_keeps_owned_admission_and_shutdown_drains_commit() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let config = Config::default();
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    let limits = AdmissionLimits {
        error_work_bytes: ERROR_WORK_BYTES,
        ..AdmissionLimits::default()
    };
    let (handle, owner) = AdmissionController::start(store.clone(), clock(), limits).unwrap();
    let mut db = support::raw(&path).await;
    let lock = db.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let input = renamed(admission(&case("new-greeting-stream")), "owned");
    let identity = work(&input);
    let waiter = submit(&handle, input).unwrap();
    let second = renamed(admission(&case("new-greeting-stream")), "owned-second");
    let second_id = work(&second).id;
    let second_waiter = submit(&handle, second).unwrap();
    assert_eq!(handle.state(&identity.id), Some(AdmissionState::Reserved));
    assert_eq!(count(&path, &identity.id).await, 0);
    drop(waiter);
    drop(second_waiter);
    let mut drain = Box::pin(owner.shutdown());
    assert!(
        tokio::time::timeout(Duration::from_millis(25), &mut drain)
            .await
            .is_err()
    );
    assert!(handle.snapshot().closing);
    assert!(matches!(
        handle.try_reserve_bytes(1),
        Err(SubmitError::Closed)
    ));
    lock.commit().await.unwrap();
    let report = tokio::time::timeout(Duration::from_secs(5), drain)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        report,
        ShutdownReport {
            ready: 2,
            uncertain: 0
        }
    );
    assert_eq!(handle.state(&identity.id), Some(AdmissionState::Ready));
    assert_eq!(count(&path, &identity.id).await, 1);
    assert_eq!(count(&path, &second_id).await, 1);
    assert_eq!(
        store.admission().available_execution_result_bytes,
        config.execution_result_bytes
    );
    assert_eq!(
        handle.snapshot().available_context_bytes,
        limits.context_bytes
    );
    assert_eq!(handle.snapshot().available_reply_bytes, limits.reply_bytes);
    assert_eq!(
        handle.snapshot().available_error_work_bytes,
        ERROR_WORK_BYTES
    );
    store.shutdown().await.unwrap();
    let reopened = SqliteAssistantStore::open(&path, config).await.unwrap();
    assert_eq!(
        reopened.text_recovery(&identity.id).await.unwrap().state,
        RecoveryState::Unclaimed
    );
    assert_eq!(
        reopened.text_recovery(&second_id).await.unwrap().state,
        RecoveryState::Unclaimed
    );
    // Durable Unclaimed is a classification, never restart-dispatch permission.
    reopened.shutdown().await.unwrap();
}

#[tokio::test]
async fn confirmed_rejection_preserves_error_and_aborts_only_its_reservation() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let (handle, owner) =
        AdmissionController::start(store.clone(), clock(), AdmissionLimits::default()).unwrap();
    let good = renamed(
        admission(&case("new-greeting-stream")),
        "retry-after-rollback",
    );
    let mut invalid = good.clone();
    invalid.provider_profile_id = "wrong-profile".into();
    let identity = work(&invalid);
    let outcome = submit(&handle, invalid).unwrap().wait().await;
    assert_eq!(outcome.disposition, Disposition::RolledBack);
    let Err(AdmissionFailure::Storage(failure)) = outcome.result else {
        panic!("expected typed rollback")
    };
    assert!(matches!(failure.error(), StorageError::InvalidBounds));
    assert_eq!(handle.state(&identity.id), None);
    assert_eq!(count(&path, &identity.id).await, 0);
    drop(failure);
    let accepted = submit(&handle, good).unwrap().wait().await;
    assert_eq!(accepted.disposition, Disposition::Committed);
    assert_eq!(accepted.result.unwrap().turn_id, identity.id);
    assert_eq!(owner.shutdown().await.unwrap().ready, 1);
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn intake_pending_identity_and_ownership_limits_precede_persistence() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let limits = AdmissionLimits {
        intake: 1,
        jobs: 1,
        pending: 1,
        ..AdmissionLimits::default()
    };
    let (handle, owner) = AdmissionController::start(store.clone(), clock(), limits).unwrap();
    let (other, other_owner) = AdmissionController::start(store.clone(), clock(), limits).unwrap();
    let source = renamed(admission(&case("new-greeting-stream")), "capacity");
    let identity = work(&source);
    let bytes = admission_bytes(&source, &identity).unwrap();
    for field in ["id", "session", "project"] {
        let mut wrong = identity.clone();
        match field {
            "id" => wrong.id = "wrong".into(),
            "session" => wrong.session_id = "wrong".into(),
            _ => wrong.project_id = "wrong".into(),
        }
        assert!(matches!(
            handle.try_submit(
                source.clone(),
                wrong,
                handle.try_reserve_bytes(bytes).unwrap()
            ),
            Err(SubmitError::Identity)
        ));
    }
    assert!(matches!(
        handle.try_submit(
            source.clone(),
            identity.clone(),
            other.try_reserve_bytes(bytes).unwrap()
        ),
        Err(SubmitError::ForeignLease)
    ));
    assert!(matches!(
        handle.try_submit(
            source.clone(),
            identity.clone(),
            handle.try_reserve_bytes(bytes - 1).unwrap()
        ),
        Err(SubmitError::InputBytes)
    ));
    assert_eq!(handle.snapshot().outstanding, 0);
    assert_eq!(
        handle.snapshot().available_context_bytes,
        limits.context_bytes
    );
    let waiter = submit(&handle, source).unwrap();
    let next = renamed(admission(&case("new-greeting-stream")), "full");
    let next_id = work(&next).id;
    assert!(matches!(
        submit(&handle, next),
        Err(SubmitError::Queue(
            nebula_assistant_runtime::Error::Capacity
        ))
    ));
    assert_eq!(handle.state(&next_id), None);
    assert_eq!(waiter.wait().await.disposition, Disposition::Committed);
    assert_eq!(count(&path, &next_id).await, 0);
    owner.shutdown().await.unwrap();
    other_owner.shutdown().await.unwrap();
    // A full intake with spare FairQueue capacity must abort the failed enqueue.
    let (handle, owner) = AdmissionController::start(
        store.clone(),
        clock(),
        AdmissionLimits {
            intake: 1,
            jobs: 1,
            pending: 3,
            ..limits
        },
    )
    .unwrap();
    let first = submit(
        &handle,
        renamed(admission(&case("new-greeting-stream")), "intake-first"),
    )
    .unwrap();
    let retry = renamed(admission(&case("new-greeting-stream")), "intake-retry");
    let retry_id = work(&retry).id;
    assert!(matches!(
        submit(&handle, retry.clone()),
        Err(SubmitError::Capacity)
    ));
    assert_eq!(handle.state(&retry_id), None);
    assert_eq!(handle.snapshot().outstanding, 1);
    assert_eq!(first.wait().await.disposition, Disposition::Committed);
    assert_eq!(
        submit(&handle, retry).unwrap().wait().await.disposition,
        Disposition::Committed
    );
    owner.shutdown().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn uncertain_writer_outcome_retains_capacity_without_dispatch_or_reuse() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store.shutdown().await.unwrap();
    let limits = AdmissionLimits {
        pending: 1,
        ..AdmissionLimits::default()
    };
    let (handle, owner) = AdmissionController::start(store, clock(), limits).unwrap();
    let input = renamed(admission(&case("new-greeting-stream")), "unknown");
    let identity = work(&input);
    let outcome = submit(&handle, input.clone()).unwrap().wait().await;
    assert_eq!(outcome.disposition, Disposition::Unknown);
    let Err(AdmissionFailure::Storage(error)) = outcome.result else {
        panic!("expected closing error")
    };
    assert!(matches!(error.error(), StorageError::Closed));
    assert_eq!(handle.state(&identity.id), Some(AdmissionState::Uncertain));
    assert!(matches!(
        submit(&handle, input),
        Err(SubmitError::Queue(
            nebula_assistant_runtime::Error::InvalidTransition
        ))
    ));
    assert!(matches!(
        submit(
            &handle,
            renamed(admission(&case("new-greeting-stream")), "unknown-full")
        ),
        Err(SubmitError::Queue(
            nebula_assistant_runtime::Error::Capacity
        ))
    ));
    assert_eq!(
        owner.shutdown().await.unwrap(),
        ShutdownReport {
            ready: 0,
            uncertain: 1
        }
    );
    assert_eq!(count(&path, &identity.id).await, 0);
}

#[tokio::test]
async fn error_replies_retain_credits_and_oversized_reports_keep_rollback_disposition() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    seed(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let source = admission(&case("existing-stream"));
    let sid = work(&source).session_id;
    let mut db = support::raw(&path).await;
    let raw: String = sqlx::query("SELECT payload FROM entities WHERE id=?")
        .bind(&sid)
        .fetch_one(&mut db)
        .await
        .unwrap()
        .get("payload");
    let mut payload: Value = serde_json::from_str(&raw).unwrap();
    payload["private_extra"] = "do-not-print".repeat(1024).into();
    sqlx::query("UPDATE entities SET payload=? WHERE id=?")
        .bind(payload.to_string())
        .bind(&sid)
        .execute(&mut db)
        .await
        .unwrap();
    let limits = AdmissionLimits {
        reply_bytes: 65536,
        error_allowance: 65536,
        error_work_bytes: ERROR_WORK_BYTES,
        ..AdmissionLimits::default()
    };
    let (handle, owner) = AdmissionController::start(store.clone(), clock(), limits).unwrap();
    let outcome = submit(&handle, source.clone()).unwrap().wait().await;
    assert_eq!(outcome.disposition, Disposition::RolledBack);
    assert!(!format!("{outcome:?}").contains("do-not-print"));
    let Err(AdmissionFailure::Storage(failure)) = outcome.result else {
        panic!("expected retained report")
    };
    assert!(matches!(
        failure.error(),
        StorageError::WrappedRecord(RecordError::ModelValidation(_))
    ));
    let remaining = handle.snapshot().available_reply_bytes;
    assert!(remaining > 0 && remaining < limits.reply_bytes);
    assert!(matches!(
        submit(&handle, source.clone()),
        Err(SubmitError::Capacity)
    ));
    drop(failure);
    let resumed = tokio::time::timeout(
        Duration::from_secs(5),
        submit(&handle, source.clone()).unwrap().wait(),
    )
    .await
    .unwrap();
    assert_eq!(resumed.disposition, Disposition::RolledBack);
    drop(resumed);
    assert_eq!(handle.snapshot().available_reply_bytes, limits.reply_bytes);
    owner.shutdown().await.unwrap();
    // Holding a prior error must not block a later accepted job or shutdown:
    // materialization credit is independent from retained reply credit.
    let (handle, owner) = AdmissionController::start(
        store.clone(),
        clock(),
        AdmissionLimits {
            reply_bytes: 131072,
            ..limits
        },
    )
    .unwrap();
    let old_error = submit(&handle, source.clone()).unwrap().wait().await;
    assert_eq!(old_error.disposition, Disposition::RolledBack);
    let successful = submit(
        &handle,
        renamed(admission(&case("new-greeting-stream")), "held-error-drain"),
    )
    .unwrap();
    let report = tokio::time::timeout(Duration::from_secs(5), owner.shutdown())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        report,
        ShutdownReport {
            ready: 1,
            uncertain: 0
        }
    );
    assert_eq!(successful.wait().await.disposition, Disposition::Committed);
    assert!(handle.snapshot().available_reply_bytes < 131072);
    assert_eq!(
        handle.snapshot().available_error_work_bytes,
        ERROR_WORK_BYTES
    );
    drop(old_error);
    assert_eq!(handle.snapshot().available_reply_bytes, 131072);
    let (handle, owner) = AdmissionController::start(
        store.clone(),
        clock(),
        AdmissionLimits {
            reply_bytes: 256,
            error_allowance: 256,
            ..limits
        },
    )
    .unwrap();
    let outcome = submit(&handle, source).unwrap().wait().await;
    assert_eq!(outcome.disposition, Disposition::RolledBack);
    assert!(matches!(
        outcome.result,
        Err(AdmissionFailure::ReplyCapacity {
            class: FailureClass::Validation
        })
    ));
    assert_eq!(handle.snapshot().outstanding, 0);
    assert_eq!(handle.snapshot().available_reply_bytes, 256);
    assert_eq!(
        sqlx::query("SELECT payload FROM entities WHERE id=?")
            .bind(&sid)
            .fetch_one(&mut db)
            .await
            .unwrap()
            .get::<String, _>("payload"),
        payload.to_string()
    );
    owner.shutdown().await.unwrap();
    db.close().await.unwrap();
    store.shutdown().await.unwrap();
}
