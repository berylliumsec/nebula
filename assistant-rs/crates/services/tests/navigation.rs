#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_services::{
    AssistantRecords, Error,
    navigation::{BookmarkWrite, SearchRequest, bookmark_id},
};
use nebula_assistant_storage::entities::{
    Config, Error as StorageError, Mutation, SqliteAssistantStore,
};
use serde_json::Value;
use sqlx::Connection;
use std::{path::Path, time::Duration};

fn oracle() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-navigation.json"
    ))
    .unwrap()
}

async fn setup(path: &Path) -> (SqliteAssistantStore, AssistantRecords) {
    support::database(path).await;
    let mut raw = support::raw(path).await;
    sqlx::query("DELETE FROM entities")
        .execute(&mut raw)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut raw)
        .await
        .unwrap();
    let fixture = oracle();
    for row in fixture["projects"].as_array().unwrap() {
        let p = &row["payload"];
        sqlx::query("INSERT INTO entities (id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,'engagements',NULL,?,?,NULL,?,?)")
            .bind(p["id"].as_str().unwrap()).bind(p["revision"].as_i64().unwrap())
            .bind(serde_json::to_string(p).unwrap()).bind("2020-01-01 00:00:00.000000")
            .bind("2020-01-01 00:00:00.000000").execute(&mut raw).await.unwrap();
    }
    raw.close().await.unwrap();
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    let records = fixture["initial_records"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            Mutation::Create(
                StoredAssistantRecord::decode(
                    Kind::try_from(row["kind"].as_str().unwrap()).unwrap(),
                    &serde_json::to_vec(&row["payload"]).unwrap(),
                )
                .unwrap(),
            )
        })
        .collect();
    store.apply(records).await.unwrap();
    (store.clone(), AssistantRecords::new(store))
}

fn payloads(records: Vec<StoredAssistantRecord>) -> Value {
    Value::Array(
        records
            .into_iter()
            .map(StoredAssistantRecord::into_payload)
            .collect(),
    )
}

fn expected(fixture: &Value, name: &str) -> Value {
    fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == name)
        .unwrap()["expected"]["body"]
        .clone()
}

fn write(active: bool, revision: i64) -> BookmarkWrite {
    BookmarkWrite {
        active,
        expected_revision: revision.into(),
    }
}

