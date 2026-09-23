use nebula_assistant_domain::AppendEvent;
use nebula_assistant_storage::{Config, Error, Journal};
use serde_json::json;
use sqlx::{Connection, Row, SqliteConnection, sqlite::SqliteConnectOptions};
use std::time::Duration;

fn event(turn: &str, key: &str) -> AppendEvent {
    AppendEvent {
        turn_id: turn.into(),
        event_type: "delta".into(),
        payload: json!({"text": key}).as_object().unwrap().clone(),
        actor_id: None,
        idempotency_key: Some(key.into()),
    }
}

#[tokio::test]
async fn committed_events_survive_reopen_and_idempotent_retries() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("assistant.db");
    let journal = Journal::create(&path, Config::default()).await.unwrap();
    let first = journal.append(event("turn", "one")).await.unwrap();
    assert_eq!(journal.append(event("turn", "one")).await.unwrap(), first);
    journal.shutdown().await.unwrap();
    let journal = Journal::open(&path, Config::default()).await.unwrap();
    assert_eq!(journal.append(event("turn", "one")).await.unwrap(), first);
    assert_eq!(
        journal.append(event("turn", "two")).await.unwrap().sequence,
        2
    );
    assert_eq!(journal.replay("turn", 0).await.unwrap().len(), 2);
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn conflicting_key_rolls_back_without_consuming_a_sequence() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(&temp.path().join("assistant.db"), Config::default())
        .await
        .unwrap();
    journal.append(event("turn", "one")).await.unwrap();
    let mut changed = event("turn", "one");
    changed.event_type = "different".into();
    assert!(matches!(
        journal.append(changed).await,
        Err(Error::Conflict)
    ));
    assert_eq!(
        journal.append(event("turn", "two")).await.unwrap().sequence,
        2
    );
    journal.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_appenders_produce_contiguous_unique_sequences() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(&temp.path().join("assistant.db"), Config::default())
        .await
        .unwrap();
    let mut tasks = tokio::task::JoinSet::new();
    for index in 0..100 {
        let journal = journal.clone();
        tasks.spawn(async move {
            journal
                .append(event("turn", &index.to_string()))
                .await
                .unwrap()
        });
    }
    while let Some(result) = tasks.join_next().await {
        result.unwrap();
    }
    let events = journal.replay("turn", 0).await.unwrap();
    assert_eq!(
        events.iter().map(|e| e.sequence).collect::<Vec<_>>(),
        (1..=100).collect::<Vec<_>>()
    );
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn lagged_viewer_replays_all_events_without_a_live_buffer() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(&temp.path().join("assistant.db"), Config::default())
        .await
        .unwrap();
    let mut viewer = journal.follow("turn", 0).unwrap();
    for index in 0..600 {
        journal
            .append(event("turn", &index.to_string()))
            .await
            .unwrap();
    }
    let mut all = Vec::new();
    while viewer.sequence() < 600 {
        all.extend(viewer.next_batch().await.unwrap());
    }
    assert_eq!(
        all.iter().map(|e| e.sequence).collect::<Vec<_>>(),
        (1..=600).collect::<Vec<_>>()
    );
    drop(viewer);
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn replay_then_wait_cannot_miss_the_next_commit() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(&temp.path().join("assistant.db"), Config::default())
        .await
        .unwrap();
    let mut viewer = journal.follow("turn", 0).unwrap();
    let waiting = tokio::spawn(async move { viewer.next_batch().await });
    tokio::task::yield_now().await;
    journal
        .append(event("other-turn", "ignored"))
        .await
        .unwrap();
    journal.append(event("turn", "one")).await.unwrap();
    let batch = tokio::time::timeout(Duration::from_secs(2), waiting)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(batch.len(), 1);
    assert_eq!(batch[0].turn_id, "turn");
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn non_laboratory_database_is_rejected_without_changes() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let mut connection = SqliteConnection::connect_with(
        &SqliteConnectOptions::new()
            .filename(&path)
            .create_if_missing(true),
    )
    .await
    .unwrap();
    sqlx::query("CREATE TABLE entities(id TEXT)")
        .execute(&mut connection)
        .await
        .unwrap();
    connection.close().await.unwrap();
    let before = std::fs::read(&path).unwrap();
    assert!(matches!(
        Journal::open(&path, Config::default()).await,
        Err(Error::IncompatibleDatabase)
    ));
    assert!(Journal::create(&path, Config::default()).await.is_err());
    assert_eq!(std::fs::read(&path).unwrap(), before);
}

