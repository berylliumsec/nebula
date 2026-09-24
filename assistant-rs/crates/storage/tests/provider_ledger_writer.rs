#[allow(dead_code)]
mod support;
use nebula_assistant_domain::provider_stream::Draft;
use nebula_assistant_storage::entities::provider_ledger::{self as schema, writer::*};
use serde_json::json;
use sqlx::{Connection, SqliteConnection};
use support::{database, raw};
use uuid::Uuid;

const CAP: u64 = 64 * 1024 * 1024;
const RESERVE: i64 = 16 * 1024 * 1024;
fn uuid() -> Uuid {
    Uuid::from_u128(901)
}
fn limits() -> schema::SchemaLimits {
    schema::SchemaLimits {
        capacity_bytes: CAP,
    }
}
fn scope() -> StreamFence {
    StreamFence {
        turn_id: "fixture-chat_turns".into(),
        stream_id: "stream-one".into(),
        session_id: "fixture-chat_sessions".into(),
        project_id: "project-alpha".into(),
    }
}
fn attempt() -> AttemptFence {
    AttemptFence {
        attempt_id: "attempt-one".into(),
        ordinal: 1,
        owner_id: "worker".into(),
        claim_id: "claim-one".into(),
        request_sha256: Digest([1; 32]),
        route_sha256: Digest([2; 32]),
    }
}
fn canonical(s: &str) -> CanonicalJson {
    CanonicalJson::from_raw(s).unwrap()
}
fn ordinary(key: &str, epoch: i64, body: &str) -> ReceiptCommand {
    ReceiptCommand {
        key: key.into(),
        turn_epoch: epoch,
        command: canonical(body),
        payload: ReceiptPayload::Ordinary {
            kind: OrdinaryReceiptKind::Admitted,
            payload: canonical(r#"{"accepted":true}"#),
        },
    }
}
fn control(key: &str, epoch: i64, payload: ControlReceipt) -> ReceiptCommand {
    ReceiptCommand {
        key: key.into(),
        turn_epoch: epoch,
        command: canonical("{}"),
        payload: ReceiptPayload::Control(Box::new(payload)),
    }
}
fn proof(epoch: i64) -> RecordProof {
    RecordProof {
        id: scope().turn_id,
        revision: "1".into(),
        mutation_epoch: epoch,
        raw_sha256: Digest([3; 32]),
    }
}
fn recovery() -> ControlReceipt {
    ControlReceipt::RecoveryClassified {
        turn: proof(0),
        classification: RecoveryClass::Interrupted,
        evidence_receipt: None,
    }
}
fn reference(receipt: &StagedReceipt) -> ReceiptReference {
    ReceiptReference {
        sequence: receipt.sequence,
        hash: receipt.receipt_hash,
    }
}
fn event(key: &str, kind: &str, settlement: Option<ReceiptReference>) -> EventCommand {
    EventCommand {
        key: key.into(),
        turn_epoch: 0,
        settlement,
        draft: Draft::from_payload(kind, &json!({"type":kind,"text":"hello π"})).unwrap(),
    }
}
async fn fresh() -> (tempfile::TempDir, SqliteConnection) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("ledger.db");
    database(&path).await;
    let mut db = raw(&path).await;
    schema::install(
        &mut db,
        schema::InstallIdentity {
            database_uuid: uuid(),
            installed_at_us: 1,
        },
        limits(),
    )
    .await
    .unwrap();
    (dir, db)
}
async fn initialize(db: &mut SqliteConnection, reservation: i64) {
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    ledger
        .open_stream(&scope(), reservation, 0, 1)
        .await
        .unwrap();
    ledger
        .register_watches(&scope(), &[scope().turn_id, scope().session_id], 1)
        .await
        .unwrap();
    ledger
        .add_attempt(&scope(), &attempt(), None, 1)
        .await
        .unwrap();
    tx.commit().await.unwrap();
}
async fn budget(db: &mut SqliteConnection) -> (i64, i64) {
    sqlx::query_as("SELECT used_bytes,reserved_bytes FROM assistant_provider_budget")
        .fetch_one(db)
        .await
        .unwrap()
}
async fn legacy(db: &mut SqliteConnection) -> Vec<String> {
    sqlx::query_scalar("SELECT json_array(id,kind,revision,payload) FROM entities ORDER BY id")
        .fetch_all(db)
        .await
        .unwrap()
}
async fn corrupt(db: &mut SqliteConnection, trigger: &str, sql: &str) {
    let ddl: String = sqlx::query_scalar("SELECT sql FROM sqlite_schema WHERE name=?")
        .bind(trigger)
        .fetch_one(&mut *db)
        .await
        .unwrap();
    sqlx::query(&format!("DROP TRIGGER {trigger}"))
        .execute(&mut *db)
        .await
        .unwrap();
    sqlx::query(sql).execute(&mut *db).await.unwrap();
    sqlx::query(&ddl).execute(db).await.unwrap();
}