#[tokio::test]
async fn python_navigation_reads_preserve_history_pagination_and_unicode() {
    let temp = tempfile::tempdir().unwrap();
    let (store, service) = setup(&temp.path().join("nebula.db")).await;
    let fixture = oracle();
    for (name, session, history) in [
        ("messages-current", "session", false),
        ("messages-history", "session", true),
        ("messages-empty", "empty", false),
        ("messages-temporary", "temporary", false),
        ("messages-peer", "peer", false),
        ("messages-foreign", "foreign", false),
    ] {
        assert_eq!(
            payloads(service.session_messages(session, history).await.unwrap()),
            expected(&fixture, name),
            "{name}"
        );
    }
    for (name, text, offset, limit) in [
        ("search-all", "", 0, 50),
        ("search-empty-trim", " \t\n", 0, 50),
        ("search-python-whitespace", "\u{1c}", 0, 50),
        ("search-literal-percent", "%", 0, 50),
        ("search-literal-underscore", "_", 0, 50),
        ("search-literal-slash", "/", 0, 50),
        ("search-literal-path", "a/b", 0, 50),
        ("search-encoded-plus", "+", 0, 50),
        ("search-casefold-excerpt", "MARKER", 0, 50),
        ("search-astral-excerpt", "STRASSE", 0, 50),
        ("search-unicode-lower", "straße", 0, 50),
        ("search-unicode-uppercase-not-folded", "CAFÉ", 0, 50),
        ("search-replaced-first-page", "", 0, 1),
        ("search-page-two", "", 1, 1),
        ("search-two-stored-rows", "", 0, 2),
        ("search-end-page", "", 20, 2),
        ("search-offset-beyond-end", "", 500, 50),
    ] {
        let result = service
            .search_messages(
                "project",
                SearchRequest {
                    q: text.into(),
                    session_id: None,
                    bookmarked: false,
                    offset,
                    limit,
                },
            )
            .await
            .unwrap();
        assert_eq!(result, expected(&fixture, name), "{name}");
    }
    assert_eq!(
        payloads(service.bookmarks("session").await.unwrap()),
        expected(&fixture, "bookmarks-initial")
    );
    let overlong = "🚀".repeat(201);
    for absent in ["", overlong.as_str()] {
        assert!(matches!(service.session_messages(absent, false).await,
            Err(Error::EntityNotFound {kind:"chat_sessions", id}) if id == absent));
        assert!(matches!(service.bookmarks(absent).await,
            Err(Error::EntityNotFound {kind:"chat_sessions", id}) if id == absent));
        assert!(
            matches!(service.set_bookmark("session", absent, write(true, 0)).await,
            Err(Error::EntityNotFound {kind:"chat_messages", id}) if id == absent)
        );
        let query = SearchRequest {
            q: String::new(),
            session_id: None,
            bookmarked: false,
            offset: 0,
            limit: 50,
        };
        assert!(matches!(service.search_messages(absent, query).await,
            Err(Error::EntityNotFound {kind:"engagements", id}) if id == absent));
    }
    let query = SearchRequest {
        q: String::new(),
        session_id: Some(overlong.clone()),
        bookmarked: false,
        offset: 0,
        limit: 50,
    };
    assert!(matches!(service.search_messages("project", query).await,
        Err(Error::EntityNotFound {kind:"chat_sessions", id}) if id == overlong));
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn bookmark_mutations_retain_identity_and_inactive_revision_after_reopen() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let (store, service) = setup(&path).await;
    let identity = bookmark_id("session", "d-casefold");
    let first = service
        .set_bookmark("session", "d-casefold", write(true, 0))
        .await
        .unwrap();
    assert_eq!(first.payload()["id"], identity);
    assert_eq!(first.payload()["revision"], 1);
    assert_eq!(first.payload()["created_at"], first.payload()["updated_at"]);
    let recorded_time = |record: &StoredAssistantRecord, field: &str| {
        chrono::DateTime::parse_from_rfc3339(record.payload()[field].as_str().unwrap()).unwrap()
    };
    assert!(
        matches!(service.set_bookmark("session", "d-casefold", write(true, 0)).await,
        Err(Error::Storage(StorageError::AlreadyExists(id))) if id == identity)
    );
    let inactive = service
        .set_bookmark("session", "d-casefold", write(false, 1))
        .await
        .unwrap();
    assert_eq!(inactive.payload()["revision"], 2);
    assert_eq!(
        inactive.payload()["created_at"],
        first.payload()["created_at"]
    );
    assert_eq!(inactive.payload()["active"], false);
    assert!(recorded_time(&inactive, "updated_at") >= recorded_time(&first, "updated_at"));
    assert!(matches!(
        service
            .set_bookmark("session", "d-casefold", write(true, 1))
            .await,
        Err(Error::Storage(StorageError::RevisionConflict {
            expected: 1,
            found: 2
        }))
    ));
    let huge: BookmarkWrite = serde_json::from_str(
        "{\"active\":true,\"expected_revision\":99999999999999999999999999999999}",
    )
    .unwrap();
    assert!(matches!(
        service.set_bookmark("session", "d-casefold", huge).await,
        Err(Error::RevisionConflict { found: 2, .. })
    ));
    assert!(
        matches!(service.set_bookmark("session", "e-unicode", write(true, 1)).await,
        Err(Error::EntityNotFound{kind:"chat_bookmarks",id}) if id == bookmark_id("session", "e-unicode"))
    );
    for source in ["i-peer", "m-mixed-project"] {
        assert!(matches!(
            service
                .set_bookmark("session", source, write(true, 0))
                .await,
            Err(Error::StorageNotFound(
                "Message is not in this conversation"
            ))
        ));
    }
    assert!(
        service
            .bookmarks("session")
            .await
            .unwrap()
            .contains(&inactive)
    );
    store.shutdown().await.unwrap();
    drop(service);
    drop(store);
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::new(reopened.clone());
    let records = service.bookmarks("session").await.unwrap();
    assert!(records.contains(&inactive));
    assert_eq!(
        reopened.get(Kind::Bookmark, &identity).await.unwrap(),
        inactive
    );
    let active = service
        .set_bookmark("session", "d-casefold", write(true, 2))
        .await
        .unwrap();
    assert_eq!(active.payload()["revision"], 3);
    assert_eq!(active.payload()["active"], true);
    assert_eq!(
        active.payload()["created_at"],
        first.payload()["created_at"]
    );
    assert!(recorded_time(&active, "updated_at") >= recorded_time(&inactive, "updated_at"));
    reopened.shutdown().await.unwrap();
    drop(service);
    drop(reopened);
    let final_store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(
        final_store.get(Kind::Bookmark, &identity).await.unwrap(),
        active
    );
    final_store.shutdown().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_bookmark_writes_have_one_revision_winner() {
    let temp = tempfile::tempdir().unwrap();
    let (store, service) = setup(&temp.path().join("nebula.db")).await;
    for (message, expected_revision) in [("d-casefold", 0), ("b-literal", 1)] {
        let mut tasks = tokio::task::JoinSet::new();
        for index in 0..24 {
            let service = service.clone();
            tasks.spawn(async move {
                service
                    .set_bookmark("session", message, write(index % 2 == 0, expected_revision))
                    .await
            });
        }
        let mut successes = 0;
        while let Some(result) = tasks.join_next().await {
            match result.unwrap() {
                Ok(_) => successes += 1,
                Err(Error::Storage(StorageError::AlreadyExists(_))) if expected_revision == 0 => {}
                Err(Error::Storage(StorageError::RevisionConflict { .. }))
                    if expected_revision == 1 => {}
                other => panic!("unexpected bookmark result: {other:?}"),
            }
        }
        assert_eq!(successes, 1);
        assert_eq!(
            store
                .get(Kind::Bookmark, &bookmark_id("session", message))
                .await
                .unwrap()
                .payload()["revision"],
            expected_revision + 1
        );
    }
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn bookmark_write_rechecks_session_and_message_inside_writer() {
    for source in ["session", "d-casefold"] {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("nebula.db");
        let (store, service) = setup(&path).await;
        let mut connection = support::raw(&path).await;
        let mut tx = connection.begin_with("BEGIN IMMEDIATE").await.unwrap();
        let pending = tokio::spawn(async move {
            service
                .set_bookmark("session", "d-casefold", write(true, 0))
                .await
        });
        tokio::time::timeout(Duration::from_secs(3), async {
            while store.admission().available_bytes == Config::default().queued_bytes {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap();
        sqlx::query(
            "UPDATE entities SET revision=2,payload=json_set(payload,'$.revision',2) WHERE id=?",
        )
        .bind(source)
        .execute(&mut *tx)
        .await
        .unwrap();
        tx.commit().await.unwrap();
        assert!(matches!(
            pending.await.unwrap(),
            Err(Error::Storage(StorageError::Conflict))
        ));
        assert!(matches!(
            store
                .get(Kind::Bookmark, &bookmark_id("session", "d-casefold"))
                .await,
            Err(StorageError::NotFound)
        ));
        connection.close().await.unwrap();
        store.shutdown().await.unwrap();
    }
}