#[tokio::test]
async fn ownership_and_append_only_constraints_are_enforced() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("assistant.db");
    let journal = Journal::create(&path, Config::default()).await.unwrap();
    journal.append(event("turn", "one")).await.unwrap();
    assert!(matches!(
        Journal::open(&path, Config::default()).await,
        Err(Error::AlreadyOwned)
    ));
    let mut connection =
        SqliteConnection::connect_with(&SqliteConnectOptions::new().filename(&path))
            .await
            .unwrap();
    assert!(
        sqlx::query("DELETE FROM assistant_events")
            .execute(&mut connection)
            .await
            .is_err()
    );
    assert!(
        sqlx::query("UPDATE assistant_events SET event_type='other'")
            .execute(&mut connection)
            .await
            .is_err()
    );
    let row = sqlx::query("PRAGMA journal_mode")
        .fetch_one(&mut connection)
        .await
        .unwrap();
    assert_eq!(row.get::<String, _>(0), "wal");
    connection.close().await.unwrap();
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn admission_is_bounded_and_rejected_writes_are_not_committed() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(
        &temp.path().join("assistant.db"),
        Config {
            writer_capacity: 1,
            readers: 1,
        },
    )
    .await
    .unwrap();
    // Poll every append once before yielding to the actor. Exactly one channel
    // slot can be reserved, regardless of when SQLite subsequently commits.
    let futures = (0..20)
        .map(|i| journal.append(event("turn", &i.to_string())))
        .collect::<Vec<_>>();
    let results = futures_util::future::join_all(futures).await;
    let committed = results.iter().filter(|r| r.is_ok()).count();
    assert_eq!(committed, 1);
    assert!(
        results
            .iter()
            .filter(|r| r.is_err())
            .all(|r| matches!(r, Err(Error::Capacity)))
    );
    assert_eq!(journal.replay("turn", 0).await.unwrap().len(), committed);
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn replay_byte_budget_and_payload_limit_bound_memory() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(&temp.path().join("assistant.db"), Config::default())
        .await
        .unwrap();
    for index in 0..8 {
        let mut large = event("turn", &index.to_string());
        large
            .payload
            .insert("text".into(), json!("x".repeat(900_000)));
        journal.append(large).await.unwrap();
    }
    let first = journal.replay("turn", 0).await.unwrap();
    assert_eq!(first.len(), 4);
    assert_eq!(journal.replay("turn", 4).await.unwrap().len(), 4);
    let mut too_large = event("turn", "large");
    too_large
        .payload
        .insert("text".into(), json!("x".repeat(1_048_576)));
    assert!(matches!(
        journal.append(too_large).await,
        Err(Error::Validation(_))
    ));
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn disconnected_append_caller_can_resolve_outcome_by_idempotency() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(&temp.path().join("assistant.db"), Config::default())
        .await
        .unwrap();
    let original = event("turn", "one");
    let mut request = Box::pin(journal.append(original.clone()));
    assert!(futures_util::poll!(request.as_mut()).is_pending());
    drop(request);
    let resolved = journal.append(original).await.unwrap();
    assert_eq!(resolved.sequence, 1);
    assert_eq!(journal.replay("turn", 0).await.unwrap().len(), 1);
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn process_exit_after_commit_preserves_acknowledged_events() {
    const CHILD_PATH: &str = "NEBULA_ASSISTANT_JOURNAL_CRASH_TEST_PATH";
    if let Some(path) = std::env::var_os(CHILD_PATH) {
        let journal = Journal::create(std::path::Path::new(&path), Config::default())
            .await
            .unwrap();
        journal.append(event("turn", "committed")).await.unwrap();
        // Deliberately skip destructors and graceful shutdown after commit.
        std::process::exit(31);
    }
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("assistant.db");
    let result = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "process_exit_after_commit_preserves_acknowledged_events",
            "--exact",
        ])
        .env(CHILD_PATH, &path)
        .output()
        .unwrap();
    assert_eq!(
        result.status.code(),
        Some(31),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let journal = Journal::open(&path, Config::default()).await.unwrap();
    assert_eq!(journal.replay("turn", 0).await.unwrap().len(), 1);
    assert_eq!(
        journal
            .append(event("turn", "committed"))
            .await
            .unwrap()
            .sequence,
        1
    );
    assert_eq!(
        journal
            .append(event("turn", "next"))
            .await
            .unwrap()
            .sequence,
        2
    );
    journal.shutdown().await.unwrap();
}

#[tokio::test]
async fn viewer_admission_is_bounded_and_drop_releases_capacity() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(&temp.path().join("assistant.db"), Config::default())
        .await
        .unwrap();
    let mut viewers = (0..4096)
        .map(|_| journal.follow("turn", 0).unwrap())
        .collect::<Vec<_>>();
    assert!(matches!(
        journal.follow("turn", 0),
        Err(Error::SubscriberCapacity)
    ));
    assert!(matches!(
        journal.follow("other", 0),
        Err(Error::SubscriberCapacity)
    ));
    viewers.pop();
    let replacement = journal.follow("other", 0).unwrap();
    drop(viewers);
    drop(replacement);
    // Different turn ids must also be pruned; historical subscriptions may not
    // consume permanent map capacity.
    for i in 0..5000 {
        drop(journal.follow(&format!("turn-{i}"), 0).unwrap());
    }
    journal.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn one_hundred_viewers_follow_only_their_own_turn() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Journal::create(&temp.path().join("assistant.db"), Config::default())
        .await
        .unwrap();
    let mut viewers = tokio::task::JoinSet::new();
    for i in 0..100 {
        let turn = format!("turn-{i}");
        let mut cursor = journal.follow(&turn, 0).unwrap();
        viewers.spawn(async move {
            let events = cursor.next_batch().await.unwrap();
            assert_eq!(events.len(), 1);
            assert_eq!(events[0].turn_id, turn);
        });
    }
    for i in 0..100 {
        journal
            .append(event(&format!("turn-{i}"), "one"))
            .await
            .unwrap();
    }
    tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(result) = viewers.join_next().await {
            result.unwrap();
        }
    })
    .await
    .unwrap();
    journal.shutdown().await.unwrap();
}