#[tokio::test]
async fn ledger_writer_atomic_receipts_events_dedup_and_reopen() {
    let (dir, mut db) = fresh().await;
    let before = legacy(&mut db).await;
    initialize(&mut db, RESERVE).await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    let first = ledger
        .append_receipt(
            &scope(),
            Some(&attempt()),
            ordinary(
                "admission",
                0,
                r#"{"z":1e100,"a":10000000000000000000000000000000000000000}"#,
            ),
            2,
        )
        .await
        .unwrap();
    let duplicate = ledger
        .append_receipt(
            &scope(),
            Some(&attempt()),
            ordinary(
                "admission",
                0,
                r#"{"a":10000000000000000000000000000000000000000,"z":1e100}"#,
            ),
            99,
        )
        .await
        .unwrap();
    assert!(duplicate.duplicate);
    assert_eq!(first.receipt_hash, duplicate.receipt_hash);
    assert!(matches!(
        ledger
            .append_receipt(
                &scope(),
                Some(&attempt()),
                ordinary(
                    "admission",
                    1,
                    r#"{"a":10000000000000000000000000000000000000000,"z":1e100}"#
                ),
                3
            )
            .await,
        Err(Error::Conflict)
    ));
    assert!(matches!(
        ledger
            .append_receipt(
                &scope(),
                Some(&attempt()),
                ordinary(
                    "admission",
                    0,
                    r#"{"a":10000000000000000000000000000000000000000,"z":1.0e100}"#
                ),
                3
            )
            .await,
        Err(Error::Conflict)
    ));
    let one = ledger
        .append_event(
            &scope(),
            Some(&attempt()),
            event("token-one", "token", None),
            3,
        )
        .await
        .unwrap();
    let duplicate = ledger
        .append_event(
            &scope(),
            Some(&attempt()),
            event("token-one", "token", None),
            99,
        )
        .await
        .unwrap();
    assert!(duplicate.duplicate);
    assert_eq!(duplicate.event.sse, one.event.sse);
    let two = ledger
        .append_event(
            &scope(),
            Some(&attempt()),
            event("token-two", "token", None),
            4,
        )
        .await
        .unwrap();
    assert_eq!((one.event.sequence, two.event.sequence), (1, 2));
    let completed = ledger
        .append_receipt(
            &scope(),
            Some(&attempt()),
            control(
                "complete",
                0,
                ControlReceipt::Completed {
                    turn: proof(0),
                    answer_id: "answer".into(),
                    answer_sha256: Digest([4; 32]),
                },
            ),
            5,
        )
        .await
        .unwrap();
    assert!(matches!(
        ledger
            .append_event(
                &scope(),
                Some(&attempt()),
                event("done", "done", Some(reference(&completed))),
                6
            )
            .await,
        Err(Error::Conflict)
    ));
    let released = ledger
        .append_receipt(
            &scope(),
            Some(&attempt()),
            control(
                "release",
                0,
                ControlReceipt::Released {
                    turn: proof(0),
                    settlement_receipt: completed.receipt_hash,
                },
            ),
            6,
        )
        .await
        .unwrap();
    let done = ledger
        .append_event(
            &scope(),
            Some(&attempt()),
            event("done", "done", Some(reference(&released))),
            7,
        )
        .await
        .unwrap();
    assert_eq!(done.event.sequence, 3);
    tx.commit().await.unwrap();
    let conserved = budget(&mut db).await;
    assert_eq!(conserved.0 + conserved.1, RESERVE);
    assert_eq!(legacy(&mut db).await, before);
    db.close().await.unwrap();
    let mut db = raw(&dir.path().join("ledger.db")).await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    let page = ledger
        .replay(&scope(), ReplayCursor::After(0), PageLimits::default())
        .await
        .unwrap();
    assert_eq!(page.events.len(), 3);
    assert_eq!(page.events[0].sse, one.event.sse);
    assert_eq!(page.events[2].sse, done.event.sse);
    assert_eq!(page.stream.state, "terminal");
    assert!(!page.has_more);
    assert!(
        ledger
            .append_event(&scope(), Some(&attempt()), event("late", "token", None), 8)
            .await
            .is_err()
    );
    tx.rollback().await.unwrap();
    assert_eq!(budget(&mut db).await, conserved);
    // A staged handle acknowledges only its savepoint, never the enclosing transaction.
    let (_dir, mut db) = fresh().await;
    initialize(&mut db, RESERVE).await;
    let before = budget(&mut db).await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    let staged = ledger
        .append_event(&scope(), None, event("rolled-back", "token", None), 1)
        .await
        .unwrap();
    assert_eq!(staged.event.sequence, 1);
    tx.rollback().await.unwrap();
    assert_eq!(budget(&mut db).await, before);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM assistant_provider_events")
        .fetch_one(&mut db)
        .await
        .unwrap();
    assert_eq!(count, 0);
}

