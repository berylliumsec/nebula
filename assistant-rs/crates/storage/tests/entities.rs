mod support;

use nebula_assistant_domain::records::{
    AssistantKind as Kind, MAX_RECORD_BYTES, StoredAssistantRecord,
};
use nebula_assistant_storage::entities::{
    Config, Error, ListQuery, Mutation, SqliteAssistantStore,
};
use serde_json::json;
use sqlx::{Connection, Row};
use std::{path::Path, time::Duration};
use support::{database, fixture, patch, payload, raw, record};

async fn open(path: &Path) -> SqliteAssistantStore {
    SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap()
}

#[tokio::test]
async fn reads_python_records_and_checks_persisted_envelopes() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    database(&path).await;
    let store = open(&path).await;
    for row in fixture()["entities"].as_array().unwrap() {
        let kind = Kind::try_from(row["kind"].as_str().unwrap()).unwrap();
        assert_eq!(
            store
                .get(kind, row["id"].as_str().unwrap())
                .await
                .unwrap()
                .payload(),
            &row["payload"]
        );
    }
    assert!(matches!(
        store.get(Kind::Message, "fixture-chat_sessions").await,
        Err(Error::NotFound)
    ));
    assert!(matches!(
        SqliteAssistantStore::open(&path, Config::default()).await,
        Err(Error::AlreadyOwned)
    ));
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn atomic_changes_preserve_search_revisions_and_legacy_readability() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    database(&path).await;
    let store = open(&path).await;
    let results = store.apply(vec![
        patch(Kind::Session, "fixture-chat_sessions", 2, json!({"title":"\u{1c} Café 🦀 \u{1f}","model":"\u{1c} model \u{1f}","engagement_id":" moved ", "metadata":{"opaque":"  retain  "}})),
        patch(Kind::Message, "fixture-chat_messages", 2, json!({"session_id":"new-session","metadata":{"retracted_at":"retained"}})),
        Mutation::Create(record(Kind::Bookmark, "new-bookmark")),
    ]).await.unwrap();
    assert_eq!(
        results[0].as_ref().unwrap().payload()["title"],
        "\u{1c} Café 🦀 \u{1f}"
    );
    assert_eq!(
        results[0].as_ref().unwrap().payload()["metadata"]["opaque"],
        "  retain  "
    );
    assert!(results[1].as_ref().unwrap().is_replaced_message());
    let mut connection = raw(&path).await;
    let search = sqlx::query("SELECT * FROM search_documents WHERE id = 'fixture-chat_sessions'")
        .fetch_one(&mut connection)
        .await
        .unwrap();
    assert_eq!(search.get::<String, _>("label"), "Café 🦀");
    assert_eq!(search.get::<String, _>("project_id"), "moved");
    assert_eq!(search.get::<String, _>("description"), "model");
    assert_eq!(
        results[0].as_ref().unwrap().payload()["model"],
        "\u{1c} model \u{1f}"
    );
    assert_eq!(search.get::<i64, _>("revision"), 3);
    store
        .apply(vec![patch(
            Kind::Session,
            "fixture-chat_sessions",
            3,
            json!({"metadata":{"temporary_assistant":true}}),
        )])
        .await
        .unwrap();
    assert!(
        store
            .list(ListQuery::new(Kind::Session))
            .await
            .unwrap()
            .records
            .is_empty()
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM search_documents WHERE id = 'fixture-chat_sessions'"
        )
        .fetch_one(&mut connection)
        .await
        .unwrap(),
        0
    );
    let mut query = ListQuery::new(Kind::Session);
    query.include_temporary = true;
    assert_eq!(store.list(query).await.unwrap().records.len(), 1);
    store
        .apply(vec![
            patch(
                Kind::Session,
                "fixture-chat_sessions",
                4,
                json!({"metadata":{}}),
            ),
            Mutation::Delete {
                kind: Kind::Bookmark,
                id: "new-bookmark".into(),
                expected_revision: 2,
            },
        ])
        .await
        .unwrap();
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT revision FROM search_documents WHERE id = 'fixture-chat_sessions'"
        )
        .fetch_one(&mut connection)
        .await
        .unwrap(),
        5
    );
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
    let store = open(&path).await;
    assert_eq!(
        store
            .get(Kind::Session, "fixture-chat_sessions")
            .await
            .unwrap()
            .payload()["revision"],
        5
    );
    assert!(matches!(
        store.get(Kind::Bookmark, "new-bookmark").await,
        Err(Error::NotFound)
    ));
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn conflicts_and_invalid_changes_roll_back_the_entire_batch() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    database(&path).await;
    let store = open(&path).await;
    for bad in [
        patch(
            Kind::Message,
            "fixture-chat_messages",
            2,
            json!({"content":""}),
        ),
        Mutation::Create(record(Kind::Session, "fixture-chat_sessions")),
        patch(
            Kind::Session,
            "fixture-chat_sessions",
            1,
            json!({"title":"stale"}),
        ),
        patch(
            Kind::Session,
            "fixture-chat_sessions",
            2,
            json!({"revision":99}),
        ),
    ] {
        assert!(
            store
                .apply(vec![
                    patch(
                        Kind::Session,
                        "fixture-chat_sessions",
                        2,
                        json!({"title":"must roll back"})
                    ),
                    bad
                ])
                .await
                .is_err()
        );
        assert_eq!(
            store
                .get(Kind::Session, "fixture-chat_sessions")
                .await
                .unwrap()
                .payload(),
            &payload(Kind::Session)
        );
    }
    let mut connection = raw(&path).await;
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT revision FROM search_documents WHERE id = 'fixture-chat_sessions'"
        )
        .fetch_one(&mut connection)
        .await
        .unwrap(),
        2
    );
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn simultaneous_updates_have_one_winner_and_reads_survive_writer_contention() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    database(&path).await;
    let store = open(&path).await;
    let mut tasks = tokio::task::JoinSet::new();
    for index in 0..32 {
        let store = store.clone();
        tasks.spawn(async move {
            store
                .apply(vec![patch(
                    Kind::Session,
                    "fixture-chat_sessions",
                    2,
                    json!({"title":format!("winner {index}")}),
                )])
                .await
        });
    }
    let mut winners = 0;
    while let Some(result) = tasks.join_next().await {
        match result.unwrap() {
            Ok(_) => winners += 1,
            Err(Error::RevisionConflict { .. }) => {}
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(winners, 1);
    let mut connection = raw(&path).await;
    let lock = connection.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let reader = tokio::time::timeout(
        Duration::from_secs(3),
        store.get(Kind::Session, "fixture-chat_sessions"),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(reader.payload()["revision"], 3);
    lock.rollback().await.unwrap();
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn session_queries_filter_before_paging_and_respect_byte_bounds() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    database(&path).await;
    let store = SqliteAssistantStore::open(
        &path,
        Config {
            page_bytes: 1,
            ..Config::default()
        },
    )
    .await
    .unwrap();
    for start in (0..1018).step_by(64) {
        let mutations = (start..(start + 64).min(1018))
            .map(|index| {
                let mut p = payload(Kind::Message);
                p["id"] = format!("message-{index:04}").into();
                p["session_id"] = if index < 1005 { "unrelated" } else { "target" }.into();
                Mutation::Create(
                    StoredAssistantRecord::decode(Kind::Message, &serde_json::to_vec(&p).unwrap())
                        .unwrap(),
                )
            })
            .collect();
        store.apply(mutations).await.unwrap();
    }
    let mut offset = 0;
    let mut ids = Vec::new();
    loop {
        let mut query = ListQuery::new(Kind::Message);
        query.session_id = Some("target".into());
        query.engagement_id = Some("project".into());
        query.offset = offset;
        query.limit = 4;
        let page = store.list(query).await.unwrap();
        assert_eq!(page.records.len(), 1);
        ids.push(page.records[0].payload()["id"].as_str().unwrap().to_owned());
        match page.next_offset {
            Some(next) => offset = next,
            None => break,
        }
    }
    assert_eq!(
        ids,
        (1005..1018)
            .map(|index| format!("message-{index:04}"))
            .collect::<Vec<_>>()
    );
    let mut query = ListQuery::new(Kind::Session);
    query.statuses = Some(vec!["closed".into()]);
    assert!(store.list(query).await.unwrap().records.is_empty());
    let mut query = ListQuery::new(Kind::Session);
    query.limit = 0;
    assert!(matches!(store.list(query).await, Err(Error::InvalidBounds)));
    store.shutdown().await.unwrap();
}

async fn until(mut ready: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !ready() {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn bounded_admission_and_cancelled_callers_preserve_accepted_mutations() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    database(&path).await;
    let config = Config {
        writer_capacity: 1,
        ..Config::default()
    };
    let store = SqliteAssistantStore::open(&path, config).await.unwrap();
    let mut connection = raw(&path).await;
    let lock = connection.begin_with("BEGIN IMMEDIATE").await.unwrap();
    let first = {
        let store = store.clone();
        tokio::spawn(async move {
            store
                .apply(vec![Mutation::Create(record(
                    Kind::Bookmark,
                    "accepted-one",
                ))])
                .await
        })
    };
    until(|| {
        store.admission().available_bytes < config.queued_bytes
            && store.admission().available_queue_entries == 1
    })
    .await;
    let second = {
        let store = store.clone();
        tokio::spawn(async move {
            store
                .apply(vec![Mutation::Create(record(
                    Kind::Bookmark,
                    "accepted-two",
                ))])
                .await
        })
    };
    until(|| store.admission().available_queue_entries == 0).await;
    assert!(matches!(
        store
            .apply(vec![Mutation::Create(record(Kind::Bookmark, "rejected"))])
            .await,
        Err(Error::Capacity)
    ));
    first.abort();
    second.abort();
    assert!(first.await.unwrap_err().is_cancelled());
    assert!(second.await.unwrap_err().is_cancelled());
    lock.rollback().await.unwrap();
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
    let store = open(&path).await;
    for id in ["accepted-one", "accepted-two"] {
        store.get(Kind::Bookmark, id).await.unwrap();
    }
    assert!(matches!(
        store.get(Kind::Bookmark, "rejected").await,
        Err(Error::NotFound)
    ));
    store.shutdown().await.unwrap();
    let store = SqliteAssistantStore::open(
        &path,
        Config {
            queued_bytes: 1,
            ..Config::default()
        },
    )
    .await
    .unwrap();
    assert!(matches!(
        store
            .apply(vec![Mutation::Create(record(
                Kind::Bookmark,
                "byte-rejected"
            ))])
            .await,
        Err(Error::Capacity)
    ));
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn unsupported_schemas_and_invalid_configuration_are_refused_without_changes() {
    let temp = tempfile::tempdir().unwrap();
    let missing = temp.path().join("missing.db");
    assert!(
        SqliteAssistantStore::open(&missing, Config::default())
            .await
            .is_err()
    );
    assert!(!missing.exists());
    for (index, sql) in [
        "UPDATE schema_versions SET version = 999",
        "UPDATE alembic_version SET version_num = 'future'",
        "DROP INDEX ix_entities_kind_chat_session_created",
    ]
    .iter()
    .enumerate()
    {
        let path = temp.path().join(format!("unsupported-{index}.db"));
        database(&path).await;
        let mut connection = raw(&path).await;
        sqlx::query(sql).execute(&mut connection).await.unwrap();
        connection.close().await.unwrap();
        let before = std::fs::read(&path).unwrap();
        assert!(matches!(
            SqliteAssistantStore::open(&path, Config::default()).await,
            Err(Error::IncompatibleSchema)
        ));
        assert_eq!(std::fs::read(&path).unwrap(), before);
    }
    let path = temp.path().join("valid.db");
    database(&path).await;
    assert!(matches!(
        SqliteAssistantStore::open(
            &path,
            Config {
                readers: 0,
                ..Config::default()
            }
        )
        .await,
        Err(Error::InvalidBounds)
    ));
}

#[tokio::test]
async fn acknowledged_mutations_survive_process_exit() {
    if let Some(path) = std::env::var_os("NEBULA_ENTITY_TEST_CHILD_DB") {
        let store = open(Path::new(&path)).await;
        store
            .apply(vec![Mutation::Create(record(
                Kind::Bookmark,
                "crash-acknowledged",
            ))])
            .await
            .unwrap();
        std::process::exit(0);
    }
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    database(&path).await;
    let status = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "acknowledged_mutations_survive_process_exit",
            "--exact",
            "--nocapture",
        ])
        .env("NEBULA_ENTITY_TEST_CHILD_DB", &path)
        .status()
        .unwrap();
    assert!(status.success());
    let store = open(&path).await;
    store
        .get(Kind::Bookmark, "crash-acknowledged")
        .await
        .unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn oversized_records_corrupt_projections_and_patch_amplification_are_bounded() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    database(&path).await;
    let store = open(&path).await;
    let mut connection = raw(&path).await;
    sqlx::query("UPDATE entities SET chat_session_id='wrong' WHERE id='fixture-chat_messages'")
        .execute(&mut connection)
        .await
        .unwrap();
    assert!(matches!(
        store.get(Kind::Message, "fixture-chat_messages").await,
        Err(Error::CorruptEnvelope)
    ));
    sqlx::query("UPDATE entities SET payload=? WHERE id='fixture-chat_messages'")
        .bind(" ".repeat(MAX_RECORD_BYTES + 1))
        .execute(&mut connection)
        .await
        .unwrap();
    assert!(matches!(
        store.get(Kind::Message, "fixture-chat_messages").await,
        Err(Error::Record(_))
    ));
    for id in ["large-one", "large-two"] {
        let mut p = payload(Kind::Session);
        p["id"] = id.into();
        p["metadata"] = json!({"opaque":"x".repeat(9*1024*1024)});
        store
            .apply(vec![Mutation::Create(
                StoredAssistantRecord::decode(Kind::Session, &serde_json::to_vec(&p).unwrap())
                    .unwrap(),
            )])
            .await
            .unwrap();
    }
    assert!(matches!(
        store
            .apply(vec![
                patch(Kind::Session, "large-one", 2, json!({"title":"changed"})),
                patch(Kind::Session, "large-two", 2, json!({"title":"changed"}))
            ])
            .await,
        Err(Error::TransactionLimit)
    ));
    for id in ["large-one", "large-two"] {
        assert_eq!(
            store.get(Kind::Session, id).await.unwrap().payload()["revision"],
            2
        );
    }
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}