#[tokio::test]
async fn ledger_writer_control_quota_conserves_reservations_and_rolls_back() {
    let (_dir, mut db) = fresh().await;
    let reservation = CONTROL_FLOOR
        + stream_charge(&scope()).unwrap()
        + watch_charge(&scope().turn_id).unwrap()
        + watch_charge(&scope().session_id).unwrap()
        + attempt_charge(&scope(), &attempt(), None).unwrap();
    initialize(&mut db, reservation).await;
    let before = budget(&mut db).await;
    assert_eq!(before.1, CONTROL_FLOOR);
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), schema::SchemaLimits { capacity_bytes: 1 })
        .await
        .unwrap();
    assert!(matches!(
        ledger.reserve_more(&scope(), 1, 1).await,
        Err(Error::Capacity)
    ));
    assert!(matches!(
        ledger
            .append_event(
                &scope(),
                Some(&attempt()),
                event("over-quota", "token", None),
                1
            )
            .await,
        Err(Error::Capacity)
    ));
    let page = ledger
        .replay(&scope(), ReplayCursor::BeyondHead, PageLimits::default())
        .await
        .unwrap();
    assert!(page.events.is_empty());
    let mut last = None;
    for i in 0..MAX_CONTROL_RECEIPTS {
        let current = attempt();
        let bound = if i % 2 == 0 { Some(&current) } else { None };
        let receipt = ledger
            .append_receipt(
                &scope(),
                bound,
                control(&format!("control-{i}"), 0, recovery()),
                2 + i,
            )
            .await
            .unwrap();
        last = Some(receipt);
    }
    let final_key = format!("control-{}", MAX_CONTROL_RECEIPTS - 1);
    assert!(
        ledger
            .append_receipt(&scope(), None, control(&final_key, 0, recovery()), 20)
            .await
            .unwrap()
            .duplicate
    );
    assert!(matches!(
        ledger
            .append_receipt(
                &scope(),
                Some(&attempt()),
                control("ninth", 0, recovery()),
                21
            )
            .await,
        Err(Error::Capacity)
    ));
    assert!(matches!(
        ledger
            .append_event(&scope(), None, event("error", "error", None), 22)
            .await,
        Err(Error::Invalid)
    ));
    let final_receipt = last.unwrap();
    let wrong = ReceiptReference {
        sequence: final_receipt.sequence,
        hash: Digest::ZERO,
    };
    assert!(
        ledger
            .append_event(&scope(), None, event("error", "error", Some(wrong)), 23)
            .await
            .is_err()
    );
    assert!(
        ledger
            .append_event(
                &scope(),
                Some(&attempt()),
                event("wrong-attempt", "error", Some(reference(&final_receipt))),
                23
            )
            .await
            .is_err()
    );
    // Source shutdown is an error event backed by interrupted recovery, not a fabricated Failed state.
    ledger
        .append_event(
            &scope(),
            None,
            event("error", "error", Some(reference(&final_receipt))),
            24,
        )
        .await
        .unwrap();
    assert!(matches!(
        ledger
            .release_unused_reservation(&scope(), None, 0, reference(&final_receipt), 25)
            .await,
        Err(Error::Lineage)
    ));
    tx.commit().await.unwrap();
    let after = budget(&mut db).await;
    assert!(after.0 > before.0);
    assert_eq!(after.0 + after.1, reservation);
    assert!(after.1 > 0);
    let counts:(i64,i64)=sqlx::query_as("SELECT (SELECT count(*) FROM assistant_provider_receipts),(SELECT count(*) FROM assistant_provider_events)").fetch_one(&mut db).await.unwrap();
    assert_eq!(counts, (8, 1));
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), schema::SchemaLimits { capacity_bytes: 1 })
        .await
        .unwrap();
    assert_eq!(
        ledger
            .replay(&scope(), ReplayCursor::After(0), PageLimits::default())
            .await
            .unwrap()
            .events
            .len(),
        1
    );
    tx.rollback().await.unwrap();
    for terminal_type in ["error", "cancelled"] {
        let (_dir, mut db) = fresh().await;
        initialize(&mut db, RESERVE).await;
        let mut tx = db.begin().await.unwrap();
        let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
        let payload = if terminal_type == "error" {
            ControlReceipt::Failed {
                turn: proof(0),
                note: None,
                reason: FailureReason::ProviderFailure,
            }
        } else {
            ControlReceipt::Cancelled {
                turn: proof(0),
                note: None,
                reason: FailureReason::Stopped,
            }
        };
        let settled = ledger
            .append_receipt(&scope(), Some(&attempt()), control("settle", 0, payload), 1)
            .await
            .unwrap();
        let released = ledger
            .append_receipt(
                &scope(),
                Some(&attempt()),
                control(
                    "release",
                    0,
                    ControlReceipt::Released {
                        turn: proof(0),
                        settlement_receipt: settled.receipt_hash,
                    },
                ),
                2,
            )
            .await
            .unwrap();
        assert!(matches!(
            ledger
                .release_unused_reservation(&scope(), Some(&attempt()), 0, reference(&released), 3)
                .await,
            Err(Error::Lineage)
        ));
        assert!(matches!(
            ledger
                .append_event(
                    &scope(),
                    Some(&attempt()),
                    event("false-done", "done", Some(reference(&released))),
                    3
                )
                .await,
            Err(Error::Conflict)
        ));
        ledger
            .append_event(
                &scope(),
                Some(&attempt()),
                event("terminal", terminal_type, Some(reference(&released))),
                3,
            )
            .await
            .unwrap();
        tx.commit().await.unwrap();
        let before = budget(&mut db).await;
        let mut tx = db.begin().await.unwrap();
        let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
        assert_eq!(
            ledger
                .release_unused_reservation(&scope(), Some(&attempt()), 0, reference(&released), 4)
                .await
                .unwrap(),
            before.1
        );
        tx.rollback().await.unwrap();
        assert_eq!(budget(&mut db).await, before);
        let mut tx = db.begin().await.unwrap();
        let mut ledger =
            LedgerTx::bind(&mut tx, uuid(), schema::SchemaLimits { capacity_bytes: 1 })
                .await
                .unwrap();
        assert_eq!(
            ledger
                .release_unused_reservation(&scope(), Some(&attempt()), 0, reference(&released), 4)
                .await
                .unwrap(),
            before.1
        );
        assert_eq!(
            ledger
                .release_unused_reservation(&scope(), Some(&attempt()), 0, reference(&released), 5)
                .await
                .unwrap(),
            0
        );
        tx.commit().await.unwrap();
        assert_eq!(budget(&mut db).await, (before.0, 0));
        let counts:(i64,i64)=sqlx::query_as("SELECT (SELECT count(*) FROM assistant_provider_receipts),(SELECT count(*) FROM assistant_provider_events)").fetch_one(&mut db).await.unwrap();
        assert_eq!(counts, (2, 1));
    }
}

#[tokio::test]
async fn ledger_writer_replay_rejects_corruption_and_bounds_cursors() {
    let (_dir, mut db) = fresh().await;
    initialize(&mut db, RESERVE).await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    ledger
        .append_receipt(&scope(), Some(&attempt()), ordinary("admitted", 0, "{}"), 1)
        .await
        .unwrap();
    for i in 0..3 {
        ledger
            .append_event(
                &scope(),
                Some(&attempt()),
                event(&format!("token-{i}"), "token", None),
                2 + i,
            )
            .await
            .unwrap();
    }
    tx.commit().await.unwrap();
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    assert!(matches!(
        ledger
            .replay(
                &scope(),
                ReplayCursor::After(0),
                PageLimits {
                    events: 2,
                    bytes: 1
                }
            )
            .await,
        Err(Error::Capacity)
    ));
    let first = ledger
        .replay(
            &scope(),
            ReplayCursor::After(0),
            PageLimits {
                events: 2,
                bytes: 4096,
            },
        )
        .await
        .unwrap();
    assert_eq!(first.events.len(), 2);
    assert!(first.has_more);
    let tail = ledger
        .replay(&scope(), first.next, PageLimits::default())
        .await
        .unwrap();
    assert_eq!(tail.events[0].sequence, 3);
    assert!(!tail.has_more);
    let huge = ReplayCursor::decimal(&"9".repeat(4300)).unwrap();
    assert!(
        ledger
            .replay(&scope(), huge, PageLimits::default())
            .await
            .unwrap()
            .events
            .is_empty()
    );
    tx.rollback().await.unwrap();
    sqlx::query("UPDATE assistant_provider_streams SET first_retained_sequence=2")
        .execute(&mut db)
        .await
        .unwrap();
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    assert!(matches!(
        ledger
            .replay(&scope(), ReplayCursor::After(0), PageLimits::default())
            .await,
        Err(Error::CursorExpired)
    ));
    tx.rollback().await.unwrap();
    // Even end/future cursors check the retained head; restoring DDL does not conceal row tampering.
    corrupt(
        &mut db,
        "assistant_provider_events_no_update",
        "UPDATE assistant_provider_events SET sse_bytes=x'78',sse_bytes_len=1 WHERE sequence=3",
    )
    .await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    for cursor in [ReplayCursor::After(3), ReplayCursor::BeyondHead] {
        assert!(matches!(
            ledger.replay(&scope(), cursor, PageLimits::default()).await,
            Err(Error::Corrupt)
        ));
    }
    tx.rollback().await.unwrap();
    let (_dir, mut db) = fresh().await;
    initialize(&mut db, RESERVE).await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    ledger
        .append_receipt(&scope(), None, ordinary("admitted", 0, "{}"), 1)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    corrupt(
        &mut db,
        "assistant_provider_receipts_delete_gate",
        "DELETE FROM assistant_provider_receipts",
    )
    .await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    assert!(matches!(
        ledger
            .replay(&scope(), ReplayCursor::BeyondHead, PageLimits::default())
            .await,
        Err(Error::Corrupt)
    ));
    tx.rollback().await.unwrap();
    // Terminal event links are durable foreign keys; an unrelated valid receipt is not settlement.
    let (_dir, mut db) = fresh().await;
    initialize(&mut db, RESERVE).await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    let receipt = ledger
        .append_receipt(&scope(), None, control("recovery", 0, recovery()), 1)
        .await
        .unwrap();
    ledger
        .append_event(
            &scope(),
            None,
            event("shutdown", "error", Some(reference(&receipt))),
            2,
        )
        .await
        .unwrap();
    tx.commit().await.unwrap();
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    assert_eq!(
        ledger
            .replay(&scope(), ReplayCursor::After(0), PageLimits::default())
            .await
            .unwrap()
            .events
            .len(),
        1
    );
    ledger
        .append_receipt(
            &scope(),
            None,
            control("another-recovery", 0, recovery()),
            3,
        )
        .await
        .unwrap();
    tx.commit().await.unwrap();
    corrupt(
        &mut db,
        "assistant_provider_events_no_update",
        "UPDATE assistant_provider_events SET settlement_receipt_sequence=2 WHERE sequence=1",
    )
    .await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    assert!(matches!(
        ledger
            .replay(&scope(), ReplayCursor::BeyondHead, PageLimits::default())
            .await,
        Err(Error::Corrupt)
    ));
    tx.rollback().await.unwrap();
    let ddl: String = sqlx::query_scalar(
        "SELECT sql FROM sqlite_schema WHERE name='assistant_provider_receipts_delete_gate'",
    )
    .fetch_one(&mut db)
    .await
    .unwrap();
    sqlx::query("DROP TRIGGER assistant_provider_receipts_delete_gate")
        .execute(&mut db)
        .await
        .unwrap();
    assert!(
        sqlx::query("DELETE FROM assistant_provider_receipts")
            .execute(&mut db)
            .await
            .is_err()
    );
    sqlx::query(&ddl).execute(&mut db).await.unwrap();
}

#[tokio::test]
async fn ledger_writer_watch_fences_and_canonical_identity_preserve_lineage() {
    let giant = format!("1{}", "0".repeat(100));
    let raw = format!(r#"{{"z":{giant},"a":1e100}}"#);
    let expected = format!(r#"{{"a":1e100,"z":{giant}}}"#);
    assert_eq!(canonical(&raw).text(), expected);
    assert_ne!(canonical("1.0").bytes(), canonical("1").bytes());
    assert_ne!(canonical("1e100").bytes(), canonical(&giant).bytes());
    let e: Error = CanonicalJson::from_raw(r#"{"a":1,"\u0061":2}"#)
        .unwrap_err()
        .into();
    assert!(matches!(e, Error::Invalid));
    for raw in [
        format!("[{}]", vec!["0"; 10001].join(",")),
        format!(
            "{{{}}}",
            (0..10001)
                .map(|i| format!("\"k{i}\":0"))
                .collect::<Vec<_>>()
                .join(",")
        ),
        format!("{}0{}", "[".repeat(65), "]".repeat(65)),
    ] {
        let e: Error = CanonicalJson::from_raw(&raw).unwrap_err().into();
        assert!(matches!(e, Error::Capacity));
    }
    assert!(ReplayCursor::decimal("-1").is_err());
    let (_dir, mut db) = fresh().await;
    initialize(&mut db, RESERVE).await;
    let before = budget(&mut db).await;
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    let watches = ledger
        .register_watches(&scope(), &[scope().turn_id, "future-answer".into()], 1)
        .await
        .unwrap();
    assert_eq!(watches[0].epoch, 0);
    assert!(watches[0].rowid.is_some());
    assert!(watches[1].rowid.is_none());
    let mut wrong = attempt();
    wrong.claim_id = "another-claim".into();
    assert!(matches!(
        ledger.attempt_phase(&scope(), &wrong).await,
        Err(Error::Conflict)
    ));
    tx.commit().await.unwrap();
    assert_eq!(
        before.1 - budget(&mut db).await.1,
        watch_charge("future-answer").unwrap()
    );
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) SELECT 'future-answer',kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at FROM entities WHERE id='fixture-chat_messages'").execute(&mut db).await.unwrap();
    let observed:(i64,i64)=sqlx::query_as("SELECT mutation_epoch,present FROM assistant_provider_entity_epochs WHERE entity_id='future-answer'").fetch_one(&mut db).await.unwrap();
    assert_eq!(observed, (1, 1));
    sqlx::query("UPDATE entities SET payload=payload || ' ' WHERE id='fixture-chat_turns'")
        .execute(&mut db)
        .await
        .unwrap();
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    assert!(matches!(
        ledger
            .append_event(&scope(), Some(&attempt()), event("stale", "token", None), 2)
            .await,
        Err(Error::Lineage)
    ));
    assert!(matches!(
        ledger
            .replay(&scope(), ReplayCursor::BeyondHead, PageLimits::default())
            .await,
        Err(Error::Lineage)
    ));
    let watches = ledger
        .register_watches(&scope(), &[scope().turn_id], 3)
        .await
        .unwrap();
    assert_eq!(watches[0].epoch, 1);
    tx.rollback().await.unwrap();
    // Completion and release are source-separated Turn mutations: proof epochs are
    // historical, while the terminal event is fenced by the current release epoch.
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    let completed = ledger
        .append_receipt(
            &scope(),
            Some(&attempt()),
            control(
                "complete",
                1,
                ControlReceipt::Completed {
                    turn: proof(1),
                    answer_id: "future-answer".into(),
                    answer_sha256: Digest([4; 32]),
                },
            ),
            4,
        )
        .await
        .unwrap();
    tx.commit().await.unwrap();
    sqlx::query("UPDATE entities SET payload=payload || ' ' WHERE id='fixture-chat_turns'")
        .execute(&mut db)
        .await
        .unwrap();
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    let released = ledger
        .append_receipt(
            &scope(),
            Some(&attempt()),
            control(
                "release",
                2,
                ControlReceipt::Released {
                    turn: proof(2),
                    settlement_receipt: completed.receipt_hash,
                },
            ),
            5,
        )
        .await
        .unwrap();
    let mut stale = event("done", "done", Some(reference(&released)));
    stale.turn_epoch = 1;
    assert!(matches!(
        ledger
            .append_event(&scope(), Some(&attempt()), stale, 6)
            .await,
        Err(Error::Lineage)
    ));
    let mut done = event("done", "done", Some(reference(&released)));
    done.turn_epoch = 2;
    ledger
        .append_event(&scope(), Some(&attempt()), done, 6)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    let mut tx = db.begin().await.unwrap();
    let mut ledger = LedgerTx::bind(&mut tx, uuid(), limits()).await.unwrap();
    assert_eq!(
        ledger
            .replay(&scope(), ReplayCursor::After(0), PageLimits::default())
            .await
            .unwrap()
            .events
            .len(),
        1
    );
    tx.rollback().await.unwrap();
}
